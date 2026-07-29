"""Execute a user-configured command when a download completes.

The command is the user's own, run on their own machine because they typed it
into Settings - but it is still split with shlex and run as an argument list,
never through a shell (S1), with the finished file's path appended as the
last argument. Runs detached; failures log and never disturb the queue.
"""

from __future__ import annotations

import logging
import shlex
import subprocess

from app.core import proc

log = logging.getLogger(__name__)


def run_script(command: str, file_path: str) -> bool:
    """Launch ``command`` with ``file_path`` appended. Returns whether it
    started (not whether it succeeded - it runs detached)."""
    command = command.strip()
    if not command:
        return False
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        log.warning("script setting could not be parsed: %s", exc)
        return False
    if not parts:
        return False
    try:
        # env=clean_env() so the user's command runs against system libraries,
        # not the frozen app's bundled ones (see proc.clean_env).
        subprocess.Popen([*parts, file_path], env=proc.clean_env())  # arg list, no shell (S1)
        return True
    except OSError as exc:
        log.warning("script failed to start (%s): %s", parts[0], exc)
        return False
