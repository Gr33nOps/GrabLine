"""URL pattern expansion, checksum verification, and archive extraction."""

from __future__ import annotations

import hashlib
import tarfile
import zipfile
from pathlib import Path

import pytest

from app.core import archive, verify
from app.core.batch import MAX_EXPANSION, expand_all, expand_pattern
from app.core.errors import DownloadError

# --------------------------------------------------------- URL patterns


def test_numeric_range_expands():
    assert expand_pattern("http://x/f[1-3].jpg") == [
        "http://x/f1.jpg",
        "http://x/f2.jpg",
        "http://x/f3.jpg",
    ]


def test_zero_padded_range_keeps_width():
    assert expand_pattern("http://x/img[08-10].png") == [
        "http://x/img08.png",
        "http://x/img09.png",
        "http://x/img10.png",
    ]


def test_letter_range_and_descending():
    assert expand_pattern("http://x/[a-c].txt") == [
        "http://x/a.txt",
        "http://x/b.txt",
        "http://x/c.txt",
    ]
    assert expand_pattern("http://x/f[3-1].jpg") == [
        "http://x/f3.jpg",
        "http://x/f2.jpg",
        "http://x/f1.jpg",
    ]


def test_two_ranges_multiply():
    out = expand_pattern("http://x/[1-2]/[a-b].jpg")
    assert out == [
        "http://x/1/a.jpg",
        "http://x/1/b.jpg",
        "http://x/2/a.jpg",
        "http://x/2/b.jpg",
    ]


def test_no_range_is_unchanged():
    assert expand_pattern("http://x/plain.jpg") == ["http://x/plain.jpg"]


def test_runaway_expansion_is_refused():
    huge = f"http://x/f[1-{MAX_EXPANSION + 5}].jpg"
    assert expand_pattern(huge) == [huge]  # treated as a literal, not exploded


def test_expand_all_dedupes():
    assert expand_all(["http://x/[1-2].jpg", "http://x/1.jpg"]) == [
        "http://x/1.jpg",
        "http://x/2.jpg",
    ]


# ----------------------------------------------------------- checksums


def test_hash_and_verify(tmp_path: Path):
    f = tmp_path / "data.bin"
    f.write_bytes(b"grabline")
    expected_sha = hashlib.sha256(b"grabline").hexdigest()
    assert verify.hash_file(f, "sha256") == expected_sha
    assert verify.verify_file(f, expected_sha)
    assert verify.verify_file(f, expected_sha.upper())  # case-insensitive
    assert not verify.verify_file(f, "deadbeef" * 8)


def test_algorithm_guessed_from_length(tmp_path: Path):
    f = tmp_path / "data.bin"
    f.write_bytes(b"grabline")
    assert verify.guess_algorithm("a" * 32) == "md5"
    assert verify.guess_algorithm("a" * 64) == "sha256"
    assert verify.verify_file(f, hashlib.md5(b"grabline").hexdigest())


# ----------------------------------------------------------- extraction


def test_extract_zip(tmp_path: Path):
    src = tmp_path / "bundle.zip"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("a.txt", "one")
        z.writestr("sub/b.txt", "two")
    out = archive.extract(src)
    assert (out / "a.txt").read_text() == "one"
    assert (out / "sub" / "b.txt").read_text() == "two"


def test_extract_tar_gz(tmp_path: Path):
    payload = tmp_path / "a.txt"
    payload.write_text("hello")
    src = tmp_path / "bundle.tar.gz"
    with tarfile.open(src, "w:gz") as t:
        t.add(payload, arcname="a.txt")
    out = archive.extract(src)
    assert (out / "a.txt").read_text() == "hello"


def test_extract_rejects_path_traversal(tmp_path: Path):
    src = tmp_path / "evil.zip"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("../escape.txt", "nope")
    with pytest.raises(DownloadError, match="unsafe"):
        archive.extract(src)


def test_is_archive():
    assert archive.is_archive(Path("x.zip"))
    assert archive.is_archive(Path("x.tar.gz"))
    assert archive.is_archive(Path("x.7z"))
    assert not archive.is_archive(Path("x.mp4"))
