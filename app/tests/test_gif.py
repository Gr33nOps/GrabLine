"""GIF conversion (F2.3) against a real FFmpeg fixture."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import DownloadError
from app.core.gif import make_gif
from app.tests.media_fixtures import FFMPEG, make_mp4

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="needs a real ffmpeg")


@pytest.fixture()
def video(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    make_mp4(path, seconds=2)
    return path


def test_whole_video_becomes_a_gif(video: Path):
    assert FFMPEG is not None
    target = make_gif(FFMPEG, video, fps=8, width=64)
    assert target == video.with_suffix(".gif")
    assert target.read_bytes()[:6] in (b"GIF87a", b"GIF89a")


def test_existing_gif_is_never_overwritten(video: Path):
    assert FFMPEG is not None
    sentinel = video.with_suffix(".gif")
    sentinel.write_bytes(b"do not touch")
    target = make_gif(FFMPEG, video, fps=8, width=64)
    assert target != sentinel
    assert sentinel.read_bytes() == b"do not touch"
    assert target.read_bytes()[:6] in (b"GIF87a", b"GIF89a")


def test_trimmed_clip(video: Path):
    assert FFMPEG is not None
    target = make_gif(FFMPEG, video, start=0.5, end=1.0, fps=8, width=64)
    assert target.exists() and target.stat().st_size > 0


def test_bad_range_is_rejected(video: Path):
    assert FFMPEG is not None
    with pytest.raises(DownloadError, match="after the start"):
        make_gif(FFMPEG, video, start=1.0, end=0.5)


def test_missing_file_is_a_friendly_error(tmp_path: Path):
    assert FFMPEG is not None
    with pytest.raises(DownloadError, match="not found"):
        make_gif(FFMPEG, tmp_path / "nope.mp4")


def test_ffmpeg_failure_reports_and_cleans_up(tmp_path: Path):
    assert FFMPEG is not None
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"this is not a video")
    with pytest.raises(DownloadError, match="GIF"):
        make_gif(FFMPEG, junk)
    assert not junk.with_suffix(".gif").exists()
