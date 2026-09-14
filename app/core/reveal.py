"""Open a download's folder in the OS file manager, selecting the file when it
still exists.

Three platform bugs this exists to avoid:

- On Linux, ``QDesktopServices.openUrl`` on a ``file://`` URL routes through
  xdg-open to ``x-scheme-handler/file``, which is usually the web browser. We
  pass a plain directory path instead, never a URL.
- Still on Linux, ``xdg-open`` on a *directory* is not safe either. It resolves
  ``inode/directory`` through the desktop's mimeapps list, and on a system
  where a browser has claimed that association (Brave and Chrome both register
  handlers that accept directories) the folder opens as a file listing *inside
  the browser*. That is the reported bug. So xdg-open is a last resort, after
  every real file manager: we ask the desktop environment which manager it
  ships, and otherwise take the first installed one we recognise.
- On Windows, launching ``explorer`` with the console-hiding startupinfo the app
  uses for FFmpeg (``STARTF_USESHOWWINDOW`` / ``SW_HIDE``) opened the folder
  window *hidden*, so "Open folder" looked like it did nothing. We use
  ``os.startfile``, which never carries those flags.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from app.core import proc

#: Every Linux file manager we know how to launch, in the order we would pick
#: one when the desktop gives us no better hint. Deliberately only real file
#: managers - nothing in here can open an http(s) URL, so a browser can never
#: be chosen as "the file manager" no matter how the system is configured.
_FILE_MANAGERS: tuple[str, ...] = (
    "nautilus",  # GNOME / Budgie / Pop
    "dolphin",  # KDE Plasma
    "nemo",  # Cinnamon / Linux Mint
    "thunar",  # Xfce
    "caja",  # MATE
    "pcmanfm-qt",  # LXQt
    "pcmanfm",  # LXDE
    "dde-file-manager",  # Deepin
    "io.elementary.files",  # elementary Pantheon
    "cosmic-files",  # COSMIC
    "nnn",  # nothing graphical left: a terminal manager still beats a browser
)

#: What each desktop environment ships, keyed by the lower-cased tokens that
#: turn up in ``XDG_CURRENT_DESKTOP`` / ``DESKTOP_SESSION``. The value is a
#: preference order, not a single name: "KDE but Dolphin isn't installed"
#: should still land on a real manager rather than fall through to xdg-open.
_DESKTOP_PREFERENCE: dict[str, tuple[str, ...]] = {
    "cinnamon": ("nemo",),
    "x-cinnamon": ("nemo",),
    "kde": ("dolphin",),
    "plasma": ("dolphin",),
    "gnome": ("nautilus",),
    "gnome-classic": ("nautilus",),
    "gnome-flashback": ("nautilus",),
    "ubuntu": ("nautilus",),
    "unity": ("nautilus",),
    "budgie": ("nautilus",),
    "pop": ("nautilus",),
    "xfce": ("thunar",),
    "mate": ("caja",),
    "lxqt": ("pcmanfm-qt", "pcmanfm"),
    "lxde": ("pcmanfm", "pcmanfm-qt"),
    "deepin": ("dde-file-manager",),
    "dde": ("dde-file-manager",),
    "pantheon": ("io.elementary.files",),
    "cosmic": ("cosmic-files",),
}

#: How to ask a given manager to open a folder with one file highlighted. Only
#: managers that document the flag are listed: guessing wrong here is worse
#: than not selecting, because a file path handed to a manager that does *not*
#: understand it is opened in its default application instead (a video player,
#: a PDF reader) - which is not what "Open in folder" means. Everything else
#: opens the containing directory, which is always correct.
_SELECT_FLAG: dict[str, str] = {
    "nautilus": "--select",
    "dolphin": "--select",
    "io.elementary.files": "--select",
    "dde-file-manager": "--show-item",
}

#: Generic URL handlers. These are a fallback only - they are exactly the ones
#: that can route a directory to a browser, which is the bug this module is
#: about. ``gio open`` goes first: it resolves ``inode/directory`` through GIO
#: rather than the ``x-scheme-handler`` chain, so it misroutes less often.
_GENERIC_OPENERS: tuple[str, ...] = ("gio", "xdg-open")


def _desktop_tokens(environ: dict[str, str] | None = None) -> list[str]:
    """The desktop-environment names advertised by the session, lower-cased.

    ``XDG_CURRENT_DESKTOP`` is the standard and may be colon-separated
    ("ubuntu:GNOME"); ``DESKTOP_SESSION`` and ``XDG_SESSION_DESKTOP`` are the
    older spellings still set by several session managers.
    """
    env = os.environ if environ is None else environ
    tokens: list[str] = []
    for name in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION"):
        raw = env.get(name) or ""
        for part in raw.replace(";", ":").split(":"):
            part = part.strip().lower()
            # DESKTOP_SESSION is sometimes a path (/usr/share/xsessions/cinnamon).
            part = part.rsplit("/", 1)[-1]
            if part and part not in tokens:
                tokens.append(part)
    return tokens


def preferred_managers(environ: dict[str, str] | None = None) -> tuple[str, ...]:
    """File-manager executables to try, best first, for this desktop.

    The desktop's own manager leads (Cinnamon -> Nemo, KDE -> Dolphin, GNOME ->
    Nautilus, Xfce -> Thunar, MATE -> Caja, LXQt/LXDE -> PCManFM), then every
    other manager we recognise, so an unusual mix (Dolphin on Xfce, a bare
    window manager) still opens a real file manager. Pure - it looks at the
    environment only, never at the filesystem - so the choice is testable.
    """
    ordered: list[str] = []
    for token in _desktop_tokens(environ):
        for manager in _DESKTOP_PREFERENCE.get(token, ()):
            if manager not in ordered:
                ordered.append(manager)
    for manager in _FILE_MANAGERS:
        if manager not in ordered:
            ordered.append(manager)
    return tuple(ordered)


def _linux_command(
    directory: Path,
    reveal: Path | None,
    resolve: Callable[[str], str | None],
    environ: dict[str, str] | None = None,
) -> list[str] | None:
    for manager in preferred_managers(environ):
        found = resolve(manager)
        if not found:
            continue
        flag = _SELECT_FLAG.get(manager)
        if reveal is not None and flag is not None:
            return [found, flag, str(reveal)]
        return [found, str(directory)]
    # No real file manager on this machine. Only now do we hand the directory
    # to a generic handler, which may or may not route it somewhere sensible.
    for opener in _GENERIC_OPENERS:
        found = resolve(opener)
        if not found:
            continue
        return [found, "open", str(directory)] if opener == "gio" else [found, str(directory)]
    return None


def unix_command(
    directory: Path,
    platform: str,
    *,
    reveal: Path | None = None,
    which: Callable[[str], str | None] | None = None,
    environ: dict[str, str] | None = None,
) -> list[str] | None:
    """The argv that opens *directory* in the file manager on a non-Windows
    *platform*, selecting *reveal* where the chosen manager supports it, or
    ``None`` if nothing suitable is installed. Pure - it never runs a
    subprocess, so the choice is testable on any host."""
    if platform == "darwin":
        return ["open", "-R", str(reveal)] if reveal is not None else ["open", str(directory)]
    # Linux / other X-Desktop unix: a plain path, never a file:// URL.
    return _linux_command(directory, reveal, which or shutil.which, environ)


def open_folder(path: str | Path) -> bool:
    """Open *path*'s folder in the OS file manager, selecting the file when
    *path* is an existing file and the platform's manager supports it. A
    directory opens directly; anything else opens its parent. Returns ``True``
    if a manager was launched."""
    target = Path(path)
    reveal = target if target.is_file() else None
    directory = target if target.is_dir() else target.parent
    if sys.platform == "win32":  # pragma: no cover - windows-only
        try:
            if reveal is not None:
                # /select highlights the file inside its folder. No hidden
                # startupinfo: it opened the window invisibly (the reported bug).
                subprocess.Popen(["explorer", "/select,", str(reveal)])
            else:
                os.startfile(str(directory))
        except OSError:
            return False
        return True
    command = unix_command(directory, sys.platform, reveal=reveal)
    if command is None:
        return False
    try:
        # env=clean_env(): launch the file manager with the system's own
        # libraries, not the frozen app's bundled ones. Without this the AppImage
        # broke xdg-open and "Open folder" opened a browser/terminal instead.
        subprocess.Popen(command, env=proc.clean_env())  # arg list only, no shell (S1)
    except OSError:
        return False
    return True
