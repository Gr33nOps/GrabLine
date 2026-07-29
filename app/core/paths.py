"""Platform data directories, Qt-free so the CLI and core never import PySide6.

Locations match what Qt's QStandardPaths.AppDataLocation resolves to with the
application/organization name "Grabline", so existing databases stay in place.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# The data-folder identifier stays "Grabline" (not the "GrabLine" brand): it is
# the QStandardPaths folder name, and renaming it would orphan every user's
# database, settings and downloaded ffmpeg/deno binaries. Users never see it.
APP_NAME = "Grabline"

log = logging.getLogger(__name__)


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (and parents) and make it private to the current user.

    The data folder holds the SQLite DB - which stores API keys and the
    session cookies a browser handoff passed through. On a shared POSIX machine
    a world-readable data dir would let another local user read them, so the
    directory is locked to 0700: no other user can even list it, which covers
    the DB and its -wal/-shm sidecars in one stroke (CWE-732). On Windows the
    per-user LOCALAPPDATA is already ACL-protected, so this is POSIX-only, and
    best-effort - a chmod failure logs and never blocks startup.
    """
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            path.chmod(0o700)  # explicit, since mkdir's mode is masked by umask
        except OSError as exc:  # pragma: no cover - unusual filesystem
            log.warning("could not restrict permissions on %s: %s", path, exc)
    return path


def data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return base / APP_NAME


def bin_dir() -> Path:
    """Where fetched, checksum-verified binaries (FFmpeg) are installed."""
    return data_dir() / "bin"


def default_download_dir() -> Path:
    return Path.home() / "Downloads" / APP_NAME
