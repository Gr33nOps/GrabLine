"""Checksum helpers: hash a finished file and compare it to an expected value.

Supports MD5, SHA-1, SHA-256, SHA-512 (hashlib) and CRC32 (zlib). The
algorithm behind a pasted digest is inferred from its length, so a user can
paste any of them and GrabLine verifies against the right one.
"""

from __future__ import annotations

import hashlib
import zlib
from pathlib import Path

#: Algorithms offered, in the order the checksums panel shows them.
ALGORITHMS = ("md5", "sha1", "sha256", "sha512", "crc32")
_HASHLIB = ("md5", "sha1", "sha256", "sha512")
_CHUNK = 1 << 20

#: Hex-digest length -> algorithm, for guessing what a pasted value is.
_LENGTH_TO_ALGO = {8: "crc32", 32: "md5", 40: "sha1", 64: "sha256", 128: "sha512"}


def hash_file(path: Path, algorithm: str = "sha256") -> str:
    """Streaming hex digest of ``path`` (never loads the whole file)."""
    if algorithm == "crc32":
        crc = 0
        with open(path, "rb") as handle:
            while chunk := handle.read(_CHUNK):
                crc = zlib.crc32(chunk, crc)
        return f"{crc & 0xFFFFFFFF:08x}"
    if algorithm not in _HASHLIB:
        raise ValueError(f"unsupported algorithm: {algorithm}")
    digest = hashlib.new(algorithm)
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def hash_all(path: Path) -> dict[str, str]:
    """Every supported checksum of ``path`` (streamed, so large files are OK)."""
    return {algorithm: hash_file(path, algorithm) for algorithm in ALGORITHMS}


def guess_algorithm(expected: str) -> str:
    """Pick the algorithm implied by a hex digest's length."""
    return _LENGTH_TO_ALGO.get(len(expected.strip()), "sha256")


def verify_file(path: Path, expected: str, algorithm: str | None = None) -> bool:
    """True if ``path``'s digest equals ``expected`` (case-insensitive)."""
    expected = expected.strip().lower()
    algorithm = algorithm or guess_algorithm(expected)
    return hash_file(path, algorithm).lower() == expected
