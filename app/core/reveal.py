"""Open a download's folder in the OS file manager, selecting the file when it
still exists.

Two platform bugs this exists to avoid:

- On Linux, ``QDesktopServices.openUrl`` on a ``file://`` URL routes through
  xdg-open to ``x-scheme-handler/file``, which is usually the web browser. We
  pass a plain directory path instead, resolved as ``inode/directory``.
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

# Linux file managers to try, best first: xdg-open honours the desktop's
# default, gio is the GLib opener, and the rest are the common concrete apps.
_LINUX_OPENERS = ("xdg-open", "gio", "nautilus", "dolphin", "nemo", "thunar", "pcmanfm", "caja")


def unix_command(
    directory: Path,
    platform: str,
    *,
    reveal: Path | None = None,
    which: Callable[[str], str | None] | None = None,
) -> list[str] | None:
    """The argv that opens *directory* in the file manager on a non-Windows
    *platform* (selecting *reveal* on macOS), or ``None`` if no opener is found.
    Pure - it never runs a subprocess, so the choice is testable on any host."""
    if platform == "darwin":
        return ["open", "-R", str(reveal)] if reveal is not None else ["open", str(directory)]
    # Linux / other X-Desktop unix: a plain directory path, never a file:// URL.
    resolve = which or shutil.which
    for tool in _LINUX_OPENERS:
        found = resolve(tool)
        if not found:
            continue
        if tool == "gio":
            return [found, "open", str(directory)]
        return [found, str(directory)]
    return None


def open_folder(path: str | Path) -> bool:
    """Open *path*'s folder in the OS file manager, selecting the file when
    *path* is an existing file (Windows and macOS). A directory opens directly;
    anything else opens its parent. Returns ``True`` if a manager was launched."""
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
