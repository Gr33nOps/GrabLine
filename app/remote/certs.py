"""The PC's TLS identity for the phone-remote listener (PROTOCOL.md section 4).

One ECDSA P-256 key and one self-signed certificate, created the first time
they are needed and reused for as long as the data directory lives. The phone
pins the *key* (see :func:`pin`), so the identity is the key file:

* key missing, unreadable or not a P-256 key -> a new key and certificate are
  made, the pin changes and every pairing breaks (QA-07 relies on this);
* key fine but the certificate is missing, corrupt, expired, for another key
  or for another ``pcId`` -> the certificate alone is re-issued around the
  same key, so the pin and the pairings survive (PROTOCOL.md, "The pin").

Files: ``<data dir>/remote/remote_cert.pem`` and ``remote_key.pem``. Both are
written atomically (temp file in the same folder, fsync, ``os.replace``). On
POSIX the folder is 0700 and the key file is created 0600. On Windows there
are no mode bits to set: the files inherit the per-user ACL of LOCALAPPDATA
(the user, SYSTEM and Administrators), which is the best this module does
there - it does not rewrite ACLs.

Nothing here logs key material, PEM text or the pin. The module is Qt-free at
import time; the Qt helpers import PySide6 only when called.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import hashlib
import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.core import paths

if TYPE_CHECKING:
    from PySide6.QtNetwork import QSslCertificate, QSslKey

log = logging.getLogger(__name__)

REMOTE_SUBDIR = "remote"
CERT_FILENAME = "remote_cert.pem"
KEY_FILENAME = "remote_key.pem"

_VALID_BEFORE = dt.timedelta(days=1)
_VALID_FOR = dt.timedelta(days=30 * 365 + 8)  # 30 years, leap days included
_MAX_PEM_BYTES = 64 * 1024  # a P-256 key or certificate is under 1 KiB
_PC_ID_RE = re.compile(r"[A-Za-z0-9_-]{22}")


@dataclass(frozen=True)
class PcIdentity:
    """The loaded certificate and key. ``key_pem`` is secret: never log it."""

    cert_pem: bytes
    key_pem: bytes = field(repr=False)
    pin: str


def remote_dir() -> Path:
    """``<GrabLine data dir>/remote`` (not created here)."""
    return paths.data_dir() / REMOTE_SUBDIR


def pin_from_spki(spki_der: bytes) -> str:
    """SHA-256 of a DER SubjectPublicKeyInfo, base64url without padding (43 chars)."""
    return base64.urlsafe_b64encode(hashlib.sha256(spki_der).digest()).rstrip(b"=").decode("ascii")


def pin(cert: x509.Certificate) -> str:
    """The pin a phone stores for ``cert`` (PROTOCOL.md section 4, "The pin")."""
    return pin_from_spki(
        cert.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )


def load_or_create(pc_id: str, directory: Path | None = None) -> PcIdentity:
    """Return the stored identity, creating or repairing it when needed.

    ``pc_id`` is the 22-character base64url id of this PC; it is the
    certificate's common name. ``directory`` defaults to :func:`remote_dir`.
    """
    _check_pc_id(pc_id)
    folder = paths.ensure_private_dir(directory if directory is not None else remote_dir())
    key = _load_key(folder / KEY_FILENAME)
    if key is None:
        log.info("phone remote: no usable identity key, creating a new identity")
        return _create(pc_id, folder)
    cert_pem = _load_cert_pem(folder / CERT_FILENAME, key, pc_id)
    if cert_pem is None:
        log.info("phone remote: re-issuing the certificate for the existing key")
        cert = _issue(key, pc_id)
        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        _write_atomic(folder / CERT_FILENAME, cert_pem, private=False)
    return PcIdentity(cert_pem, _key_pem(key), pin_from_spki(_spki(key)))


def regenerate(pc_id: str, directory: Path | None = None) -> PcIdentity:
    """Replace the key and certificate ("Reset phone connections").

    The pin changes, so every paired phone refuses this PC until it pairs
    again; the caller is responsible for dropping the device rows.
    """
    _check_pc_id(pc_id)
    folder = paths.ensure_private_dir(directory if directory is not None else remote_dir())
    log.info("phone remote: identity reset, creating a new identity")
    return _create(pc_id, folder)


def qt_tls_available() -> bool:
    """True when Qt has a working TLS backend (the listener cannot run without one)."""
    from PySide6.QtNetwork import QSslSocket

    return bool(QSslSocket.supportsSsl())


def qt_certificate(identity: PcIdentity) -> QSslCertificate:
    """``identity``'s certificate as a QSslCertificate. ValueError if Qt cannot read it."""
    from PySide6.QtCore import QByteArray
    from PySide6.QtNetwork import QSsl, QSslCertificate

    cert = QSslCertificate(QByteArray(identity.cert_pem), QSsl.EncodingFormat.Pem)
    if cert.isNull():
        raise ValueError("Qt could not load the phone-remote certificate")
    return cert


