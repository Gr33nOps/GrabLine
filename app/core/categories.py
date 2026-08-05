"""Auto-organize downloads into category folders by file type (F0.6)."""

from __future__ import annotations

from pathlib import Path

_CATEGORY_EXTENSIONS = {
    "Video": {
        "mp4",
        "mkv",
        "webm",
        "mov",
        "avi",
        "flv",
        "m4v",
        "ts",
        "mpg",
        "mpeg",
        "wmv",
        "3gp",
    },
    "Music": {"mp3", "m4a", "aac", "flac", "wav", "ogg", "opus", "wma", "mka", "aiff"},
    "Images": {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "avif", "tiff", "heic"},
    "Documents": {
        "pdf",
        "doc",
        "docx",
        "xls",
        "xlsx",
        "ppt",
        "pptx",
        "txt",
        "epub",
        "odt",
        "csv",
        "md",
    },
    "Archives": {"zip", "rar", "7z", "tar", "gz", "xz", "bz2", "zst", "iso"},
    # .dmg is an installer image, not an archive to unpack - it sorts here.
    "Programs": {
        "exe",
        "msi",
        "msix",
        "apk",
        "xapk",
        "deb",
        "rpm",
        "appimage",
        "pkg",
        "dmg",
        "jar",
    },
    "Games": {
        "rom",
        "gb",
        "gbc",
        "gba",
        "nds",
        "3ds",
        "cia",
        "nes",
        "sfc",
        "smc",
        "n64",
        "z64",
        "gcm",
        "wbfs",
        "xci",
        "nsp",
        "wad",
    },
    # The torrent engine comes later; the category folder is ready now.
    "Torrents": {"torrent"},
}

_EXTENSION_TO_CATEGORY = {
    extension: category
    for category, extensions in _CATEGORY_EXTENSIONS.items()
    for extension in extensions
}


def category_for(filename: str) -> str | None:
    """The category folder for a filename, or None for uncategorized types."""
    extension = Path(filename).suffix.lstrip(".").lower()
    return _EXTENSION_TO_CATEGORY.get(extension)


def dest_dir_for(base: Path, filename: str, *, enabled: bool) -> Path:
    """Where a file belongs: base/<Category> when enabled, plain base otherwise."""
    if not enabled:
        return base
    category = category_for(filename)
    return base / category if category else base
