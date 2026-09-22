"""Torrent watch folder: drop a .torrent into a folder, GrabLine queues it.

Polled rather than event-driven, on purpose. The one thing a watch folder must
get right is not importing a file that is still being written - a half-copied
.torrent parses as garbage, and a filesystem event fires the moment the file
is *created*, not when the copy finishes. So each scan records what it saw,
and a file is only handed over once two consecutive scans agree on its size
and modification time. That also makes the whole thing testable without any
platform watcher, and behaves the same on a network share, where inotify-style
events are unreliable anyway.

Nothing here deletes or moves the user's .torrent files. Imported ones are
remembered by info-hash (falling back to path+size) so the same file is not
queued again on the next scan, or after a restart.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.core import torrentbatch

log = logging.getLogger(__name__)

#: How many scans a file must look unchanged for before it is imported. Two
#: means "seen once, then seen again the same" - one poll interval of quiet.
STABLE_SCANS = 2

#: Cap on the remembered-import list, like the RSS seen-list. Old entries fall
#: off the front; a .torrent that old being re-imported is not a real risk.
MAX_REMEMBERED = 500

#: Refuse to walk a folder someone pointed at their whole disk.
MAX_PER_SCAN = 200


@dataclass(frozen=True)
class WatchResult:
    """What one scan found: files ready to import, plus why nothing happened."""

    ready: tuple[Path, ...] = ()
    #: Files seen but not yet stable (still being copied) - purely informational.
    settling: tuple[Path, ...] = ()
    error: str = ""


class TorrentWatcher:
    """The polling state for one watched folder.

    Holds only in-memory stability bookkeeping; the permanent "already
    imported" list lives in Settings, so it survives a restart, and is passed
    in on every scan rather than cached here.
    """

    def __init__(self) -> None:
        #: path -> (size, mtime_ns, how many scans it has looked like this)
        self._seen: dict[str, tuple[int, int, int]] = {}
        self._folder: str | None = None

    def reset(self) -> None:
        self._seen.clear()
        self._folder = None

    def scan(self, folder: Path | str | None, imported: Iterable[str] = ()) -> WatchResult:
        """One poll of the folder.

        ``imported`` is the set of keys already queued (info-hashes, or
        ``path|size`` for files whose hash could not be read). Files in it are
        skipped without being parsed again.
        """
        if not folder:
            self.reset()
            return WatchResult()
        path = Path(folder).expanduser()
        if str(path) != self._folder:  # the folder changed: start over
            self._seen.clear()
            self._folder = str(path)
        if not path.is_dir():
            # An unplugged drive or a deleted folder is a quiet no-op, not an
            # error dialog every poll: it may well come back.
            self._seen.clear()
            return WatchResult(error=f"the watch folder is not available: {path}")

        known = {key.strip().lower() for key in imported if key and key.strip()}
        candidates = torrentbatch.torrent_files_in(path, recursive=False)[:MAX_PER_SCAN]
        present = {str(p) for p in candidates}
        for gone in [p for p in self._seen if p not in present]:
            del self._seen[gone]  # the file was moved away; forget its progress

        ready: list[Path] = []
        settling: list[Path] = []
        for file_path in candidates:
            if watch_key_for_path(file_path) in known:
                continue
            try:
                stat = file_path.stat()
            except OSError:  # vanished between listing and stat
                continue
            signature = (stat.st_size, stat.st_mtime_ns)
            previous = self._seen.get(str(file_path))
            count = previous[2] + 1 if previous is not None and previous[:2] == signature else 1
            self._seen[str(file_path)] = (*signature, count)
            if stat.st_size == 0:
                continue  # a freshly created, still-empty file
            if count >= STABLE_SCANS:
                ready.append(file_path)
            else:
                settling.append(file_path)
        return WatchResult(ready=tuple(ready), settling=tuple(settling))


def watch_key_for_path(path: Path) -> str:
    """The cheap identity of a file on disk, used to skip re-parsing something
    already imported: its path and size. Two different torrents never share
    both, and the same file keeps both across restarts."""
    try:
        return f"{path.resolve()}|{path.stat().st_size}".lower()
    except OSError:
        return str(path).lower()


def watch_keys_for(path: Path, info_hash: str = "") -> list[str]:
    """Everything worth remembering about an imported file: its info-hash (so
    the same torrent copied in under another name is still skipped) and its
    path+size (so an unparseable file is not retried forever)."""
    keys = [watch_key_for_path(path)]
    if info_hash:
        keys.append(info_hash.strip().lower())
    return keys


def remember(existing: Sequence[str], new_keys: Iterable[str]) -> list[str]:
    """The updated remembered-import list, de-duplicated and capped."""
    merged = list(dict.fromkeys([*existing, *(k.strip().lower() for k in new_keys if k.strip())]))
    return merged[-MAX_REMEMBERED:]
