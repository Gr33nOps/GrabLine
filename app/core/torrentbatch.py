"""Bulk torrent import: turn a pile of sources into a checked list of
candidates, all sharing one destination.

The point of the batch importer is that the *decisions* are made once - where
it saves, which queue it joins, whether each torrent gets its own folder - and
then applied to every torrent in the list. So everything here is about turning
raw sources (a file picker's selection, a dropped folder, a box of pasted
magnet links) into candidates that already know their name, their size and
their identity, with the broken and the already-queued ones marked rather than
thrown away.

Deliberately Qt-free and free of manager/DB imports: the dialog runs
:func:`load_candidates` on a worker thread, and the tests run the same
functions with no event loop at all.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from app.core import naming
from app.core.errors import DownloadError
from app.engines import torrent as torrent_engine

log = logging.getLogger(__name__)

#: A hard ceiling on one import. Dropping a home directory onto the window
#: should not walk a million files or queue them; the count is reported and
#: the rest is left alone.
MAX_BATCH = 2000

#: How deep a dropped folder is walked for .torrent files.
MAX_WALK_DEPTH = 6

#: Reasons a candidate cannot be queued. Kept as constants because both the
#: dialog and the tests match on them.
DUPLICATE_IN_BATCH = "already in this list"
DUPLICATE_QUEUED = "already in your downloads"


@dataclass(frozen=True)
class TorrentCandidate:
    """One row of the batch dialog.

    ``source`` is what gets handed to the manager: a magnet link, a local
    .torrent path, or an http(s) .torrent URL. ``error`` set means the source
    could not be read; ``duplicate`` set means it could, but queueing it would
    add the same torrent twice. Either one makes it unqueueable - nothing else
    does.
    """

    source: str
    name: str
    info_hash: str = ""
    #: Every hash form this torrent answers to (v1, v2, truncated v2). One
    #: torrent has several true names; see ``torrent.hash_forms``.
    info_hashes: tuple[str, ...] = ()
    total_size: int = 0
    file_count: int = 0
    error: str = ""
    duplicate: str = ""
    #: The folder this one saves into, filled in by :func:`assign_destinations`.
    dest_dir: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and not self.duplicate

    @property
    def keys(self) -> frozenset[str]:
        """What identifies this torrent for duplicate detection: every form of
        its info-hash *and* its source string.

        Both, because the two sides of the comparison know different things. A
        job that has never started has no info-hash recorded on it, only the
        path or magnet it came from - so matching on hashes alone would call a
        whole folder "new" the second time it is imported. Matching on the
        source alone would miss the same torrent arriving as a magnet. The
        union catches both, and cannot produce a false match: two different
        torrents share neither a hash nor a source.
        """
        return frozenset({*self.info_hashes, self.source.strip().lower()})

    @property
    def key(self) -> str:
        """One stable key for this torrent, for grouping by destination."""
        return self.info_hash or self.source.strip().lower()

    def identity_options(self) -> dict[str, object]:
        """Job options that record what this torrent *is*.

        Written at add time so the next import can tell that it is already in
        the queue. Without it a job only learns its hash once it starts, and a
        torrent queued but not yet running would be re-added by the same
        magnet link.
        """
        if not self.info_hashes:
            return {}
        return {"info_hash": self.info_hash, "info_hashes": list(self.info_hashes)}


# --------------------------------------------------------------- collecting


def is_magnet(text: str) -> bool:
    return text.strip().lower().startswith("magnet:")


def magnet_links(text: str) -> list[str]:
    """Every magnet link in a pasted block, one per line, de-duplicated in
    order. Blank lines and prose are ignored, so pasting a page of text with
    magnets in it works as well as a clean list."""
    found: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        candidate = line.strip()
        if not is_magnet(candidate):
            continue
        # A magnet's own name parameter may contain spaces; a line is one link.
        if candidate in seen:
            continue
        seen.add(candidate)
        found.append(candidate)
    return found


def torrent_files_in(folder: Path, *, recursive: bool = True) -> list[Path]:
    """Every .torrent file under ``folder``, sorted by name so an import is
    reproducible. Unreadable subfolders are skipped, not raised: one
    permission-denied directory must not lose the other 400 files.
    """
    found: list[Path] = []
    root_depth = len(folder.resolve().parts) if folder.is_dir() else 0
    try:
        walker: Iterator[tuple[str, list[str], list[str]]] = os.walk(
            folder, onerror=lambda exc: log.debug("skipping %s: %s", folder, exc)
        )
        for dirpath, dirnames, filenames in walker:
            here = Path(dirpath)
            if not recursive or len(here.resolve().parts) - root_depth >= MAX_WALK_DEPTH:
                dirnames[:] = []
            for filename in filenames:
                if filename.lower().endswith(".torrent"):
                    found.append(here / filename)
                    if len(found) >= MAX_BATCH:
                        return sorted(found)
    except OSError as exc:  # the folder itself went away mid-walk
        log.info("could not read %s: %s", folder, exc)
    return sorted(found)


def expand_sources(raw: Iterable[str], *, recursive: bool = True) -> tuple[list[str], list[str]]:
    """Turn dropped/selected paths into concrete torrent sources.

    A folder expands to the .torrent files inside it, a .torrent file stays
    itself, a magnet link stays itself. Returns ``(sources, ignored)`` - the
    second list is everything that is not a torrent source at all, so the
    dialog can say "3 files were not torrents" instead of silently dropping
    them.
    """
    sources: list[str] = []
    ignored: list[str] = []
    seen: set[str] = set()

    def keep(value: str) -> None:
        if value not in seen:
            seen.add(value)
            sources.append(value)

    for entry in raw:
        text = entry.strip()
        if not text:
            continue
        if is_magnet(text):
            keep(text)
            continue
        if text.lower().startswith(("http://", "https://")):
            if torrent_engine.is_torrent_source(text):
                keep(text)
            else:
                ignored.append(text)
            continue
        path = Path(text)
        if path.is_dir():
            for found in torrent_files_in(path, recursive=recursive):
                keep(str(found))
        elif path.is_file() and text.lower().endswith(".torrent"):
            keep(text)
        else:
            ignored.append(text)
    return sources[:MAX_BATCH], ignored


# ------------------------------------------------------------------ reading


def load_candidate(
    source: str, *, proxy: str | None = None, insecure: bool = False
) -> TorrentCandidate:
    """Read one source into a candidate. Never raises: a source that cannot be
    read comes back with ``error`` set, because one corrupt file in a folder of
    four hundred must not end the import."""
    if is_magnet(source):
        hashes = torrent_engine.info_hashes_from_magnet(source)
        return TorrentCandidate(
            source=source,
            name=torrent_engine.magnet_display_name(source) or "magnet",
            info_hash=hashes[0] if hashes else "",
            info_hashes=hashes,
        )
    try:
        data = torrent_engine.fetch_torrent_bytes(source, proxy, insecure=insecure)
        meta = torrent_engine.parse_torrent(data)
    except DownloadError as exc:
        return TorrentCandidate(
            source=source, name=Path(source.split("?")[0]).name or source, error=str(exc)
        )
    except OSError as exc:  # unreadable file, vanished mid-import
        return TorrentCandidate(
            source=source, name=Path(source.split("?")[0]).name or source, error=str(exc)
        )
    return TorrentCandidate(
        source=source,
        name=meta.name,
        info_hash=meta.info_hash,
        info_hashes=meta.info_hashes,
        total_size=meta.total_size,
        file_count=len(meta.files),
    )


def load_candidates(
    sources: Sequence[str],
    *,
    proxy: str | None = None,
    insecure: bool = False,
    on_progress: object = None,
) -> list[TorrentCandidate]:
    """Read every source in order. ``on_progress`` is called with
    ``(done, total)`` if given, so a dialog can show a bar while a folder of
    several hundred torrents is parsed."""
    candidates: list[TorrentCandidate] = []
    total = len(sources)
    for index, source in enumerate(sources, start=1):
        candidates.append(load_candidate(source, proxy=proxy, insecure=insecure))
        if callable(on_progress):
            on_progress(index, total)
    return candidates


def mark_duplicates(
    candidates: Sequence[TorrentCandidate], existing: Iterable[str] = ()
) -> list[TorrentCandidate]:
    """Flag candidates that repeat within the batch, or that are already in the
    queue. ``existing`` is a set of keys from :func:`queued_keys`.

    The first occurrence of a torrent is kept and the later ones are marked, so
    "48 added, 2 duplicates skipped" always means the user still gets one copy
    of everything they picked.
    """
    already = {key.strip().lower() for key in existing if key and key.strip()}
    seen: set[str] = set()
    marked: list[TorrentCandidate] = []
    for candidate in candidates:
        if candidate.error:
            marked.append(candidate)
            continue
        keys = candidate.keys
        if keys & seen:
            marked.append(replace(candidate, duplicate=DUPLICATE_IN_BATCH))
            continue
        seen |= keys
        marked.append(replace(candidate, duplicate=DUPLICATE_QUEUED if keys & already else ""))
    return marked


def queued_keys(jobs: Iterable[object]) -> set[str]:
    """Duplicate keys for the downloads already in the app: each torrent job's
    recorded info-hash, plus its source string for the ones that have not
    started yet and so have no hash on them.

    Takes plain job objects (duck-typed) rather than importing the manager, to
    keep this module free of the DB layer.
    """
    keys: set[str] = set()
    for job in jobs:
        url = str(getattr(job, "url", "") or "").strip()
        if url:
            keys.add(url.lower())
            keys.update(torrent_engine.info_hashes_from_magnet(url))
        options = getattr(job, "options", None) or {}
        try:
            recorded = [str(options.get("info_hash") or "")]
            recorded += [str(h) for h in options.get("info_hashes") or ()]
        except AttributeError:
            recorded = []
        keys.update(torrent_engine.hash_forms(*recorded))
    return keys


# ------------------------------------------------------------- destinations


class DestinationError(ValueError):
    """The shared destination cannot be used as given."""


def validate_destination(raw: str) -> Path:
    """The shared destination, checked once for the whole batch.

    Returns the resolved folder. Raises :class:`DestinationError` when it is
    blank, is not absolute, or exists as something other than a directory -
    the three ways a mistyped path turns a hundred queued torrents into a
    hundred failures an hour later.
    """
    text = (raw or "").strip()
    if not text:
        raise DestinationError("choose a folder to save into")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise DestinationError("the destination must be a full path")
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise DestinationError(f"that folder cannot be used ({exc})") from exc
    if resolved.exists() and not resolved.is_dir():
        raise DestinationError("that path is a file, not a folder")
    return resolved


def ensure_destination(path: Path) -> Path:
    """Create the destination if it isn't there yet. Raises
    :class:`DestinationError` when it cannot be created or written to."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DestinationError(f"could not create {path} ({exc})") from exc
    if not os.access(path, os.W_OK):
        raise DestinationError(f"no permission to write to {path}")
    return path


