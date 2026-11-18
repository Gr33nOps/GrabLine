"""Paired phones: the device list, the stable pcId and the login check.

Pure logic over the app database: no sockets, no Qt. PROTOCOL.md sections 5.2
(pcId), 6 (authentication) and 8 (revocation).

The PC keeps only each phone's PUBLIC key, so nothing in the database can act
as a phone. Removing a device deletes its row; a removed device and one that
never existed are the same thing to :func:`verify_auth` and get the same
answer. Everything here fails closed and nothing is logged: ids, keys, nonces
and signatures never reach a log line.
"""

from __future__ import annotations

import base64
import binascii
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.db.database import Database

MAX_DEVICES = 8
DEVICE_ID_BYTES = 16
DEVICE_ID_LENGTH = 22
DEVICE_NAME_MAX = 64
#: PROTOCOL.md section 21: what a paired device is. A device that does not say is a phone.
DEVICE_KINDS = ("phone", "pc")
DEFAULT_DEVICE_KIND = "phone"
NONCE_BYTES = 32
PIN_BYTES = 32
PUBLIC_KEY_DER_BYTES = 91
SIGNATURE_MAX_BYTES = 72

_AUTH_LABEL = b"grabline-auth-v1\x00"
_PC_ID_KEY = "pc_id"
_B64URL_RE = re.compile(r"[A-Za-z0-9_-]*\Z")
# C0 controls, DEL, C1 controls, and the invisible direction/format marks that
# could make a device name render misleadingly in the Allow prompt.
_NAME_STRIP_RE = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]")


class DeviceLimitError(Exception):
    """The device list is full (``MAX_DEVICES``) or the device id is taken."""


