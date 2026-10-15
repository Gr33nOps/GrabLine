"""Filename derivation and sanitization (S4: filesystem safety)."""

from __future__ import annotations

import logging
import mimetypes
import os
import posixpath
import re
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

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


_ENDPOINT_SUFFIXES = {".php", ".asp", ".aspx", ".ashx", ".jsp", ".do"}


def filename_from_url(url: str) -> str:
    parts = urlsplit(url)
    name = unquote(posixpath.basename(parts.path))
    # LMS/download endpoints often carry the real name in an explicit query
    # parameter. Do not replace a real path filename with arbitrary query data.
    if not Path(name).suffix or Path(name).suffix.lower() in _ENDPOINT_SUFFIXES:
        query = parse_qs(parts.query)
        for key in ("filename", "file"):
            for value in query.get(key, []):
                candidate = posixpath.basename(value.replace("\\", "/"))
                if candidate and Path(candidate).suffix:
                    return sanitize_filename(candidate)
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


# Canonical suffixes for common downloads missing from Python's built-in MIME
# database, plus ambiguous defaults (e.g. MP3 vs MP2). This is not a download
# whitelist: unknown types and all explicit filename suffixes remain supported.
_MIME_EXTENSIONS = {
    "application/msword": ".doc",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template": ".dotx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.template": ".xltx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.presentationml.slideshow": ".ppsx",
    "application/vnd.openxmlformats-officedocument.presentationml.template": ".potx",
    "application/vnd.ms-word.document.macroenabled.12": ".docm",
    "application/vnd.ms-word.template.macroenabled.12": ".dotm",
    "application/vnd.ms-excel.sheet.macroenabled.12": ".xlsm",
    "application/vnd.ms-excel.sheet.binary.macroenabled.12": ".xlsb",
    "application/vnd.ms-excel.template.macroenabled.12": ".xltm",
    "application/vnd.ms-powerpoint.presentation.macroenabled.12": ".pptm",
    "application/vnd.ms-powerpoint.slideshow.macroenabled.12": ".ppsm",
    "application/vnd.ms-powerpoint.template.macroenabled.12": ".potm",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.presentation": ".odp",
    "application/epub+zip": ".epub",
    "application/rtf": ".rtf",
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
    "application/vnd.rar": ".rar",
    "application/x-rar-compressed": ".rar",
    "application/x-7z-compressed": ".7z",
    "application/gzip": ".gz",
    "application/x-gzip": ".gz",
    "application/x-bzip2": ".bz2",
    "application/x-xz": ".xz",
    "application/zstd": ".zst",
    "application/x-tar": ".tar",
    "application/x-iso9660-image": ".iso",
    "application/x-msdownload": ".exe",
    "application/vnd.microsoft.portable-executable": ".exe",
    "application/x-msi": ".msi",
    "application/vnd.android.package-archive": ".apk",
    "application/vnd.debian.binary-package": ".deb",
    "application/x-debian-package": ".deb",
    "application/x-rpm": ".rpm",
    "application/x-apple-diskimage": ".dmg",
    "application/java-archive": ".jar",
    "application/x-bittorrent": ".torrent",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "video/x-matroska": ".mkv",
    "video/webm": ".webm",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "image/heic": ".heic",
    "text/markdown": ".md",
}
# Some MIME types cover several formats. Retain an extension-only name's
# more specific hint instead of turning a DLL into dll.exe or PPS into pps.ppt.
_MIME_SUFFIX_ALIASES = {
    "application/x-msdownload": {"exe", "dll", "ocx"},
    "application/vnd.microsoft.portable-executable": {"exe", "dll", "sys"},
    "application/vnd.ms-powerpoint": {"ppt", "pps", "pot"},
    "application/vnd.ms-excel": {"xls", "xlt"},
    "image/jpeg": {"jpg", "jpeg", "jpe"},
}
_BUILTIN_MIMES = mimetypes.MimeTypes(filenames=())
_GENERIC_TYPES = {
    "",
    "application/octet-stream",
    "binary/octet-stream",
    "application/force-download",
}
_HTML_COMPATIBLE_SUFFIXES = {".html", ".htm", ".xhtml", ".txt", ".xml", ".svg"}


def extension_for_type(content_type: str | None) -> str:
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime in _GENERIC_TYPES:
        return ""  # a generic binary response tells us nothing about its format
    return _MIME_EXTENSIONS.get(mime) or _BUILTIN_MIMES.guess_extension(mime) or ""


def filename_with_type(name: str, content_type: str | None) -> str:
    """Restore missing suffixes without changing explicit or custom extensions."""
    name = sanitize_filename(name)
    suffix = extension_for_type(content_type)
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if name.lower() in _MIME_SUFFIX_ALIASES.get(mime, set()):
        suffix = "." + name.lower()
    if not suffix or Path(name).suffix:
        return name
    stem = FALLBACK_NAME if name.lower() == suffix[1:] else name
    return sanitize_filename(stem + suffix)


def resolve_download_name(
    name: str,
    *,
    url: str,
    final_url: str,
    server_filename: str | None,
    content_type: str | None,
) -> str:
    """Use response metadata, preserving names the user chose explicitly."""
    if server_filename:
        return filename_with_type(server_filename, content_type)
    if name == filename_from_url(url) and (
        not Path(name).suffix or Path(name).suffix.lower() in _ENDPOINT_SUFFIXES
    ):
        redirected = filename_from_url(final_url)
        if Path(redirected).suffix and Path(redirected).suffix.lower() not in _ENDPOINT_SUFFIXES:
            name = redirected
        suffix = extension_for_type(content_type)
        if (
            Path(name).suffix.lower() in _ENDPOINT_SUFFIXES
            and suffix
            and expects_binary_file("download" + suffix)
        ):
            # download.php is the endpoint, not the type of the returned PDF.
            name = Path(name).stem
    return filename_with_type(name, content_type)


def expects_binary_file(name: str) -> bool:
    """Whether HTML would clearly be an error page for the named download."""
    from app.core.categories import category_for

    suffix = Path(name).suffix.lower() or "." + name.lower()
    if suffix in _HTML_COMPATIBLE_SUFFIXES:
        return False
    probe_name = "download" + suffix
    if category_for(probe_name) in {
        "Archives",
        "Programs",
        "Games",
        "Torrents",
        "Music",
        "Video",
        "Images",
    }:
        return True
    mime = next((mime for mime, ext in _MIME_EXTENSIONS.items() if ext == suffix), None)
    mime = mime or _BUILTIN_MIMES.guess_type(probe_name)[0]
    return bool(mime and mime.startswith(("application/", "audio/", "video/", "image/")))


def improved_filename(url: str, page_title: str | None, content_type: str | None = None) -> str:
    """The ugly-name fixer (F1.8): keep good URL names, replace junk with the
    page title, keeping (or guessing) the file extension."""
    from_url = filename_from_url(url)
    if not page_title or not is_ugly_name(from_url):
        return filename_with_type(from_url, content_type)
    extension = Path(from_url).suffix
    if not extension and content_type:
        extension = extension_for_type(content_type)
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
