"""Tintable monochrome SVG icons. One path set, rendered to a QIcon in any
color, so the same glyph works in light and dark and as an active/inactive nav
item. Paths are 16x16, 1.5 stroke, rounded: the set from the redesign.
"""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

#: name -> one or more path `d` strings (pipe-separated).
_PATHS: dict[str, str] = {
    "download": "M8 2v8|M5 7l3 3 3-3|M3 13h10",
    "torrent": "M8 2a6 6 0 100 12A6 6 0 008 2z|M5 8l3 3 3-3|M8 5v6",
    "cloud": "M4 11a3 3 0 010-6 5 5 0 019.9 1.5A3 3 0 0112 12H4z",
    "pause": "M5 3v10|M11 3v10",
    "resume": "M5 3l9 5-9 5V3z",
    "cancel": "M4 4l8 8|M12 4l-8 8",
    "trash": "M3 4h10|M5 4V3a1 1 0 011-1h4a1 1 0 011 1v1|M6 7v5M10 7v5|M4 4l1 9h6l1-9",
    "folder": "M2 4a1 1 0 011-1h3.5L8 4.5H13a1 1 0 011 1V12a1 1 0 01-1 1H3a1 1 0 01-1-1V4z",
    "settings": (
        "M8 10a2 2 0 100-4 2 2 0 000 4z|M8 2v1M8 13v1M2 8H1M15 8h-1"
        "M3.5 3.5l.7.7M11.8 11.8l.7.7M3.5 12.5l.7-.7M11.8 4.2l.7-.7"
    ),
    "dashboard": "M2 9l4-4 3 3 5-5|M2 13h12",
    "queue": "M2 4h12|M2 8h8|M2 12h10",
    "minimize": "M4 8h8",
    "maximize": "M4.5 4.5h7v7h-7z",
    "restore": "M6 6V4.5h5.5V10H10|M4.5 6H10v5.5H4.5z",
    "search": "M6.5 11a4.5 4.5 0 100-9 4.5 4.5 0 000 9z|M10.5 10.5L14 14",
    "add": "M8 3v10|M3 8h10",
    "moon": "M12 3a6 6 0 000 10 6 6 0 01-6-6 6 6 0 016-10z",
    "sun": (
        "M8 5a3 3 0 100 6 3 3 0 000-6z|M8 2v1M8 13v1M3 8H2M14 8h-1"
        "M4.2 4.2l-.7-.7M12.5 12.5l-.7-.7M4.2 11.8l-.7.7M12.5 3.5l-.7.7"
    ),
    "copy": (
        "M5 4H3a1 1 0 00-1 1v8a1 1 0 001 1h8a1 1 0 001-1v-2|"
        "M6 2h6a1 1 0 011 1v8a1 1 0 01-1 1H6a1 1 0 01-1-1V3a1 1 0 011-1z"
    ),
    "vpn": "M8 2L3 4v5c0 3 2.5 5 5 6 2.5-1 5-3 5-6V4L8 2z",
    # A page with a folded corner and two text lines - "this download has
    # tags/notes". Stroked, not filled, so it reads as an icon, not an emoji.
    "note": (
        "M4 2.5h5l3 3v8a.5.5 0 01-.5.5h-7a.5.5 0 01-.5-.5V3a.5.5 0 01.5-.5z"
        "|M9 2.5V6h3|M6 8.5h4|M6 10.5h4"
    ),
    "shield": "M8 2L3 4v5c0 3 2.5 5 5 6 2.5-1 5-3 5-6V4L8 2z|M6 8l1.5 1.5L10.5 6",
    # A box with an arrow leaving its top-right corner - "open this file".
    "open": "M12 9v3a1 1 0 01-1 1H4a1 1 0 01-1-1V5a1 1 0 011-1h3|M9 3h4v4|M13 3L7 9",
    # A pencil across a line - "rename".
    "rename": "M3 11l6-6 2 2-6 6H3v-2z|M9 4l2 2|M3 14h10",
    "inspect": "M6.5 11a4.5 4.5 0 100-9 4.5 4.5 0 000 9z|M10.5 10.5L14 14|M6.5 4.5v4M4.5 6.5h4",
    "chevron-right": "M6 4l4 4-4 4",
    "check": "M3 8.5L6.5 12 13 4",
    "globe": (
        "M8 2a6 6 0 100 12A6 6 0 008 2z|M2 8h12"
        "|M8 2c-1.8 1.6-1.8 10.4 0 12M8 2c1.8 1.6 1.8 10.4 0 12"
    ),
    "import": "M8 2v7|M5 6l3 3 3-3|M2 10v2a1 1 0 001 1h10a1 1 0 001-1v-2",
    "export": "M8 9V2|M5 5l3-3 3 3|M2 10v2a1 1 0 001 1h10a1 1 0 001-1v-2",
    "package": "M8 2l6 3-6 3-6-3z|M2 5v6l6 3 6-3V5|M8 8v6",
    "duplicate": (
        "M6 2h6a1 1 0 011 1v6a1 1 0 01-1 1H6a1 1 0 01-1-1V3a1 1 0 011-1z|M3 5v7a1 1 0 001 1h6"
    ),
    # type icons
    "t-video": "M2 5a1 1 0 011-1h7a1 1 0 011 1v6a1 1 0 01-1 1H3a1 1 0 01-1-1V5z|M11 7l3-2v6l-3-2",
    "t-audio": "M9 4v8|M6 6v4|M3 7v2|M12 5v6",
    "t-image": (
        "M2 4a1 1 0 011-1h10a1 1 0 011 1v8a1 1 0 01-1 1H3a1 1 0 01-1-1V4z|M2 10l3-3 3 3 2-2 3 4"
    ),
    "t-document": (
        "M4 2h6l4 4v8a1 1 0 01-1 1H4a1 1 0 01-1-1V3a1 1 0 011-1z|M10 2v4h4|M5 8h6|M5 11h4"
    ),
    "t-archive": "M5 2h6l3 3v9a1 1 0 01-1 1H4a1 1 0 01-1-1V3a1 1 0 011-1z|M7 2v4|M9 2v4|M7 6h2",
    "t-torrent": "M8 2a6 6 0 100 12A6 6 0 008 2z|M5 8l3 3 3-3|M8 5v6",
    "t-program": (
        "M2 4a1 1 0 011-1h10a1 1 0 011 1v8a1 1 0 01-1 1H3a1 1 0 01-1-1V4z|M5 8l2-2 2 2|M9 8l2 2"
    ),
    "t-game": (
        "M4 6h8a2 2 0 012 2v2a2 2 0 01-2 2H4a2 2 0 01-2-2V8a2 2 0 012-2z"
        "|M5 8v2M4 9h2|M11 9h.01M12.5 9.5h.01"
    ),
    "t-cloud": "M4 10a3 3 0 010-6 5 5 0 019.9 1.5A3 3 0 0112 11H4z|M8 11v3|M6 12l2 2 2-2",
}

