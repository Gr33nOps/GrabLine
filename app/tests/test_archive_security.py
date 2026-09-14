"""Archive attack tests for the 1.23.0 security pass.

Each test crafts a hostile archive and asserts extraction refuses it, plus a
matching benign case proving legitimate archives still extract. These are the
proof tests behind findings F1 (decompression bomb) and F2 (external-tool
path traversal) in docs/security-model.md.
"""

from __future__ import annotations

import bz2
import shutil
import zipfile
from pathlib import Path

import pytest

from app.core import archive
from app.core.errors import DownloadError

MB = 1024 * 1024


# --------------------------------------------------------- F1: decompression bomb


def test_zip_bomb_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A tiny zip that declares more output than the ceiling is refused before
    a byte lands on disk - the declared-size check (CWE-409). The ceiling is
    shrunk for the test so it needn't write 20 GB to prove the point; the
    zip's declared member size is honest and real."""
    monkeypatch.setattr(archive, "_MAX_EXTRACTED_BYTES", 8 * MB)
    bomb = tmp_path / "bomb.zip"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("bomb.bin", b"\0" * (64 * MB))  # declares 64 MB > 8 MB cap
    assert bomb.stat().st_size < MB  # ~kilobytes on disk
    with pytest.raises(DownloadError, match="decompression bomb"):
        archive.extract(bomb, tmp_path / "out")
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())


def test_streaming_bomb_refused_without_declared_size(tmp_path: Path):
    """A bare .bz2 declares no size, so the ceiling must hold on the stream
    itself - the writer stops the moment output passes the cap."""
    # Shrink the ceiling for the test rather than write 20 GiB.
    original = archive._MAX_EXTRACTED_BYTES
    archive._MAX_EXTRACTED_BYTES = 4 * MB
    try:
        blob = tmp_path / "big.bin.bz2"
        blob.write_bytes(bz2.compress(b"\0" * (16 * MB)))  # 16 MB > 4 MB cap
        with pytest.raises(DownloadError, match="decompression bomb"):
            archive.extract(blob, tmp_path / "out2")
        # The partial output is cleaned up, never left behind.
        assert not (tmp_path / "out2" / "big.bin").exists()
    finally:
        archive._MAX_EXTRACTED_BYTES = original


def test_normal_archive_still_extracts(tmp_path: Path):
    """The ceiling must never reject a legitimate archive."""
    good = tmp_path / "good.zip"
    with zipfile.ZipFile(good, "w") as z:
        z.writestr("notes.txt", "hello")
        z.writestr("data/report.csv", "a,b,c\n1,2,3\n")
    out = archive.extract(good, tmp_path / "out3")
    assert (out / "notes.txt").read_text() == "hello"
    assert (out / "data" / "report.csv").exists()


# --------------------------------------------------- F2: external-tool traversal


def _seven_zip() -> str | None:
    return shutil.which("7z") or shutil.which("7za")


@pytest.mark.skipif(_seven_zip() is None, reason="needs 7z to build a .7z")
def test_external_traversal_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A .7z whose listing shows a member escaping the target is refused
    before the external tool runs (CWE-22) - parity with the zip/tar guard.

    The listing is what the guard inspects, so the attack is simulated by a
    listing that contains a traversal entry; the real 7z refuses to build such
    an archive, which is exactly why the in-app guard is defense in depth.
    """
    payload = tmp_path / "x.txt"
    payload.write_text("data")
    bundle = tmp_path / "a.7z"
    import subprocess

    sevenz = _seven_zip()
    assert sevenz is not None  # skipif guards this; narrows str | None -> str
    subprocess.run([sevenz, "a", str(bundle), str(payload)], capture_output=True, check=True)

    # Force the listing to include an escaping member.
    evil = (archive.ArchiveEntry("../../escaped.txt", 4), archive.ArchiveEntry("x.txt", 4))
    monkeypatch.setattr(archive, "_list_external", lambda _p: evil)

    with pytest.raises(DownloadError, match="unsafe paths"):
        archive.extract(bundle, tmp_path / "out")


@pytest.mark.skipif(_seven_zip() is None, reason="needs 7z")
def test_external_normal_still_extracts(tmp_path: Path):
    """The external guard must not reject a benign .7z."""
    payload = tmp_path / "hello.txt"
    payload.write_text("hi there")
    bundle = tmp_path / "b.7z"
    import subprocess

    sevenz = _seven_zip()
    assert sevenz is not None  # skipif guards this; narrows str | None -> str
    subprocess.run([sevenz, "a", str(bundle), str(payload)], capture_output=True, check=True)
    out = archive.extract(bundle, tmp_path / "out")
    assert (out / "hello.txt").read_text() == "hi there"
