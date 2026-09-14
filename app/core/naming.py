"""Filename derivation and sanitization (S4: filesystem safety)."""

from __future__ import annotations

import logging
import mimetypes
import os
import posixpath
import re
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import unquote, urlsplit

FALLBACK_NAME = "download"
MAX_NAME_LENGTH = 150
_MAX_EXT_LENGTH = 16

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def sanitize_filename(name: str, max_length: int = MAX_NAME_LENGTH) -> str:
    """Make an arbitrary (often title-derived) string safe as a filename."""
    name = _INVALID_CHARS.sub("_", name).strip().strip(".")
    if not name:
        return FALLBACK_NAME
    if name.split(".")[0].upper() in _WINDOWS_RESERVED:
        name = "_" + name
    if len(name) > max_length:
        root, ext = os.path.splitext(name)
        ext = ext[:_MAX_EXT_LENGTH]
        name = root[: max_length - len(ext)] + ext
    return name


def filename_from_url(url: str) -> str:
    name = unquote(posixpath.basename(urlsplit(url).path))
    return sanitize_filename(name) if name else FALLBACK_NAME


#: Stems servers hand out that say nothing about the content (F1.8).
_UGLY_STEMS = frozenset(
    {
        "videoplayback",
        "download",
        "index",
        "file",
        "video",
        "audio",
        "media",
        "stream",
        "content",
        "item",
        "untitled",
        "attachment",
        "get",
        "fetch",
        FALLBACK_NAME,
    }
)


#: 8-4-4-4-12 UUIDs and long bare hex runs - cache keys, not names.
_UUID_STEM = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HEX_STEM = re.compile(r"^[0-9a-f]{16,}$")


def is_ugly_name(name: str) -> bool:
    """Would a human curse this filename? (videoplayback.mp4, index, 1234…,
    1434659607842-pgv4ql-1642193429401.mp4)"""
    stem = Path(name).stem.lower()
    if len(stem) < 3 or stem.isdigit() or stem in _UGLY_STEMS:
        return True
    if _UUID_STEM.match(stem) or _HEX_STEM.match(stem):
        return True
    # ID soup: a long spaceless token that is mostly digits is a CDN asset
    # key (timestamps, video ids), not a title. Real release names carry
    # their digits sparsely ("Movie.2024.1080p" is ~25% digits), so half the
    # characters being digits separates the two cleanly.
    if " " not in stem and len(stem) >= 12:
        digits = sum(c.isdigit() for c in stem)
        if digits / len(stem) >= 0.5:
            return True
    return False


def improved_filename(url: str, page_title: str | None, content_type: str | None = None) -> str:
    """The ugly-name fixer (F1.8): keep good URL names, replace junk with the
    page title, keeping (or guessing) the file extension."""
    from_url = filename_from_url(url)
    if not page_title or not is_ugly_name(from_url):
        return from_url
    extension = Path(from_url).suffix
    if not extension and content_type:
        extension = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ""
    return sanitize_filename(page_title.strip() + extension)


def apply_rename_rules(name: str, rules: Sequence[tuple[str, str]]) -> str:
    """User rename rules: literal find -> replace, applied in order to the
    stem only (the extension never changes). The result is re-sanitized, so a
    rule can't smuggle in path separators or an empty name."""
    if not rules:
        return name
    root, ext = os.path.splitext(name)
    for find, replace in rules:
        if find:
            root = root.replace(find, replace)
    return sanitize_filename((root or FALLBACK_NAME) + ext)


def fsync_before_rename(path: Path) -> None:
    """Flush a finished file to disk before renaming it into place, so a crash
    or power loss can't leave a correctly-named file with unwritten tail bytes.

    Opened read/write ("r+b"), never read-only: Windows' FlushFileBuffers -
    which os.fsync calls - rejects a read-only handle. That exact mistake once
    left every finished stream stuck at "Downloading": the raised error
    skipped the completed-status write. Best-effort by design - the writers
    already flushed on close, so if the platform still refuses, log and let
    the rename proceed rather than fail a download whose bytes are on disk."""
    try:
        with open(path, "r+b") as handle:
            os.fsync(handle.fileno())
    except OSError as exc:
        logging.getLogger(__name__).warning("could not fsync %s before rename: %s", path, exc)


def unique_path(path: Path) -> Path:
    """Never overwrite silently: 'file.bin' -> 'file (1).bin' -> ... (S4)."""
    if not path.exists():
        return path
    counter = 1
    while True:
        candidate = path.with_name(f"{path.stem} ({counter}){path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def clean_page_title(title: str | None) -> str | None:
    """A page title minus the site-name boilerplate ("Video - YouTube" ->
    "Video"); None when nothing meaningful is left (e.g. just "YouTube")."""
    if not title:
        return None
    cleaned = title.strip()
    # Browsers prepend the unread-notification badge to the tab title, and the
    # extension hands us the tab title verbatim: "(93) Video - YouTube".
    cleaned = re.sub(r"^\(\d+\)\s*", "", cleaned)
    for suffix in (
        " - YouTube",
        " - YouTube Music",
        " | Vimeo",
        " on Vimeo",
        " / X",
        " | Instagram",
    ):
        if cleaned.lower().endswith(suffix.lower()):
            cleaned = cleaned[: -len(suffix)].strip()
    junk = {"youtube", "youtube music", "vimeo", "instagram", "x", "twitter", ""}
    return None if cleaned.lower() in junk else cleaned
