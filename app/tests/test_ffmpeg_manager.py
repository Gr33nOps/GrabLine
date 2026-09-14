from __future__ import annotations

import hashlib
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from app.core.errors import DownloadError
from app.core.ffmpeg import ensure_ffmpeg, find_ffmpeg, platform_key
from app.core.ffmpeg_pins import PINS, PinnedArchive
from app.core.settings import Settings
from app.db.database import Database
from app.tests.media_server import MediaServer

FAKE_BINARY = b"#!/bin/sh\necho 'ffmpeg version fake-1.0'\n"


def _tar_xz(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz") as bundle:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o755
            bundle.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, content in members.items():
            bundle.writestr(name, content)
    return buffer.getvalue()


def _pins_for(
    url: str, archive: bytes, archive_format: str
) -> dict[str, tuple[PinnedArchive, ...]]:
    pinned = PinnedArchive(
        url=url, sha256=hashlib.sha256(archive).hexdigest(), format=archive_format
    )
    return {platform_key(): (pinned,)}


def test_install_from_tar_extracts_only_binaries(server: MediaServer, tmp_path: Path):
    archive = _tar_xz(
        {
            "ffmpeg-test/bin/ffmpeg": FAKE_BINARY,
            "ffmpeg-test/bin/ffprobe": FAKE_BINARY,
            "ffmpeg-test/doc/README.txt": b"docs",
        }
    )
    url = server.add("/ff.tar.xz", archive)
    bin_dir = tmp_path / "bin"

    path = ensure_ffmpeg(bin_dir=bin_dir, pins=_pins_for(url, archive, "tar.xz"))

    assert path == bin_dir / "ffmpeg"
    assert (bin_dir / "ffprobe").exists()
    assert not (bin_dir / "README.txt").exists()


def test_install_from_zip(server: MediaServer, tmp_path: Path):
    archive = _zip({"ffmpeg": FAKE_BINARY})
    url = server.add("/ff.zip", archive)
    bin_dir = tmp_path / "bin"
    path = ensure_ffmpeg(bin_dir=bin_dir, pins=_pins_for(url, archive, "zip"))
    assert path.exists()


def test_checksum_mismatch_refuses_install(server: MediaServer, tmp_path: Path):
    archive = _tar_xz({"bin/ffmpeg": FAKE_BINARY})
    url = server.add("/evil.tar.xz", archive)
    pins: dict[str, tuple[PinnedArchive, ...]] = {
        platform_key(): (PinnedArchive(url=url, sha256="0" * 64, format="tar.xz"),)
    }
    bin_dir = tmp_path / "bin"
    with pytest.raises(DownloadError, match="integrity"):
        ensure_ffmpeg(bin_dir=bin_dir, pins=pins)
    assert not (bin_dir / "ffmpeg").exists()


def test_unknown_platform_is_friendly(tmp_path: Path):
    with pytest.raises(DownloadError, match="manually"):
        ensure_ffmpeg(bin_dir=tmp_path, pins={})


def test_real_pins_cover_this_platform():
    """The generated pins file must include every platform we release for."""
    for key in ("linux-x86_64", "windows-x86_64", "darwin-x86_64", "darwin-arm64"):
        assert key in PINS
        for archive in PINS[key]:
            assert archive.url.startswith("https://")
            assert len(archive.sha256) == 64


def test_find_ffmpeg_prefers_explicit_setting(db: Database, tmp_path: Path):
    fake = tmp_path / "my-ffmpeg"
    fake.write_bytes(FAKE_BINARY)
    settings = Settings(db)
    settings.ffmpeg_path = str(fake)
    assert find_ffmpeg(settings) == str(fake)
