from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app.core import update
from app.tests.media_server import MediaServer, payload

MB = 1024 * 1024


def test_download_installer_cancel_stops_and_removes_partial(server: MediaServer, tmp_path: Path):
    """Pressing Cancel must actually abort the transfer and leave no half-written
    installer behind - the bug was a Cancel button wired to nothing, so the
    download ran on and opened the installer anyway."""
    url = server.add("/GrabLine-Setup.exe", payload(4 * MB, 5))
    cancel = threading.Event()
    cancel.set()  # already set: the loop aborts on its first chunk check

    with pytest.raises(update.UpdateCancelled):
        update.download_installer(url, str(tmp_path), "GrabLine-Setup.exe", cancel=cancel)

    assert not (tmp_path / "GrabLine-Setup.exe").exists()


def test_download_installer_completes_without_cancel(server: MediaServer, tmp_path: Path):
    """With no cancel it downloads to completion and returns the file path."""
    data = payload(256 * 1024, 9)
    url = server.add("/GrabLine-Setup.exe", data)

    path = update.download_installer(url, str(tmp_path), "GrabLine-Setup.exe")

    assert Path(path).read_bytes() == data


def test_download_installer_parallel_when_size_known(server: MediaServer, tmp_path: Path):
    """Large installers with a known size use several Range connections."""
    data = payload(6 * MB, 11)
    url = server.add("/GrabLine.AppImage", data)
    ticks: list[tuple[int, int | None]] = []

    path = update.download_installer(
        url,
        str(tmp_path),
        "GrabLine.AppImage",
        progress=lambda received, total: ticks.append((received, total)),
        expected_size=len(data),
    )

    assert Path(path).read_bytes() == data
    assert ticks
    assert ticks[-1][0] == len(data)
    assert ticks[-1][1] == len(data)
    # Several ranged GETs, not a single full-body fetch.
    assert server.request_count("/GrabLine.AppImage") >= 2


def test_asset_matches_windows_portable_zip():
    assert update._asset_matches("Grabline-1.29.8-windows-portable.zip", "win32")
    assert update._asset_matches("Grabline-Setup-1.29.8.exe", "win32")
    assert update._asset_rank("Grabline-Setup-1.29.8.exe", "win32") < update._asset_rank(
        "Grabline-1.29.8-windows-portable.zip", "win32"
    )


# ------------------------------------------------ installer integrity check


def test_asset_digest_parses_sha256_only():
    assert update._asset_digest({"digest": "sha256:" + "a" * 64}) == "a" * 64
    # Case-folded to lowercase hex.
    assert update._asset_digest({"digest": "SHA256:" + "A" * 64}) == "a" * 64
    # Non-sha256, malformed, or absent -> None (nothing to verify against).
    assert update._asset_digest({"digest": "md5:" + "a" * 32}) is None
    assert update._asset_digest({"digest": "sha256:nothex"}) is None
    assert update._asset_digest({}) is None


def test_verify_digest_none_is_a_no_op(tmp_path: Path):
    target = tmp_path / "Setup.exe"
    target.write_bytes(b"installer")
    update._verify_digest(target, None)  # must not raise or delete
    assert target.exists()


def test_verify_digest_match_keeps_file(tmp_path: Path):
    import hashlib

    target = tmp_path / "Setup.exe"
    data = b"the real installer bytes"
    target.write_bytes(data)
    update._verify_digest(target, hashlib.sha256(data).hexdigest())
    assert target.exists()


def test_verify_digest_mismatch_deletes_and_raises(tmp_path: Path):
    from app.core.errors import DownloadError

    target = tmp_path / "Setup.exe"
    target.write_bytes(b"tampered installer")
    with pytest.raises(DownloadError):
        update._verify_digest(target, "0" * 64)
    # A file that failed its checksum must never be left where it could be run.
    assert not target.exists()


def test_download_installer_rejects_wrong_checksum(server: MediaServer, tmp_path: Path):
    url = server.add("/GrabLine-Setup.exe", payload(1 * MB, 9))
    from app.core.errors import DownloadError

    with pytest.raises(DownloadError):
        update.download_installer(
            url, str(tmp_path), "GrabLine-Setup.exe", expected_sha256="0" * 64
        )
    assert not (tmp_path / "GrabLine-Setup.exe").exists()


def test_download_installer_accepts_matching_checksum(server: MediaServer, tmp_path: Path):
    from app.tests.media_server import sha256

    data = payload(1 * MB, 10)
    url = server.add("/GrabLine-Setup.exe", data)
    path = update.download_installer(
        url, str(tmp_path), "GrabLine-Setup.exe", expected_sha256=sha256(data)
    )
    assert Path(path).read_bytes() == data
