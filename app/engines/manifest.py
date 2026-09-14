"""HLS manifest parsing (F2.1): master-playlist variants and durations.

FFmpeg can eat a master playlist whole, but then *it* picks the variant and
the user gets no say. Parsing the master ourselves gives the quality picker
for raw streams, catches variants whose audio lives in a separate rendition
(``#EXT-X-MEDIA:TYPE=AUDIO``), and lets us sum ``#EXTINF`` durations for
honest progress. Only HLS is parsed here - DASH manifests go straight to
FFmpeg untouched.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from urllib.parse import urljoin

_ATTRIBUTE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
_EXTINF = re.compile(r"#EXTINF:([0-9.]+)")


@dataclass(frozen=True)
class HlsVariant:
    """One quality choice from a master playlist."""

    url: str
    bandwidth: int | None = None
    width: int | None = None
    height: int | None = None
    audio_url: str | None = None  # separate audio rendition, when referenced

    @property
    def label(self) -> str:
        if self.height:
            return f"{self.height}p"
        if self.bandwidth:
            return f"{self.bandwidth / 1_000_000:.1f} Mbps"
        return "Stream"

    @property
    def description(self) -> str:
        parts = [self.label]
        if self.height and self.bandwidth:
            parts.append(f"{self.bandwidth / 1_000_000:.1f} Mbps")
        return ", ".join(parts)


def parse_attributes(line: str) -> dict[str, str]:
    """The attribute list of an EXT-X tag; quoted values may contain commas."""
    return {key: value.strip('"') for key, value in _ATTRIBUTE.findall(line)}


def is_master_playlist(text: str) -> bool:
    return "#EXTM3U" in text and "#EXT-X-STREAM-INF" in text


def parse_master_playlist(text: str, base_url: str) -> tuple[HlsVariant, ...]:
    """Variants from a master playlist, best quality first. Empty if not one."""
    if not is_master_playlist(text):
        return ()

    # Audio renditions: GROUP-ID -> URI (a DEFAULT=YES rendition wins).
    audio_groups: dict[str, str] = {}
    audio_defaults: set[str] = set()
    for line in text.splitlines():
        if not line.startswith("#EXT-X-MEDIA:"):
            continue
        attrs = parse_attributes(line)
        if attrs.get("TYPE") != "AUDIO" or not attrs.get("URI"):
            continue
        group = attrs.get("GROUP-ID", "")
        is_default = attrs.get("DEFAULT") == "YES"
        if group not in audio_groups or (is_default and group not in audio_defaults):
            audio_groups[group] = urljoin(base_url, attrs["URI"])
            if is_default:
                audio_defaults.add(group)

    variants: list[HlsVariant] = []
    pending: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending = parse_attributes(line)
            continue
        if pending is None or not line or line.startswith("#"):
            continue
        width = height = None
        resolution = pending.get("RESOLUTION", "")
        if "x" in resolution.lower():
            try:
                w, h = resolution.lower().split("x", 1)
                width, height = int(w), int(h)
            except ValueError:
                pass
        bandwidth = None
        with contextlib.suppress(ValueError):
            bandwidth = int(pending.get("BANDWIDTH", ""))
        variants.append(
            HlsVariant(
                url=urljoin(base_url, line),
                bandwidth=bandwidth,
                width=width,
                height=height,
                audio_url=audio_groups.get(pending.get("AUDIO", "")),
            )
        )
        pending = None

    # Best first; among equal heights keep the highest bandwidth only.
    variants.sort(key=lambda v: (v.height or 0, v.bandwidth or 0), reverse=True)
    seen_heights: set[int] = set()
    unique: list[HlsVariant] = []
    for variant in variants:
        if variant.height is not None:
            if variant.height in seen_heights:
                continue
            seen_heights.add(variant.height)
        unique.append(variant)
    return tuple(unique)


def playlist_duration(text: str) -> float | None:
    """Sum of #EXTINF segment durations in a media playlist, if any."""
    durations = [float(value) for value in _EXTINF.findall(text)]
    return sum(durations) if durations else None
