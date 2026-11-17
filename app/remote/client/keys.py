"""The device key of one link (PROTOCOL.md section 21, "Key").

One ECDSA P-256 key per link, made for that pairing. The private half is
stored the way ``certs.py`` stores the PC key: a private file in the data
directory (``remote/links/``, folder 0700 and file 0600 on POSIX, the per-user
ACL of LOCALAPPDATA on Windows), written atomically. The database keeps only
the file's name. A key is deleted when its link is removed, when the host says
the pairing was removed, and when a pairing attempt fails.

The key never leaves this module as anything but a signature or the public
key's SPKI DER; PEM text is never logged and ``repr`` shows nothing.
"""

from __future__ import annotations

import contextlib
import logging
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.core import paths
from app.remote import certs

log = logging.getLogger(__name__)

LINKS_SUBDIR = "links"
_FILE_RE = re.compile(r"link-[0-9a-f]{16}\.pem")
_MAX_PEM_BYTES = 64 * 1024


@dataclass(frozen=True)
class DeviceKey:
    """A link's private key held in memory while it is used."""

    private: ec.EllipticCurvePrivateKey = field(repr=False)

    @property
    def public_der(self) -> bytes:
        """SubjectPublicKeyInfo DER, 91 bytes: what ``pair`` sends."""
        return self.private.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    def sign(self, message: bytes) -> bytes:
        """ECDSA-P256-SHA256, ASN.1 DER (section 6)."""
        return self.private.sign(message, ec.ECDSA(hashes.SHA256()))

    def pem(self) -> bytes:
        return self.private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )


def new_key() -> DeviceKey:
    return DeviceKey(ec.generate_private_key(ec.SECP256R1()))


def keys_dir(base: Path | None = None) -> Path:
    """``<data dir>/remote/links`` (or ``base``), not created here."""
    return base if base is not None else certs.remote_dir() / LINKS_SUBDIR


def new_file_name() -> str:
    return f"link-{secrets.token_hex(8)}.pem"


def is_valid_file_name(name: str) -> bool:
    """Only our own generated names: a name from the database can never walk
    out of the keys folder."""
    return bool(_FILE_RE.fullmatch(name))


def save(key: DeviceKey, base: Path | None = None) -> str:
    """Write the key to a new private file and return its file name."""
    folder = paths.ensure_private_dir(keys_dir(base))
    name = new_file_name()
    certs.write_private_file(folder / name, key.pem())
    return name


def load(name: str, base: Path | None = None) -> DeviceKey | None:
    """The stored key, or None if the file is missing, not ours, too big or
    not a P-256 key."""
    if not is_valid_file_name(name):
        return None
    try:
        with (keys_dir(base) / name).open("rb") as handle:
            data = handle.read(_MAX_PEM_BYTES + 1)
    except OSError:
        return None
    if not 0 < len(data) <= _MAX_PEM_BYTES:
        return None
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except Exception:  # any parse failure means "no usable key"; nothing is logged
        return None
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        return None
    return DeviceKey(key)


def delete(name: str, base: Path | None = None) -> None:
    """Remove a key file. Never raises."""
    if not is_valid_file_name(name):
        return
    with contextlib.suppress(OSError):
        (keys_dir(base) / name).unlink()
