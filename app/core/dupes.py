"""Duplicate detection for downloaded files: group by size first (cheap),
then confirm with SHA-256 so only byte-identical files are called duplicates.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from app.core.verify import hash_file


def find_duplicates(paths: Iterable[Path]) -> list[list[Path]]:
    """Groups of byte-identical files, each group ordered as given (so the
    first entry is the natural 'keep me' - the oldest download). Files that
    vanished or can't be read are skipped, never guessed about.
    """
    by_size: dict[int, list[Path]] = defaultdict(list)
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:  # the same file queued twice is not a duplicate
            continue
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
        except OSError:
            continue
        seen.add(resolved)
        by_size[size].append(path)

    groups: list[list[Path]] = []
    for candidates in by_size.values():
        if len(candidates) < 2:
            continue
        by_hash: dict[str, list[Path]] = defaultdict(list)
        for path in candidates:
            try:
                by_hash[hash_file(path)].append(path)
            except OSError:
                continue
        groups.extend(group for group in by_hash.values() if len(group) > 1)
    return groups