def qt_private_key(identity: PcIdentity) -> QSslKey:
    """``identity``'s private key as a QSslKey. ValueError if Qt cannot read it."""
    from PySide6.QtCore import QByteArray
    from PySide6.QtNetwork import QSsl, QSslKey

    key = QSslKey(
        QByteArray(identity.key_pem),
        QSsl.KeyAlgorithm.Ec,
        QSsl.EncodingFormat.Pem,
        QSsl.KeyType.PrivateKey,
    )
    if key.isNull():
        raise ValueError("Qt could not load the phone-remote private key")
    return key


# --- internals ---------------------------------------------------------------


def _check_pc_id(pc_id: str) -> None:
    if not _PC_ID_RE.fullmatch(pc_id):
        raise ValueError("pc_id must be 22 base64url characters")


def _subject(pc_id: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"GrabLine {pc_id}")])


def _spki(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    # SEC1 ("BEGIN EC PRIVATE KEY"): the form every Qt TLS backend reads.
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )


def _issue(key: ec.EllipticCurvePrivateKey, pc_id: str) -> x509.Certificate:
    now = dt.datetime.now(dt.UTC)
    name = _subject(pc_id)
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _VALID_BEFORE)
        .not_valid_after(now + _VALID_FOR)
        .sign(key, hashes.SHA256())
    )


def _create(pc_id: str, folder: Path) -> PcIdentity:
    key = ec.generate_private_key(ec.SECP256R1())
    cert = _issue(key, pc_id)
    key_pem = _key_pem(key)
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    # Key first: if the certificate write is lost, the next load re-issues the
    # certificate around this key instead of serving a mismatched pair.
    _write_atomic(folder / KEY_FILENAME, key_pem, private=True)
    _write_atomic(folder / CERT_FILENAME, cert_pem, private=False)
    return PcIdentity(cert_pem, key_pem, pin_from_spki(_spki(key)))


def _read_small(path: Path) -> bytes | None:
    try:
        with path.open("rb") as handle:
            data = handle.read(_MAX_PEM_BYTES + 1)
    except OSError:
        return None
    return data if 0 < len(data) <= _MAX_PEM_BYTES else None


def _load_key(path: Path) -> ec.EllipticCurvePrivateKey | None:
    data = _read_small(path)
    if data is None:
        return None
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except Exception:  # any parse failure means "no usable key"; no detail logged
        return None
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        return None
    return key


def _load_cert_pem(path: Path, key: ec.EllipticCurvePrivateKey, pc_id: str) -> bytes | None:
    """The stored certificate's PEM if it is still good for ``key`` and ``pc_id``."""
    data = _read_small(path)
    if data is None:
        return None
    try:
        cert = x509.load_pem_x509_certificate(data)
        same_key = secrets.compare_digest(
            cert.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            ),
            _spki(key),
        )
        now = dt.datetime.now(dt.UTC)
        usable = (
            same_key
            and cert.subject == _subject(pc_id)
            and cert.issuer == cert.subject
            and cert.not_valid_before_utc <= now < cert.not_valid_after_utc
        )
    except Exception:  # corrupt or unsupported certificate
        return None
    return cert.public_bytes(serialization.Encoding.PEM) if usable else None


def write_private_file(path: Path, data: bytes) -> None:
    """Write a secret (mode 0600 on POSIX) exactly as the PC key is written:
    temp file in the same folder, fsync, ``os.replace``. Used for the per-link
    device keys of ``app.remote.client``."""
    _write_atomic(path, data, private=True)


def _write_atomic(path: Path, data: bytes, *, private: bool) -> None:
    """Write ``data`` to ``path`` through a same-folder temp file and a rename.

    The temp file is created exclusively with its final mode (0600 for the
    key), so the secret is never on disk with wider permissions.
    """
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    mode = 0o600 if private else 0o644
    try:
        fd = os.open(tmp, flags, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            os.chmod(tmp, mode)  # the mode given to os.open is masked by umask
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
