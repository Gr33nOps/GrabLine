"""GIF tools (F2.3): turn a downloaded video (or a slice of it) into a GIF.

FFmpeg's two-pass palette trick in a single command: generate an optimized
256-color palette from the clip, then dither the frames against it. The
difference against a naive ``-f gif`` conversion is night and day.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.core import naming, proc
from app.core.errors import DownloadError

DEFAULT_FPS = 12
DEFAULT_WIDTH = 480


def gif_filter(fps: int, width: int) -> str:
    return (
        f"fps={fps},scale={width}:-2:flags=lanczos,"
        "split[a][b];[a]palettegen=stats_mode=diff[p];"
        "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"
    )


def make_gif(
    ffmpeg_path: str,
    source: Path,
    *,
    dest: Path | None = None,
    start: float | None = None,
    end: float | None = None,
    fps: int = DEFAULT_FPS,
    width: int = DEFAULT_WIDTH,
) -> Path:
    """Convert ``source`` (optionally just start..end seconds) to a GIF.

    Returns the written path (``source`` with a .gif suffix by default,
    never overwriting). Raises DownloadError with FFmpeg's complaint.
    """
    if not source.exists():
        raise DownloadError(f"file not found: {source}")
    if end is not None and end <= (start or 0.0):
        raise DownloadError("the end timestamp must be after the start")
    target = naming.unique_path(dest if dest is not None else source.with_suffix(".gif"))
    command = [ffmpeg_path, "-y", "-nostdin", "-loglevel", "error"]
    if start is not None:
        command += ["-ss", f"{start:.3f}"]
    if end is not None:
        command += ["-t", f"{end - (start or 0.0):.3f}"]
    command += [
        "-i",
        str(source),
        "-vf",
        gif_filter(fps, width),
        "-f",
        "gif",
        str(target),
    ]
    result = subprocess.run(  # argument list only - no shell (S1)
        command, capture_output=True, text=True, **proc.hidden()
    )
    if result.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        lines = result.stderr.strip().splitlines()
        detail = f" ({lines[-1]})" if lines else ""
        raise DownloadError(f"FFmpeg could not convert this video to a GIF{detail}")
    return target
