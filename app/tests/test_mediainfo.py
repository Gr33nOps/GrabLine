"""The ffprobe media summary used by the detail panel's Media tab.

The JSON parsing is tested directly with canned ffprobe output so it needs no
binary; the public entry point is checked only for its guard rails (a missing
file or absent ffmpeg return None, never raise).
"""

from __future__ import annotations

from pathlib import Path

from app.core.mediainfo import _parse_fraction, _summarize, read_media_info


def test_parse_fraction():
    assert _parse_fraction("30/1") == 30.0
    assert _parse_fraction("30000/1001") == 29.97  # NTSC
    assert _parse_fraction("25") == 25.0
    assert _parse_fraction("0/0") is None  # some containers emit this
    assert _parse_fraction(None) is None
    assert _parse_fraction("junk") is None


def test_summarize_video_and_audio():
    data: dict[str, object] = {
        "streams": [
            {
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
                "codec_name": "h264",
                "avg_frame_rate": "30000/1001",
            },
            {"codec_type": "audio", "codec_name": "aac"},
        ],
        "format": {"duration": "12.5", "format_name": "mov,mp4,m4a,3gp"},
    }
    summary = _summarize(data)
    assert summary is not None
    assert (summary.width, summary.height) == (1920, 1080)
    assert summary.vcodec == "H264" and summary.acodec == "AAC"
    assert summary.fps == 29.97 and summary.duration == 12.5
    assert summary.container == "MOV"  # first of the format family
    assert summary.has_video


def test_summarize_audio_only():
    data: dict[str, object] = {
        "streams": [{"codec_type": "audio", "codec_name": "mp3"}],
        "format": {"duration": "180", "format_name": "mp3"},
    }
    summary = _summarize(data)
    assert summary is not None
    assert not summary.has_video
    assert summary.acodec == "MP3" and summary.width is None


def test_summarize_non_media_returns_none():
    assert _summarize({"streams": [], "format": {}}) is None


def test_read_media_info_guards(tmp_path: Path):
    # Missing file, and no ffmpeg at all: both None, never an exception.
    assert read_media_info(tmp_path / "nope.mp4", "/usr/bin/ffmpeg") is None
    real = tmp_path / "x.bin"
    real.write_bytes(b"not media")
    assert read_media_info(real, None) is None
