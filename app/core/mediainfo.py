"""A one-shot ffprobe read for the detail panel's Media tab.

The download engines don't persist a file's resolution/codecs, so the panel
reads them on demand from the finished file with ffprobe (which ships beside
ffmpeg - see app.core.ffmpeg). Everything is best-effort: a missing binary, a
non-media file, or malformed output all return None so the caller just hides
the Media tab rather than showing blanks.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

from app.core import proc

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MediaSummary:
    """The handful of stream facts the Media tab shows. Any field may be None
    when ffprobe didn't report it (e.g. an audio-only file has no width)."""

    width: int | None = None
    height: int | None = None
    duration: float | None = None
    fps: float | None = None
    vcodec: str | None = None
    acodec: str | None = None
    container: str | None = None

    @property
    def has_video(self) -> bool:
        return self.vcodec is not None or self.width is not None


def ffprobe_for(ffmpeg: str) -> str | None:
    """The ffprobe binary that sits next to a resolved ffmpeg path, falling back
    to one on PATH. None when neither exists."""
    ffmpeg_path = Path(ffmpeg)
    suffix = ffmpeg_path.suffix  # ".exe" on Windows, "" elsewhere
    sibling = ffmpeg_path.with_name(f"ffprobe{suffix}")
    if sibling.is_file():
        return str(sibling)
    import shutil

    return shutil.which("ffprobe")


def _parse_fraction(value: str | None) -> float | None:
    """ffprobe reports frame rates as "30000/1001" or "30/1"; turn that into a
    float, guarding against the "0/0" some containers emit."""
    if not value:
        return None
    try:
        numerator, _, denominator = value.partition("/")
        den = float(denominator) if denominator else 1.0
        if den == 0:
            return None
        rate = float(numerator) / den
    except ValueError:
        return None
    return round(rate, 2) if rate > 0 else None


def _to_int(value: object) -> int | None:
    if not isinstance(value, (int, str)):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _to_float(value: object) -> float | None:
    if not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)
    except ValueError:
        return None
    return result if result > 0 else None


def read_media_info(path: Path, ffmpeg: str | None) -> MediaSummary | None:
    """Probe ``path`` with the ffprobe beside ``ffmpeg``. None if ffprobe is
    unavailable, the file is missing, it isn't media, or ffprobe fails."""
    if not ffmpeg or not path.is_file():
        return None
    ffprobe = ffprobe_for(ffmpeg)
    if ffprobe is None:
        return None
    command = [
        ffprobe,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=20, **proc.hidden()
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.info("ffprobe could not run on %s (%s)", path.name, exc)
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    summary = _summarize(data)
    if summary is None:
        return None
    # The file's own extension is the container users expect ("MP4"), not
    # ffprobe's format family (an .mp4's format_name starts "mov,mp4,...").
    extension = path.suffix.lstrip(".").upper()
    if extension:
        summary = replace(summary, container=extension)
    return summary


def _summarize(data: dict[str, object]) -> MediaSummary | None:
    streams = data.get("streams")
    fmt = data.get("format")
    streams = streams if isinstance(streams, list) else []
    fmt = fmt if isinstance(fmt, dict) else {}

    video = next(
        (s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"), None
    )
    audio = next(
        (s for s in streams if isinstance(s, dict) and s.get("codec_type") == "audio"), None
    )
    if video is None and audio is None:
        return None  # not a media file we can describe

    width = height = fps = vcodec = None
    if video is not None:
        width = _to_int(video.get("width"))
        height = _to_int(video.get("height"))
        fps = _parse_fraction(video.get("avg_frame_rate")) or _parse_fraction(
            video.get("r_frame_rate")
        )
        codec = video.get("codec_name")
        vcodec = str(codec).upper() if codec else None

    acodec = None
    if audio is not None:
        codec = audio.get("codec_name")
        acodec = str(codec).upper() if codec else None

    duration = _to_float(fmt.get("duration"))
    if duration is None and video is not None:
        duration = _to_float(video.get("duration"))

    container = None
    format_name = fmt.get("format_name")
    if format_name:
        # "mov,mp4,m4a,3gp,3g2,mj2" -> "MOV" (the first, the most specific).
        container = str(format_name).split(",")[0].upper()

    return MediaSummary(
        width=width,
        height=height,
        duration=duration,
        fps=fps,
        vcodec=vcodec,
        acodec=acodec,
        container=container,
    )
