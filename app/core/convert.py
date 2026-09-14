"""File conversion (Convert to… in the row menu): remux/re-encode a finished
download into another container or format with FFmpeg.

Deliberately simple: one input, one output, FFmpeg's own defaults for the
target container (libx264/aac for .mp4, VP9/Opus for .webm, and so on). The
output lands next to the source with a versioned name - nothing is ever
overwritten. Runs hidden (no console window) and off the UI thread.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.core import naming, proc
from app.core.errors import DownloadError

#: Offered targets per section, in menu order.
VIDEO_TARGETS = ("mp4", "mkv", "webm", "mov", "avi")
AUDIO_TARGETS = ("mp3", "m4a", "flac", "wav", "opus")
IMAGE_TARGETS = ("jpg", "png", "webp", "avif")

#: What a source file can become, by its own suffix.
VIDEO_SOURCES = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts", ".flv", ".wmv"}
AUDIO_SOURCES = {".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".aac", ".wma", ".m4b"}
IMAGE_SOURCES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif", ".tiff"}


def targets_for(path: Path) -> dict[str, tuple[str, ...]]:
    """{"Video": (...), "Audio": (...), "Image": (...)} sections that make
    sense for this file - empty when it isn't convertible media."""
    suffix = path.suffix.lower()
    if suffix in VIDEO_SOURCES:
        # A video can stay video or drop to an audio track.
        return {"Video": VIDEO_TARGETS, "Audio": AUDIO_TARGETS}
    if suffix in AUDIO_SOURCES:
        return {"Audio": AUDIO_TARGETS}
    if suffix in IMAGE_SOURCES:
        return {"Image": IMAGE_TARGETS}
    return {}


def output_path(source: Path, target_format: str) -> Path:
    """The versioned destination: video.mp4 -> video.mkv (or video (1).mkv)."""
    wanted = source.with_suffix(f".{target_format}")
    return naming.unique_path(wanted)


def convert(ffmpeg_path: str, source: Path, target_format: str) -> Path:
    """Convert ``source`` to ``target_format``; returns the new file's path."""
    target_format = target_format.lower().lstrip(".")
    target = output_path(source, target_format)
    command = [ffmpeg_path, "-hide_banner", "-i", str(source)]
    if target_format in AUDIO_TARGETS and source.suffix.lower() in VIDEO_SOURCES:
        command.append("-vn")  # video -> audio: keep only the sound
    if target_format in ("jpg", "webp", "avif"):
        command += ["-frames:v", "1"]  # a single output image
    command.append(str(target))
    result = subprocess.run(  # argument list only - no shell (S1)
        command, capture_output=True, text=True, **proc.hidden()
    )
    if result.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        lines = result.stderr.strip().splitlines()
        detail = f" ({lines[-1]})" if lines else ""
        raise DownloadError(f"FFmpeg could not convert to {target_format.upper()}{detail}")
    return target
