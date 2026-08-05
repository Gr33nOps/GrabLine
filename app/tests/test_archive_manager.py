"""The archive manager: preview listings, selected-file extraction, bare
.gz/.bz2/.xz decompression, password handling, and the pre-extract virus scan.
"""

from __future__ import annotations

import bz2
import gzip
import lzma
import struct
import subprocess
import tarfile
import zipfile
import zlib
from pathlib import Path

import pytest

from app.core import archive, virusscan
from app.core.archive import ArchiveEntry, PasswordRequired
from app.core.errors import DownloadError

# --------------------------------------------------------- encrypted fixture
#
# The standard library can DECRYPT ZipCrypto but not encrypt it, so the test
# fixture implements the (public, trivial) cipher itself and hand-writes a
# one-entry zip. This keeps the password tests end-to-end and real: extract()
# below runs the actual zipfile decryptor against actual encrypted bytes.

_CRC_TABLE = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ 0xEDB88320 if _c & 1 else _c >> 1
    _CRC_TABLE.append(_c)


def _crc(ch: int, crc: int) -> int:
    return ((crc >> 8) & 0xFFFFFF) ^ _CRC_TABLE[(crc ^ ch) & 0xFF]


class _ZipCrypto:
    def __init__(self, password: bytes) -> None:
        self.keys = [0x12345678, 0x23456789, 0x34567890]
        for ch in password:
            self._update(ch)

    def _update(self, ch: int) -> None:
        k = self.keys
        k[0] = _crc(ch, k[0])
        k[1] = (k[1] + (k[0] & 0xFF)) & 0xFFFFFFFF
        k[1] = (k[1] * 134775813 + 1) & 0xFFFFFFFF
        k[2] = _crc((k[1] >> 24) & 0xFF, k[2])

    def encrypt(self, data: bytes) -> bytes:
        out = bytearray()
        for ch in data:
            temp = (self.keys[2] | 2) & 0xFFFF
            out.append(ch ^ (((temp * (temp ^ 1)) >> 8) & 0xFF))
            self._update(ch)  # the key stream advances on the PLAINTEXT byte
        return bytes(out)


def _write_encrypted_zip(path: Path, name: str, data: bytes, password: str) -> None:
    """A single STORED, ZipCrypto-encrypted entry, written record by record."""
    crc = zlib.crc32(data) & 0xFFFFFFFF
    header = bytes(11) + bytes([(crc >> 24) & 0xFF])  # 12th byte = check byte
    payload = _ZipCrypto(password.encode()).encrypt(header + data)
    name_bytes = name.encode()
    local = (
        struct.pack(
            "<IHHHHHIIIHH",
            0x04034B50,
            20,
            0x1,
            0,
            0,
            0,
            crc,
            len(payload),
            len(data),
            len(name_bytes),
            0,
        )
        + name_bytes
    )
    central = (
        struct.pack(
            "<IHHHHHHIIIHHHHHII",
            0x02014B50,
            20,
            20,
            0x1,
            0,
            0,
            0,
            crc,
            len(payload),
            len(data),
            len(name_bytes),
            0,
            0,
            0,
            0,
            0,
            0,
        )
        + name_bytes
    )
    eocd = struct.pack(
        "<IHHHHIIH", 0x06054B50, 0, 0, 1, 1, len(central), len(local) + len(payload), 0
    )
    path.write_bytes(local + payload + central + eocd)


# ------------------------------------------------------------- single files


@pytest.mark.parametrize(
    ("suffix", "opener"),
    [(".gz", gzip.open), (".bz2", bz2.open), (".xz", lzma.open)],
)
def test_bare_compressed_file_decompresses_next_to_itself(tmp_path: Path, suffix, opener):
    src = tmp_path / f"data.csv{suffix}"
    with opener(src, "wb") as handle:
        handle.write(b"a,b\n1,2\n")
    out = archive.extract(src)
    assert out == tmp_path / "data.csv"
    assert out.read_bytes() == b"a,b\n1,2\n"


def test_corrupt_gz_is_a_friendly_error_and_leaves_nothing(tmp_path: Path):
    src = tmp_path / "broken.gz"
    src.write_bytes(b"not gzip at all")
    with pytest.raises(DownloadError):
        archive.extract(src)
    assert not (tmp_path / "broken").exists()


def test_new_suffixes_count_as_archives():
    assert archive.is_archive(Path("x.gz"))
    assert archive.is_archive(Path("x.bz2"))
    assert archive.is_archive(Path("x.xz"))
    assert not archive.is_archive(Path("x.txt"))


# ------------------------------------------------------------------ preview


def test_list_entries_zip(tmp_path: Path):
    src = tmp_path / "bundle.zip"
    with zipfile.ZipFile(src, "w") as bundle:
        bundle.writestr("a.txt", "one")
        bundle.writestr("sub/b.txt", "twotwo")
    entries = {entry.name: entry for entry in archive.list_entries(src)}
    assert entries["a.txt"].size == 3
    assert entries["sub/b.txt"].size == 6
    assert not entries["a.txt"].is_dir


def test_list_entries_tar(tmp_path: Path):
    payload = tmp_path / "a.txt"
    payload.write_text("hello")
    src = tmp_path / "bundle.tar.gz"
    with tarfile.open(src, "w:gz") as bundle:
        bundle.add(payload, arcname="a.txt")
    entries = archive.list_entries(src)
    assert entries[0].name == "a.txt"
    assert entries[0].size == 5


