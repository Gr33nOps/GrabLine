"""Small display formatting helpers shared across UI widgets."""

from __future__ import annotations

import html


def plain_tooltip(text: str | None) -> str:
    """``text`` as a tooltip that Qt shows literally. Qt tooltips switch to rich
    text when the string merely looks like markup, and rich text can load
    remote images, so any text a phone or the network supplied goes through
    here: escaped inside one rich-text paragraph that keeps its line breaks."""
    if not text:
        return ""
    return "<p style='white-space:pre-wrap'>" + html.escape(text, quote=True) + "</p>"


def human_bytes(count: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if count < 1024 or unit == "TB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024
    return f"{count:.1f} TB"  # pragma: no cover - unreachable


def duration_text(seconds: float | None) -> str:
    if seconds is None:
        return ""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
