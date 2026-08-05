"""Completion sound, played with whatever the platform already has -
winsound on Windows, afplay on macOS, paplay/aplay on Linux. QtMultimedia is
deliberately not used (it is excluded from frozen builds for size).

Playback runs on a daemon thread and never raises: a missing player or a bad
file logs once and stays silent.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from app.core import proc

log = logging.getLogger(__name__)

#: A stock sound most Linux desktops ship (freedesktop sound theme).
_LINUX_DEFAULT = "/usr/share/sounds/freedesktop/stereo/complete.oga"
_MAC_DEFAULT = "/System/Library/Sounds/Glass.aiff"


def _play(sound_file: str) -> None:
    try:
        if sys.platform == "win32":
            import winsound

            if sound_file:
                winsound.PlaySound(sound_file, winsound.SND_FILENAME | winsound.SND_ASYNC)
            else:
                winsound.MessageBeep()
            return
        if sys.platform == "darwin":
            path = sound_file or _MAC_DEFAULT
            subprocess.run(["afplay", path], timeout=15, env=proc.clean_env())  # arg list (S1)
            return
        path = sound_file or _LINUX_DEFAULT
        if not Path(path).is_file():
            log.info("no completion sound file at %s", path)
            return
        for player in ("paplay", "aplay", "ffplay"):
            tool = shutil.which(player)
            if tool is None:
                continue
            command = [tool, path]
            if player == "ffplay":
                command = [tool, "-nodisp", "-autoexit", "-loglevel", "quiet", path]
            subprocess.run(command, timeout=15, env=proc.clean_env())
            return
        log.info("no audio player found for the completion sound")
    except Exception:  # never let a sound break a download flow
        log.warning("completion sound failed", exc_info=True)


def play_complete_sound(sound_file: str = "") -> None:
    """Play the completion sound (or the platform default) without blocking."""
    threading.Thread(target=_play, args=(sound_file,), daemon=True).start()
