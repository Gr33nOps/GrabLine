"""Is the GrabLine desktop app running? PID-file based, no sockets (S3).

The Native Messaging host uses this to tell the extension whether a handed-off
URL will start immediately or wait for the next app launch. The app holds an
advisory lock on the file while it lives, so the answer survives a crash that
leaves the pid behind (and a later process inheriting that pid).
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import IO

from app.core import paths

#: The running app's open handle on the pid file. The OS lock lives with this
#: open file, so holding it is what makes "is GrabLine running?" answerable
#: without trusting a number that another process may have inherited.
_held: IO[str] | None = None
_held_path: Path | None = None


def pid_file() -> Path:
    return paths.data_dir() / "grabline.pid"


def write_pid(path: Path | None = None) -> None:
    global _held, _held_path
    target = path or pid_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = open(target, "a+")  # noqa: SIM115 - deliberately held open
    if not _try_lock(handle):
        # Another instance owns it. Nothing to claim, and its pid must stand.
        handle.close()
        return
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    _held = handle
    _held_path = target


def clear_pid(path: Path | None = None) -> None:
    global _held, _held_path
    if _held is not None:
        with contextlib.suppress(OSError):
            _held.close()
        _held = None
        _held_path = None
    (path or pid_file()).unlink(missing_ok=True)


def app_is_running(path: Path | None = None) -> bool:
    """Is a GrabLine desktop app alive right now?

    A bare "does this pid exist?" is not enough: after a crash the number in
    the file outlives the app and the OS hands it to something unrelated, so
    the extension would keep cancelling browser downloads and handing them to
    nobody. The live app holds a lock on this file, so being unable to take
    that lock is positive proof it is still there. The pid check stays as the
    fallback for platforms where locking isn't available.
    """
    target = path or pid_file()
    if _held is not None and _held_path == target:
        return True  # this process is the app
    try:
        with open(target, "r+") as handle:
            if _try_lock(handle):
                _unlock(handle)
                return False
            if _locking_works:
                return True
    except OSError:
        return False
    return _pid_alive(target)


def _pid_alive(target: Path) -> bool:
    try:
        pid = int(target.read_text().strip())
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":  # pragma: no cover - windows-only
        return _pid_running_windows(pid)
    try:
        os.kill(pid, 0)  # signal 0: existence check only (POSIX semantics)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - pid exists, owned by someone else
        return True
    else:
        return True


#: False once a platform proves it can't do advisory locks, so callers know a
#: failed lock means "unsupported", not "someone else holds it".
_locking_works = True


def _try_lock(handle: IO[str]) -> bool:
    """Take an exclusive, non-blocking lock. False when someone else holds it."""
    global _locking_works
    try:
        if sys.platform == "win32":  # pragma: no cover - windows-only
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    except (ImportError, AttributeError):  # pragma: no cover - exotic platform
        _locking_works = False
        return False
    return True


def _unlock(handle: IO[str]) -> None:
    with contextlib.suppress(OSError, ImportError, AttributeError):
        if sys.platform == "win32":  # pragma: no cover - windows-only
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _pid_running_windows(pid: int) -> bool:  # pragma: no cover - windows-only
    """Existence check WITHOUT os.kill: on Windows, os.kill(pid, 0) is not a
    probe - it TerminateProcess()es the target. The native host used to
    murder the running app every time the extension asked 'is it running?'."""
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    # getattr keeps linux mypy happy: ctypes.windll only exists on Windows.
    kernel32 = getattr(ctypes, "windll").kernel32  # noqa: B009
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)
