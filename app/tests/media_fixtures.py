"""Real media fixtures generated with the system FFmpeg (skipped when absent)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def make_mp4(path: Path, *, seconds: int = 2, with_audio: bool = True) -> bytes:
    """A tiny real MP4 (mpeg4 video + aac audio) for postprocessing tests."""
    assert FFMPEG is not None
    command = [FFMPEG, "-y", "-loglevel", "error"]
    command += ["-f", "lavfi", "-i", f"color=c=blue:s=128x72:d={seconds}:r=10"]
    if with_audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    command += ["-c:v", "mpeg4", "-pix_fmt", "yuv420p"]
    if with_audio:
        command += ["-c:a", "aac", "-shortest"]
    command += [str(path)]
    subprocess.run(command, check=True, capture_output=True)
    return path.read_bytes()


def make_hls(directory: Path, *, seconds: int = 2) -> dict[str, bytes]:
    """A tiny real HLS rendition: index.m3u8 + .ts segments, as name -> bytes."""
    assert FFMPEG is not None
    directory.mkdir(parents=True, exist_ok=True)
    command = [
        FFMPEG,
        "-y",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c=red:s=128x72:d={seconds}:r=10",
        # H.264 like real-world HLS; mpeg4-in-TS loses codec parameters on copy
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-f",
        "hls",
        "-hls_time",
        "1",
        "-hls_list_size",
        "0",
        "-hls_segment_filename",
        str(directory / "seg%03d.ts"),
        str(directory / "index.m3u8"),
    ]
    subprocess.run(command, check=True, capture_output=True)
    return {item.name: item.read_bytes() for item in directory.iterdir()}


def make_hls_audio(directory: Path, *, seconds: int = 2) -> dict[str, bytes]:
    """A tiny audio-only HLS rendition (aac in TS), as name -> bytes."""
    assert FFMPEG is not None
    directory.mkdir(parents=True, exist_ok=True)
    command = [
        FFMPEG,
        "-y",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={seconds}",
        "-c:a",
        "aac",
        "-f",
        "hls",
        "-hls_time",
        "1",
        "-hls_list_size",
        "0",
        "-hls_segment_filename",
        str(directory / "aud%03d.ts"),
        str(directory / "audio.m3u8"),
    ]
    subprocess.run(command, check=True, capture_output=True)
    return {item.name: item.read_bytes() for item in directory.iterdir()}


def probe_streams(path: Path) -> list[str]:
    """codec_type of every stream ("video", "audio"), or [] without ffprobe."""
    if FFPROBE is None:
        return []
    result = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    return result.stdout.split()


def probe_duration(path: Path) -> float | None:
    if FFPROBE is None:
        return None
    result = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None