_TEMPLATE = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16" '
    'fill="none" stroke="{color}" stroke-width="1.5" stroke-linecap="round" '
    'stroke-linejoin="round">{paths}</svg>'
)


@lru_cache(maxsize=512)
def svg_icon(name: str, color: str) -> QIcon:
    """A QIcon of ``name`` stroked in ``color`` (hex). Cached per (name,color)."""
    d = _PATHS.get(name, _PATHS["download"])
    paths = "".join(f'<path d="{p}"/>' for p in d.split("|"))
    svg = _TEMPLATE.format(color=color, paths=paths)
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)


def type_icon_name(kind_or_ext: str) -> str:
    """Map a job kind or a filename/extension to a type-icon name."""
    ext = kind_or_ext.lower().rsplit(".", 1)[-1]
    mapping = {
        "video": "t-video",
        "mp4": "t-video",
        "mkv": "t-video",
        "webm": "t-video",
        "mov": "t-video",
        "avi": "t-video",
        "m4v": "t-video",
        "audio": "t-audio",
        "mp3": "t-audio",
        "m4a": "t-audio",
        "flac": "t-audio",
        "wav": "t-audio",
        "ogg": "t-audio",
        "opus": "t-audio",
        "m4b": "t-audio",
        "image": "t-image",
        "jpg": "t-image",
        "jpeg": "t-image",
        "png": "t-image",
        "gif": "t-image",
        "webp": "t-image",
        "document": "t-document",
        "pdf": "t-document",
        "doc": "t-document",
        "docx": "t-document",
        "txt": "t-document",
        "epub": "t-document",
        "archive": "t-archive",
        "zip": "t-archive",
        "rar": "t-archive",
        "7z": "t-archive",
        "tar": "t-archive",
        "gz": "t-archive",
        "xz": "t-archive",
        "program": "t-program",
        "exe": "t-program",
        "msi": "t-program",
        "dmg": "t-program",
        "deb": "t-program",
        "appimage": "t-program",
        "iso": "t-program",
        "apk": "t-program",
        "game": "t-game",
        "torrent": "t-torrent",
        "cloud": "t-cloud",
    }
    return mapping.get(ext, "t-document")