def b64url_encode(data: bytes) -> str:
    """base64url without padding (PROTOCOL.md type ``bytes``)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    """Strict inverse of :func:`b64url_encode`.

    Raises ``ValueError`` for padding, the standard alphabet, whitespace, an
    impossible length or non-canonical trailing bits: one value has exactly
    one accepted spelling.
    """
    if not isinstance(text, str) or not _B64URL_RE.match(text) or len(text) % 4 == 1:
        raise ValueError("not base64url")
    try:
        data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise ValueError("not base64url") from None
    if b64url_encode(data) != text:
        raise ValueError("not canonical base64url")
    return data


def pin_bytes(pin: bytes | str) -> bytes:
    """The pin as its 32 raw bytes, from raw bytes or its 43-character text."""
    raw = b64url_decode(pin) if isinstance(pin, str) else bytes(pin)
    if len(raw) != PIN_BYTES:
        raise ValueError("pin must be 32 bytes")
    return raw


def is_valid_id(value: object) -> bool:
    """True for a well-formed deviceId / pcId: 22 base64url characters that
    decode to exactly 16 bytes."""
    if not isinstance(value, str) or len(value) != DEVICE_ID_LENGTH:
        return False
    try:
        return len(b64url_decode(value)) == DEVICE_ID_BYTES
    except ValueError:
        return False


def load_public_key(spki_der: bytes) -> ec.EllipticCurvePublicKey:
    """Parse a phone's public key, or raise ``ValueError``.

    Accepted: a P-256 SubjectPublicKeyInfo in DER, exactly 91 bytes, holding
    an uncompressed point on the curve (``cryptography`` checks the point).
    The key must also re-encode to the very same bytes, so only the one
    canonical encoding gets in: the proofs hash these bytes.
    """
    if not isinstance(spki_der, bytes | bytearray) or len(spki_der) != PUBLIC_KEY_DER_BYTES:
        raise ValueError("invalid public key")
    data = bytes(spki_der)
    try:
        key = serialization.load_der_public_key(data)
        if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(
            key.curve, ec.SECP256R1
        ):
            raise ValueError("invalid public key")
        canonical = key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    except ValueError:
        raise ValueError("invalid public key") from None
    except Exception:  # any parser failure is the same refusal
        raise ValueError("invalid public key") from None
    if canonical != data:
        raise ValueError("invalid public key")
    return key


def clean_device_name(name: object) -> str | None:
    """The device name as it may be shown and stored, or None if unusable.

    Control and invisible formatting characters are removed, the rest is
    trimmed; the result must be 1 to 64 UTF-16 code units.
    """
    if not isinstance(name, str):
        return None
    cleaned = _NAME_STRIP_RE.sub("", name).strip()
    if not cleaned or len(cleaned.encode("utf-16-le", "surrogatepass")) // 2 > DEVICE_NAME_MAX:
        return None
    return cleaned


@dataclass(frozen=True, slots=True)
class Device:
    """One paired phone or PC. ``public_key`` is P-256 SubjectPublicKeyInfo DER."""

    device_id: str
    name: str
    public_key: bytes
    created_at: int
    last_seen_at: int
    kind: str = DEFAULT_DEVICE_KIND
    #: A PC's own pcId as it said at pairing (never changes); None for phones.
    client_pc_id: str | None = None


def auth_transcript(nonce: bytes, pin: bytes | str, device_id: str) -> bytes:
    """The bytes a phone signs to log in (PROTOCOL.md section 6)."""
    raw_pin = pin_bytes(pin)
    if len(nonce) != NONCE_BYTES:
        raise ValueError("nonce must be 32 bytes")
    if not is_valid_id(device_id):
        raise ValueError("invalid device id")
    return _AUTH_LABEL + bytes(nonce) + raw_pin + device_id.encode("ascii")


# Verified against when the device is unknown, so an unknown id costs the same
# work as a wrong signature. The private half is discarded at import.
_DUMMY_PUBLIC_KEY = ec.generate_private_key(ec.SECP256R1()).public_key()
_DUMMY_DEVICE_ID = b64url_encode(bytes(DEVICE_ID_BYTES))


def _ecdsa_verifies(key: ec.EllipticCurvePublicKey, signature: bytes, message: bytes) -> bool:
    try:
        key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    except Exception:  # malformed DER and anything else: fail closed
        return False
    return True


def verify_auth(device: Device | None, nonce: bytes, pin: bytes | str, signature: bytes) -> bool:
    """Whether ``signature`` is this device's login for this connection.

    ``device`` is the row looked up for the claimed deviceId, or None when it
    is unknown or was removed. ``nonce`` is the 32 raw bytes of this
    connection's ``hello.nonce``, ``pin`` the PC's own pin, ``signature`` the
    raw ASN.1 DER ECDSA-P256-SHA256 signature (at most 72 bytes).

    Never raises. Unknown device, removed device, bad signature and malformed
    input all return False, and each still runs one signature verification.
    """
    try:
        raw_pin = pin_bytes(pin)
        nonce_ok = isinstance(nonce, bytes | bytearray) and len(nonce) == NONCE_BYTES
        sig_ok = isinstance(signature, bytes | bytearray) and 0 < len(signature) <= (
            SIGNATURE_MAX_BYTES
        )
        key: ec.EllipticCurvePublicKey = _DUMMY_PUBLIC_KEY
        device_id = _DUMMY_DEVICE_ID
        known = False
        if device is not None and is_valid_id(device.device_id):
            try:
                key = load_public_key(device.public_key)
                device_id = device.device_id
                known = True
            except ValueError:
                known = False
        transcript = (
            _AUTH_LABEL
            + (bytes(nonce) if nonce_ok else bytes(NONCE_BYTES))
            + raw_pin
            + device_id.encode("ascii")
        )
        verified = _ecdsa_verifies(key, bytes(signature) if sig_ok else b"", transcript)
        return bool(known and nonce_ok and sig_ok and verified)
    except Exception:
        return False


class DeviceStore:
    """The paired-device list, persisted in the app database."""

    def __init__(
        self,
        db: Database,
        *,
        clock: Callable[[], float] = time.time,
        token_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self._db = db
        self._clock = clock
        self._token_bytes = token_bytes

    def _now(self) -> int:
        return int(self._clock())

    def _new_id(self) -> str:
        raw = self._token_bytes(DEVICE_ID_BYTES)
        if len(raw) != DEVICE_ID_BYTES:
            raise ValueError("random source returned the wrong length")
        return b64url_encode(raw)

    def pc_id(self) -> str:
        """This PC's stable id: 16 random bytes as 22 base64url characters,
        created on first use and kept for the life of the data directory."""
        stored = self._db.get_remote_meta(_PC_ID_KEY)
        if stored is not None and is_valid_id(stored):
            return stored
        if stored is not None:
            raise ValueError("stored pcId is malformed")
        return self._db.init_remote_meta(_PC_ID_KEY, self._new_id())

    def new_device_id(self) -> str:
        """A fresh deviceId: 16 random bytes as 22 base64url characters."""
        return self._new_id()

    def add(
        self,
        name: str,
        public_key: bytes,
        *,
        device_id: str | None = None,
        kind: str = DEFAULT_DEVICE_KIND,
        client_pc_id: str | None = None,
    ) -> Device:
        """Store a newly paired phone or PC and return its row.

        Raises ``ValueError`` for a bad name, key or id, and
        ``DeviceLimitError`` when 8 devices are already paired (or the id is
        taken). Nothing is stored in either case.
        """
        cleaned = clean_device_name(name)
        if cleaned is None:
            raise ValueError("invalid device name")
        if kind not in DEVICE_KINDS:
            raise ValueError("invalid device kind")
        if client_pc_id is not None and (kind != "pc" or not is_valid_id(client_pc_id)):
            client_pc_id = None  # only a PC has one, and only a well-formed one is kept
        load_public_key(public_key)
        new_id = self._new_id() if device_id is None else device_id
        if not is_valid_id(new_id):
            raise ValueError("invalid device id")
        now = self._now()
        if not self._db.add_remote_device(
            new_id,
            cleaned,
            bytes(public_key),
            now,
            limit=MAX_DEVICES,
            kind=kind,
            client_pc_id=client_pc_id,
        ):
            raise DeviceLimitError("device list is full")
        return Device(new_id, cleaned, bytes(public_key), now, now, kind, client_pc_id)

    def get(self, device_id: str) -> Device | None:
        if not is_valid_id(device_id):
            return None
        row = self._db.get_remote_device(device_id)
        return Device(*row) if row is not None else None

    def list(self) -> list[Device]:
        """Paired phones and PCs, oldest pairing first."""
        return [Device(*row) for row in self._db.list_remote_devices()]

    def touch(self, device_id: str) -> bool:
        """Record that the device was just seen; False if it is not paired."""
        if not is_valid_id(device_id):
            return False
        return self._db.touch_remote_device(device_id, self._now())

    def remove(self, device_id: str) -> bool:
        """Revoke a device. Returns whether it existed."""
        if not is_valid_id(device_id):
            return False
        return self._db.delete_remote_device(device_id)

    def count(self) -> int:
        return self._db.count_remote_devices()

    def is_full(self) -> bool:
        return self.count() >= MAX_DEVICES

    def authenticate(
        self, device_id: object, nonce: bytes, pin: bytes | str, signature: bytes
    ) -> Device | None:
        """Look the device up and check its login signature.

        Returns the device on success, else None; the caller answers every
        None with the one ``unauthorized`` reply. Does not update lastSeen.
        """
        device = self.get(device_id) if isinstance(device_id, str) else None
        return device if verify_auth(device, nonce, pin, signature) else None
