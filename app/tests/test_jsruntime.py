"""Deno JS-runtime manager: pinned download, SHA-256 verification, extraction."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from app.core import jsruntime
from app.core.errors import DownloadError
from app.core.ffmpeg_pins import PinnedArchive
from app.tests.media_server import MediaServer

FAKE_DENO = b"#!/bin/sh\necho 'deno 2.9.2'\n"


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, content in members.items():
            bundle.writestr(name, content)
    return buffer.getvalue()


def _pin_for(url: str, archive: bytes) -> PinnedArchive:
    return PinnedArchive(url=url, sha256=hashlib.sha256(archive).hexdigest(), format="zip")


def test_deno_pin_covers_every_platform():
    for key in ("linux-x86_64", "linux-aarch64", "windows-x86_64", "darwin-x86_64", "darwin-arm64"):
        pin = jsruntime.deno_pin(key)
        assert pin is not None
        assert pin.url.startswith(f"https://dl.deno.land/release/{jsruntime.DENO_VERSION}/")
        assert len(pin.sha256) == 64
    assert jsruntime.deno_pin("plan9-vax") is None


def test_ensure_deno_installs_and_verifies_hash(server: MediaServer, tmp_path: Path):
    archive = _zip({"deno": FAKE_DENO})  # release zips carry the binary at the root
    url = server.add("/deno.zip", archive)
    bin_dir = tmp_path / "bin"

    path = jsruntime.ensure_deno(bin_dir=bin_dir, pin=_pin_for(url, archive), verify_run=False)

    assert path == jsruntime.managed_deno(bin_dir)
    assert path.is_file()


def test_ensure_deno_installs_once_under_concurrency(server: MediaServer, tmp_path: Path):
    # Several YouTube jobs starting together must not each download Deno into
    # the same dir (Windows would lock the file); the lock installs it once.
    import threading

    archive = _zip({"deno": FAKE_DENO})
    url = server.add("/deno.zip", archive)
    pin = _pin_for(url, archive)
    bin_dir = tmp_path / "bin"
    results: list[Path] = []

    def worker() -> None:
        results.append(jsruntime.ensure_deno(bin_dir=bin_dir, pin=pin, verify_run=False))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r == jsruntime.managed_deno(bin_dir) for r in results)
    assert server.request_count("/deno.zip") == 1  # downloaded exactly once


def test_ensure_deno_rejects_a_bad_hash(server: MediaServer, tmp_path: Path):
    archive = _zip({"deno": FAKE_DENO})
    url = server.add("/deno.zip", archive)
    bad = PinnedArchive(url=url, sha256="00" * 32, format="zip")

    with pytest.raises(DownloadError, match="integrity check"):
        jsruntime.ensure_deno(bin_dir=tmp_path / "bin", pin=bad, verify_run=False)
    assert not jsruntime.managed_deno(tmp_path / "bin").exists()  # nothing installed


def test_ensure_deno_reuses_existing_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.core.jsruntime.shutil.which", lambda _: "/usr/bin/deno")
    # A pin whose URL would 404 proves no download is attempted when Deno exists.
    path = jsruntime.ensure_deno(
        bin_dir=tmp_path / "bin",
        pin=PinnedArchive(url="http://127.0.0.1:1/none.zip", sha256="x", format="zip"),
    )
    assert str(path) == "/usr/bin/deno"


def test_detect_js_runtime_prefers_managed_deno(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.core.jsruntime.shutil.which", lambda _: "/usr/bin/node")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    jsruntime.managed_deno(bin_dir).write_bytes(FAKE_DENO)  # our install wins
    assert jsruntime.detect_js_runtime(bin_dir) == ("deno", str(jsruntime.managed_deno(bin_dir)))


def test_detect_js_runtime_finds_installed_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # No managed Deno, no deno on PATH, but node is installed -> use node.
    which = {"node": "/usr/bin/node"}
    monkeypatch.setattr("app.core.jsruntime.shutil.which", lambda exe: which.get(exe))
    assert jsruntime.detect_js_runtime(tmp_path / "bin") == ("node", "/usr/bin/node")


def test_detect_js_runtime_none_when_nothing_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("app.core.jsruntime.shutil.which", lambda _: None)
    assert jsruntime.detect_js_runtime(tmp_path / "bin") is None


def test_find_deno_prefers_managed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.core.jsruntime.shutil.which", lambda _: "/usr/bin/deno")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    managed = jsruntime.managed_deno(bin_dir)
    managed.write_bytes(FAKE_DENO)
    assert jsruntime.find_deno(bin_dir) == str(managed)
