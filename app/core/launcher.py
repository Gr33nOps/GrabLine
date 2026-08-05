"""Desktop integration, IDM-style: an application-menu entry and start-on-login.

Everything here is per-user (no root) and idempotent. Linux gets the full
treatment - an XDG desktop entry written on first run so GrabLine shows up in
the app grid/dock, and an autostart entry behind the Settings toggle. Windows
autostart uses the HKCU Run key; macOS a LaunchAgent plist. Entries launch the
current interpreter (``python -m app``), so a moved checkout heals itself.

Qt-free: the caller supplies rendered icon bytes.
"""

from __future__ import annotations

import contextlib
import os
import plistlib
import shlex
import sys
from pathlib import Path

# The brand name the menu entry shows.
DISPLAY_NAME = "GrabLine"
# The window-class / registry identifier: must stay "Grabline" so StartupWMClass
# keeps matching the running window (taskbar icon) and existing autostart
# registry values are still found.
APP_NAME = "Grabline"
_ENTRY_ID = "grabline"
_MAC_LABEL = "dev.grabline.desktop"


def launch_command(*, minimized: bool = False, windowless: bool = False) -> list[str]:
    """How to start this very installation of GrabLine.

    ``windowless`` swaps python.exe for pythonw.exe on Windows so a login-time
    autostart doesn't flash a console window (no-op elsewhere)."""
    if getattr(sys, "frozen", False):
        # Frozen build: the executable *is* GrabLine (already windowed), so
        # there's no interpreter or ``-m app`` module to invoke.
        command = [sys.executable]
        if minimized:
            command.append("--minimized")
        return command
    executable = sys.executable
    if windowless and sys.platform == "win32":  # pragma: no cover - windows-only
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if pythonw.exists():
            executable = str(pythonw)
    command = [executable, "-m", "app"]
    if minimized:
        command.append("--minimized")
    return command


def _exec_line(command: list[str]) -> str:
    """A shell command line for the platform. Windows Run keys and .bat use
    double quotes; POSIX shells (and freedesktop Exec) use shlex quoting."""
    if sys.platform == "win32":  # pragma: no cover - windows-only
        return " ".join(f'"{part}"' if " " in part else part for part in command)
    return " ".join(shlex.quote(part) for part in command)


def _xdg_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))


def _xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))


def _icon_path() -> Path:
    return _xdg_data_home() / "icons" / "hicolor" / "256x256" / "apps" / f"{_ENTRY_ID}.png"


def _menu_entry_path() -> Path:
    return _xdg_data_home() / "applications" / f"{_ENTRY_ID}.desktop"


def _autostart_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{_MAC_LABEL}.plist"
    return _xdg_config_home() / "autostart" / f"{_ENTRY_ID}.desktop"


def _desktop_entry(command: list[str], *, icon: Path | None, autostart: bool = False) -> str:
    # The menu entry takes a %u so double-clicked .torrent files and magnet
    # links land in GrabLine; the autostart entry must not (it opens nothing).
    exec_line = _exec_line(command) + ("" if autostart else " %u")
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        f"Name={DISPLAY_NAME}",
        "GenericName=Download Manager",
        "Comment=Download manager with browser integration, video and torrent support",
        f"Exec={exec_line}",
        f"Icon={icon if icon is not None else _ENTRY_ID}",
        "Terminal=false",
        "Categories=Network;FileTransfer;Qt;",
        "StartupNotify=false",
    ]
    if autostart:
        lines.append("X-GNOME-Autostart-enabled=true")
    else:
        # Keywords feed the desktop's search; StartupWMClass ties the running
        # window back to this entry so it groups and pins correctly.
        lines.append("Keywords=download;downloader;torrent;video;manager;")
        lines.append("MimeType=application/x-bittorrent;x-scheme-handler/magnet;")
        lines.append(f"StartupWMClass={APP_NAME}")
    return "\n".join(lines) + "\n"


def packaged_install() -> bool:
    """True when a system package (.deb/.rpm) installed this copy.

    Such a package ships its own /usr/share/applications entry, so writing a
    second per-user one would list GrabLine twice in the app grid and in
    search. AppImage and tarball runs are *not* packaged - nothing else
    provides an entry for them, so they still get one."""
    if not getattr(sys, "frozen", False):
        return False
    executable = str(Path(sys.executable).resolve())
    if executable.startswith(("/opt/", "/usr/")):
        return True
    return Path("/usr/share/applications/grabline.desktop").is_file()


def _write_if_changed(path: Path, content: str) -> None:
    if path.exists() and path.read_text() == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# ------------------------------------------------------------- menu entry


def install_menu_entry(icon_png: bytes | None = None) -> Path | None:
    """Put GrabLine in the application menu (Linux; silently a no-op
    elsewhere - the Windows installer and macOS app bundle own that job).
    Safe to call on every startup: rewrites only when something changed,
    so a moved venv heals itself."""
    if sys.platform != "linux" or packaged_install():
        return None
    icon = _icon_path()
    if icon_png is not None and (not icon.exists() or icon.read_bytes() != icon_png):
        icon.parent.mkdir(parents=True, exist_ok=True)
        icon.write_bytes(icon_png)
    entry = _menu_entry_path()
    _write_if_changed(entry, _desktop_entry(launch_command(), icon=icon if icon.exists() else None))
    return entry


# -------------------------------------------------------------- autostart


def autostart_enabled() -> bool:
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"
            ) as key:
                winreg.QueryValueEx(key, APP_NAME)
                return True
        except OSError:
            return False
    return _autostart_path().exists()


def set_autostart(enabled: bool) -> None:
    """Start GrabLine (minimized to the tray) on login - or stop doing so."""
    command = launch_command(minimized=True, windowless=True)
    if sys.platform == "win32":
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _exec_line(command))
            else:
                with contextlib.suppress(OSError):
                    winreg.DeleteValue(key, APP_NAME)
        return
    path = _autostart_path()
    if not enabled:
        path.unlink(missing_ok=True)
        return
    if sys.platform == "darwin":
        payload = plistlib.dumps(
            {"Label": _MAC_LABEL, "ProgramArguments": command, "RunAtLoad": True}
        ).decode()
        _write_if_changed(path, payload)
        return
    icon = _icon_path()
    _write_if_changed(
        path,
        _desktop_entry(command, icon=icon if icon.exists() else None, autostart=True),
    )


def restart_current() -> bool:
    """Spawn a fresh GrabLine process for this install, then return True so the
    caller can quit the running one. Used when a setting (e.g. language) needs a
    clean UI rebuild. Returns False if the new process could not be started."""
    command = launch_command()
    try:
        # Prefer Qt's detached spawn when a QApplication is alive (GUI path);
        # fall back to subprocess for tests / non-Qt callers.
        from PySide6.QtCore import QProcess
        from PySide6.QtWidgets import QApplication

        if QApplication.instance() is not None:
            return bool(QProcess.startDetached(command[0], command[1:]))
    except Exception:
        pass
    import subprocess

    try:
        subprocess.Popen(command, close_fds=True)
        return True
    except OSError:
        return False
