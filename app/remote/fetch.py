"""File facts for ``files``, ``fetch`` and ``fileHash`` (PROTOCOL.md section 13).

Pure helpers, no Qt: the GUI-thread handlers use them to turn one of the PC's
own job ids into a ``FileTicket`` (never a path from the phone), and the
listener thread uses the ticket to stream the file. ``FileHasher`` computes
SHA-256 on worker threads, cached by path, size and modification time.
"""

from __future__ import annotations

import hashlib
import os
import stat
import struct
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

CHUNK_SIZE = 262_144  # 256 KiB payload per binary frame
WINDOW_BYTES = 8_388_608  # unacknowledged bytes allowed in flight
STREAM_BACKLOG_BYTES = 1_048_576  # no chunk is queued above this socket backlog
STALL_SECONDS = 60.0  # no ack for this long ends the stream with ``failed``
ACKS_PER_SECOND = 50  # per connection; extra ones are ignored
MAX_STREAMS_PER_CONNECTION = 1
MAX_STREAMS_PER_DEVICE = 2
MAGIC = b"GLF1"
_HEADER = struct.Struct(">4sIQ")
HASH_BLOCK = 1_048_576
HASH_CACHE_SIZE = 64
HASH_WORKERS = 2
HASH_QUEUE = 8


def etag_for(size: int, mtime: int) -> str:
    """Opaque validator made from size and modification time."""
    return f"s{size}-m{mtime}"


def frame(stream_id: int, offset: int, payload: bytes) -> bytes:
    """One binary frame body: ``GLF1``, streamId u32, offset u64, payload."""
    return _HEADER.pack(MAGIC, stream_id, offset) + payload


@dataclass(frozen=True)
class FileTicket:
    """What the listener may open for one ``fetch``: the PC's stored path,
    checked by the handler and checked again on the opened handle."""

    path: str
    size: int
    etag: str
    offset: int
    length: int
    job_id: int


def regular_file(path: Path) -> os.stat_result | None:
    """``stat`` of ``path`` if it is one regular file (not a link, folder or
    device), else None."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return info if stat.S_ISREG(info.st_mode) else None


def etag_of(info: os.stat_result) -> str:
    return etag_for(info.st_size, int(info.st_mtime))


class FileHasher:
    """SHA-256 off the GUI thread. ``submit`` returns at once; ``done`` is
    called on a worker thread with the digest or None (file gone or changed)."""

    def __init__(self) -> None:
        self._cache: OrderedDict[tuple[str, int, int], str] = OrderedDict()
        self._lock = threading.Lock()
        self._slots = threading.Semaphore(HASH_WORKERS)
        self._waiting = 0
        self._closed = threading.Event()

    def close(self) -> None:
        self._closed.set()

    def cached(self, path: str, size: int, mtime: int) -> str | None:
        with self._lock:
            digest = self._cache.get((path, size, mtime))
            if digest is not None:
                self._cache.move_to_end((path, size, mtime))
            return digest

    def submit(self, path: str, size: int, mtime: int, done: Callable[[str | None], None]) -> bool:
        """False if too many hashes are already waiting (the caller says busy)."""
        digest = self.cached(path, size, mtime)
        if digest is not None:
            done(digest)
            return True
        with self._lock:
            if self._waiting >= HASH_QUEUE:
                return False
            self._waiting += 1
        threading.Thread(
            target=self._run, args=(path, size, mtime, done), name="grabline-hash", daemon=True
        ).start()
        return True

    def _run(self, path: str, size: int, mtime: int, done: Callable[[str | None], None]) -> None:
        result: str | None = None
        try:
            with self._slots:
                result = self._hash(path, size, mtime)
        except Exception:
            result = None
        finally:
            with self._lock:
                self._waiting -= 1
        done(result)

    def _hash(self, path: str, size: int, mtime: int) -> str | None:
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or etag_of(info) != etag_for(size, mtime):
                    return None
                while block := handle.read(HASH_BLOCK):
                    if self._closed.is_set():
                        return None
                    digest.update(block)
            after = os.lstat(path)
        except OSError:
            return None
        if after.st_size != size or int(after.st_mtime) != mtime:
            return None
        hexed = digest.hexdigest()
        with self._lock:
            self._cache[(path, size, mtime)] = hexed
            while len(self._cache) > HASH_CACHE_SIZE:
                self._cache.popitem(last=False)
        return hexed