def test_list_entries_gz_reads_size_from_footer(tmp_path: Path):
    src = tmp_path / "report.txt.gz"
    with gzip.open(src, "wb") as handle:
        handle.write(b"x" * 1234)
    (entry,) = archive.list_entries(src)
    assert entry.name == "report.txt"
    assert entry.size == 1234


def test_list_entries_works_on_an_encrypted_zip_without_a_password(tmp_path: Path):
    src = tmp_path / "locked.zip"
    _write_encrypted_zip(src, "secret.txt", b"top secret", "hunter2")
    (entry,) = archive.list_entries(src)
    assert entry.name == "secret.txt"
    assert entry.size == 10


def test_parse_7z_listing():
    output = (
        "7-Zip 23.01\n\nListing archive: x.7z\n\nPath = x.7z\nType = 7z\n\n"
        "----------\n"
        "Path = docs\nSize = 0\nAttributes = D_ drwxr-xr-x\n\n"
        "Path = docs/a.txt\nSize = 42\nAttributes = A_ -rw-r--r--\n"
    )
    entries = archive._parse_7z_listing(output)
    assert entries == (
        ArchiveEntry("docs", None, True),
        ArchiveEntry("docs/a.txt", 42, False),
    )


# --------------------------------------------------------- selected members


def test_extract_selected_members_zip(tmp_path: Path):
    src = tmp_path / "bundle.zip"
    with zipfile.ZipFile(src, "w") as bundle:
        bundle.writestr("keep.txt", "yes")
        bundle.writestr("skip.txt", "no")
        bundle.writestr("docs/inner.txt", "dir")
    out = archive.extract(src, members=["keep.txt", "docs"])
    assert (out / "keep.txt").read_text() == "yes"
    assert (out / "docs" / "inner.txt").read_text() == "dir"  # dir takes children
    assert not (out / "skip.txt").exists()


def test_extract_selected_members_tar(tmp_path: Path):
    payload = tmp_path / "a.txt"
    payload.write_text("one")
    src = tmp_path / "bundle.tar"
    with tarfile.open(src, "w") as bundle:
        bundle.add(payload, arcname="a.txt")
        bundle.add(payload, arcname="b.txt")
    out = archive.extract(src, members=["b.txt"])
    assert (out / "b.txt").exists()
    assert not (out / "a.txt").exists()


# ---------------------------------------------------------------- passwords


def test_encrypted_zip_fixture_is_readable_by_the_stdlib(tmp_path: Path):
    """Sanity for the hand-rolled cipher: python's own decryptor accepts it."""
    src = tmp_path / "locked.zip"
    _write_encrypted_zip(src, "secret.txt", b"top secret", "hunter2")
    with zipfile.ZipFile(src) as bundle:
        assert bundle.read("secret.txt", pwd=b"hunter2") == b"top secret"


def test_encrypted_zip_tries_passwords_in_order(tmp_path: Path):
    src = tmp_path / "locked.zip"
    _write_encrypted_zip(src, "secret.txt", b"top secret", "hunter2")
    out = archive.extract(src, passwords=["wrong", "hunter2"])
    assert (out / "secret.txt").read_bytes() == b"top secret"


def test_encrypted_zip_without_the_password_raises_password_required(tmp_path: Path):
    src = tmp_path / "locked.zip"
    _write_encrypted_zip(src, "secret.txt", b"top secret", "hunter2")
    with pytest.raises(PasswordRequired):
        archive.extract(src)
    with pytest.raises(PasswordRequired):
        archive.extract(src, passwords=["wrong", "also wrong"])


# --------------------------------------------------------------- virus scan


def _fake_run(returncode: int, stdout: str = "", stderr: str = ""):
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    return run


def test_scan_clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(virusscan, "find_scanner", lambda pref="auto": ("ClamAV", ["clamscan"]))
    monkeypatch.setattr("app.core.virusscan.subprocess.run", _fake_run(0))
    result = virusscan.scan(tmp_path / "x.zip")
    assert result.clean and result.scanner == "ClamAV"


def test_scan_infected_reports_the_finding(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(virusscan, "find_scanner", lambda pref="auto": ("ClamAV", ["clamscan"]))
    monkeypatch.setattr(
        "app.core.virusscan.subprocess.run",
        _fake_run(1, stdout="x.zip: Eicar-Test-Signature FOUND\n"),
    )
    result = virusscan.scan(tmp_path / "x.zip")
    assert not result.clean
    assert "Eicar" in result.detail


def test_scan_without_a_scanner_refuses_honestly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(virusscan, "find_scanner", lambda pref="auto": None)
    with pytest.raises(DownloadError, match="no virus scanner"):
        virusscan.scan(tmp_path / "x.zip")


def test_scan_error_exit_code_is_not_treated_as_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(virusscan, "find_scanner", lambda pref="auto": ("ClamAV", ["clamscan"]))
    monkeypatch.setattr(
        "app.core.virusscan.subprocess.run", _fake_run(2, stderr="database missing")
    )
    with pytest.raises(DownloadError, match="could not run"):
        virusscan.scan(tmp_path / "x.zip")
