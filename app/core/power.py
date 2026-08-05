"""System power actions for the "when all downloads finish" setting, plus
battery detection for battery mode.

Every command is an argument list (never a shell string, per S1) and best
effort: if the platform tool is missing or refused, we log and move on rather
than raise. These only ever run because the user explicitly chose them.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time

from app.core import proc

log = logging.getLogger(__name__)

_SLEEP = {
    "linux": ["systemctl", "suspend"],
    "darwin": ["pmset", "sleepnow"],
    "win32": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
}
_SHUTDOWN = {
    "linux": ["systemctl", "poweroff"],
    "darwin": ["osascript", "-e", 'tell app "System Events" to shut down'],
    "win32": ["shutdown", "/s", "/t", "60"],
}
#: macOS has no user-triggerable hibernate command; pmset sleepnow honors the
#: machine's hibernatemode, which is the closest honest equivalent.
_HIBERNATE = {
    "linux": ["systemctl", "hibernate"],
    "darwin": ["pmset", "sleepnow"],
    "win32": ["shutdown", "/h"],
}
_LOCK = {
    "linux": ["loginctl", "lock-session"],
    "darwin": [
        "/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession",
        "-suspend",
    ],
    "win32": ["rundll32.exe", "user32.dll,LockWorkStation"],
}


def _run(command: list[str]) -> bool:
    try:
        subprocess.Popen(command, env=proc.clean_env())  # arg list only, no shell (S1)
        return True
    except OSError as exc:
        log.warning("power command failed (%s): %s", command[0], exc)
        return False


def sleep() -> bool:
    """Put the computer to sleep. Returns whether the command launched."""
    command = _SLEEP.get(sys.platform)
    return _run(command) if command else False


def shutdown() -> bool:
    """Shut the computer down. Returns whether the command launched."""
    command = _SHUTDOWN.get(sys.platform)
    return _run(command) if command else False


def hibernate() -> bool:
    """Hibernate the computer (sleep on macOS - see _HIBERNATE)."""
    command = _HIBERNATE.get(sys.platform)
    return _run(command) if command else False


def lock() -> bool:
    """Lock the session without stopping anything."""
    command = _LOCK.get(sys.platform)
    return _run(command) if command else False


# ---------------------------------------------------------------- battery

_BATTERY_CACHE_SECONDS = 5.0
_battery_checked = 0.0
_battery_state = False


def battery_percent() -> int | None:
    """Current charge (0-100), or None when there is no battery / no reading."""
    try:
        import psutil

        battery = psutil.sensors_battery()
        return int(battery.percent) if battery is not None else None
    except Exception:  # pragma: no cover - platform/driver quirks
        return None


def on_battery() -> bool:
    """True when running on battery power (battery mode pauses downloads).

    Uses psutil, cached a few seconds - the scheduler asks twice a second.
    Desktops (no battery) and any probe failure count as plugged in: the
    safe default is to keep downloading.
    """
    global _battery_checked, _battery_state
    now = time.monotonic()
    if now - _battery_checked < _BATTERY_CACHE_SECONDS:
        return _battery_state
    _battery_checked = now
    try:
        import psutil

        battery = psutil.sensors_battery()
        _battery_state = battery is not None and not battery.power_plugged
    except Exception:  # pragma: no cover - platform/driver quirks
        _battery_state = False
    return _battery_state
