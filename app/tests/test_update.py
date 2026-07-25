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