def subfolder_for(base: Path, name: str) -> Path:
    """A per-torrent folder inside ``base``, guaranteed to stay inside it.

    The name comes from the torrent, which is attacker-controlled: it can be
    ``../../.ssh``, an absolute Windows path, or a device name. It is
    sanitized to a single path element and the result is re-checked against
    the base, so a crafted torrent cannot write outside the folder the user
    picked.
    """
    leaf = naming.sanitize_filename(name)
    candidate = (base / leaf).resolve()
    if not _is_within(base.resolve(), candidate):  # pragma: no cover - defence in depth
        return (base / naming.FALLBACK_NAME).resolve()
    return candidate


def _is_within(base: Path, target: Path) -> bool:
    try:
        target.relative_to(base)
        return True
    except ValueError:
        return False


def assign_destinations(
    candidates: Sequence[TorrentCandidate], base: Path, *, subfolders: bool
) -> list[TorrentCandidate]:
    """Give every candidate its ``dest_dir``.

    Without ``subfolders`` they all share ``base`` and each torrent's own
    internal directory structure lands inside it, exactly as a single add
    would. With it, each gets ``base/<its name>``; two *different* torrents
    that happen to share a name are separated with a counter, while the same
    torrent added again keeps its folder so a re-add still resumes in place.
    """
    resolved_base = base.resolve()
    if not subfolders:
        return [replace(c, dest_dir=str(resolved_base)) for c in candidates]
    taken: dict[str, str] = {}  # folder path -> the key that owns it
    out: list[TorrentCandidate] = []
    for candidate in candidates:
        folder = subfolder_for(resolved_base, candidate.name)
        owner = taken.get(str(folder))
        counter = 1
        while owner is not None and owner != candidate.key:
            folder = subfolder_for(resolved_base, f"{candidate.name} ({counter})")
            owner = taken.get(str(folder))
            counter += 1
        taken[str(folder)] = candidate.key
        out.append(replace(candidate, dest_dir=str(folder)))
    return out
