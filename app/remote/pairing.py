"""One pairing attempt: the one-time code, the QR content and the proofs.

Pure logic: no sockets, no Qt. PROTOCOL.md section 5. The listener feeds
``pair`` requests to :meth:`PairingSession.submit_proof`; the Settings dialog
shows the QR and calls :meth:`PairingSession.approve` or
:meth:`PairingSession.deny` when the user answers the Allow prompt.

Rules kept here: the code is 60 bits and never leaves this object except to be
drawn on screen; a session lives 120 s, accepts exactly one correct proof and
locks on the fifth wrong one; a device row is written only by ``approve``.
The code is never logged, and is dropped the moment the session stops needing
it.
"""

from __future__ import annotations

import enum
import hashlib
import hmac
import ipaddress
import secrets
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import quote

from app.remote.devices import (
    DEVICE_KINDS,
    NONCE_BYTES,
    Device,
    DeviceLimitError,
    DeviceStore,
    b64url_encode,
    clean_device_name,
    is_valid_id,
    load_public_key,
    pin_bytes,
)

CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 12
SESSION_LIFETIME_SECONDS = 120
APPROVAL_TIMEOUT_SECONDS = 60
MAX_WRONG_PROOFS = 5
PROOF_BYTES = 32
QR_VERSION = 1
MAX_ADDRESSES = 8
PORT_MIN = 1024
PORT_MAX = 65535
PC_NAME_MAX = 64

_PAIR_LABEL = b"grabline-pair-v1\x00"
_PAIRED_LABEL = b"grabline-paired-v1\x00"


class PairingState(enum.Enum):
    OPEN = "open"
    AWAITING_APPROVAL = "awaitingApproval"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    LOCKED = "locked"
    # A correct proof ended the session, but it never reached a decision:
    # the device list was full, the phone left during the prompt, or the
    # session was closed.
    USED = "used"


class ProofOutcome(enum.Enum):
    """Result of one ``pair`` request. Values other than ``ACCEPTED`` are the
    error ``code`` to reply with."""

    ACCEPTED = "accepted"  # send pairPending, show the Allow prompt
    BAD_REQUEST = "badRequest"
    PAIRING_EXPIRED = "pairingExpired"
    PAIRING_LOCKED = "pairingLocked"
    BAD_CODE = "badCode"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class ProofResult:
    outcome: ProofOutcome
    attempts_left: int | None = None  # set only for BAD_CODE


@dataclass(frozen=True, slots=True)
class PairedDevice:
    """What ``approve`` produced: the stored row and the ``paired`` proof."""

    device: Device
    server_proof: bytes


def generate_code(choice: Callable[[str], str] = secrets.choice) -> str:
    """A fresh 12-character Crockford base32 code (60 bits)."""
    code = "".join(choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    if not is_valid_code(code):
        raise ValueError("random source returned characters outside the alphabet")
    return code


def is_valid_code(code: object) -> bool:
    return (
        isinstance(code, str)
        and len(code) == CODE_LENGTH
        and all(ch in CODE_ALPHABET for ch in code)
    )


def format_code(code: str) -> str:
    """``XXXX-XXXX-XXXX``, the form shown to the user."""
    return "-".join(code[i : i + 4] for i in range(0, CODE_LENGTH, 4))


def _proof_body(nonce: bytes, pin: bytes | str, public_key: bytes) -> bytes:
    raw_pin = pin_bytes(pin)
    if len(nonce) != NONCE_BYTES:
        raise ValueError("nonce must be 32 bytes")
    return bytes(nonce) + raw_pin + hashlib.sha256(bytes(public_key)).digest()


def _code_key(code: str) -> bytes:
    if not is_valid_code(code):
        raise ValueError("invalid pairing code")
    return code.encode("ascii")


def compute_code_proof(code: str, nonce: bytes, pin: bytes | str, public_key: bytes) -> bytes:
    """``codeProof`` of PROTOCOL.md section 5.3 (what the phone sends)."""
    body = _proof_body(nonce, pin, public_key)
    return hmac.new(_code_key(code), _PAIR_LABEL + body, hashlib.sha256).digest()


def compute_server_proof(
    code: str, nonce: bytes, pin: bytes | str, public_key: bytes, device_id: str
) -> bytes:
    """``serverProof`` of PROTOCOL.md section 5.3 (what the PC answers)."""
    if not is_valid_id(device_id):
        raise ValueError("invalid device id")
    body = _proof_body(nonce, pin, public_key) + device_id.encode("ascii")
    return hmac.new(_code_key(code), _PAIRED_LABEL + body, hashlib.sha256).digest()


def _parse_address(address: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    text = address.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    text = text.split("%", 1)[0]  # the QR carries no zone id
    return ipaddress.ip_address(text)


def _order_addresses(
    addresses: Sequence[str],
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Unique addresses: IPv4 first, link-local IPv6 last, at most 8."""
    parsed: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for address in addresses:
        ip = _parse_address(address)
        if ip not in parsed:
            parsed.append(ip)

    def rank(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> int:
        if ip.version == 4:
            return 0
        return 2 if ip.is_link_local else 1

    parsed.sort(key=rank)  # stable: the caller's order is kept inside a group
    if not parsed:
        raise ValueError("at least one address is required")
    return parsed[:MAX_ADDRESSES]


def _host_text(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    return str(ip) if ip.version == 4 else f"[{ip}]"


def build_qr_uri(
    *,
    addresses: Sequence[str],
    port: int,
    pin: bytes | str,
    code: str,
    pc_id: str,
    pc_name: str,
    expires_at: int,
) -> str:
    """The QR content of PROTOCOL.md section 5.2. Raises ``ValueError`` for
    any input the format cannot carry."""
    if type(port) is not int or not PORT_MIN <= port <= PORT_MAX:
        raise ValueError("port out of range")
    if not is_valid_code(code):
        raise ValueError("invalid pairing code")
    if not is_valid_id(pc_id):
        raise ValueError("invalid pcId")
    name = clean_device_name(pc_name) if len(pc_name) <= PC_NAME_MAX else None
    if name is None:
        raise ValueError("invalid PC name")
    if type(expires_at) is not int or expires_at < 0:
        raise ValueError("invalid expiry")
    hosts = ",".join(
        _host_text(ip) if ip.version == 4 else quote(_host_text(ip), safe=":")
        for ip in _order_addresses(addresses)
    )
    return (
        f"grabline-pair:?v={QR_VERSION}&a={hosts}&p={port}"
        f"&f={b64url_encode(pin_bytes(pin))}&c={code}&i={pc_id}"
        f"&n={quote(name, safe='')}&x={expires_at}"
    )


class PairingSession:
    """One showing of the pairing QR.

    Thread-safe: the listener thread submits proofs while the UI thread reads
    the state and answers the prompt. ``clock`` returns Unix seconds;
    ``choice`` picks one character (``secrets.choice``); ``store`` supplies
    the device ids and receives the device on ``approve``.
    """

    def __init__(
        self,
        *,
        pc_id: str,
        pc_name: str,
        addresses: Sequence[str],
        port: int,
        pin: bytes | str,
        store: DeviceStore,
        clock: Callable[[], float] = time.time,
        choice: Callable[[str], str] = secrets.choice,
    ) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._store = store
        self._pin = pin_bytes(pin)
        self._code: str | None = generate_code(choice)
        self._created_at = int(clock())
        self._expires_at = self._created_at + SESSION_LIFETIME_SECONDS
        ordered = _order_addresses(addresses)
        # Built once, and it validates every input; a bad one raises here,
        # before any code is on screen.
        self._qr_uri: str | None = build_qr_uri(
            addresses=[str(ip) for ip in ordered],
            port=port,
            pin=self._pin,
            code=self._code,
            pc_id=pc_id,
            pc_name=pc_name,
            expires_at=self._expires_at,
        )
        self._manual_address = f"{_host_text(ordered[0])}:{port}"
        self._state = PairingState.OPEN
        self._wrong_proofs = 0
        self._pending: tuple[str, bytes, str, bytes, str] | None = None
        self._pending_deadline = 0.0

    # ------------------------------------------------------------ display

    @property
    def expires_at(self) -> int:
        """Unix second at which the code stops working (the QR's ``x``)."""
        return self._expires_at

    @property
    def state(self) -> PairingState:
        with self._lock:
            return self._refresh()

    @property
    def attempts_left(self) -> int:
        with self._lock:
            return MAX_WRONG_PROOFS - self._wrong_proofs

    @property
    def pending_device_kind(self) -> str:
        """``phone`` or ``pc``: what the waiting device said it is (section 21)."""
        with self._lock:
            if self._refresh() is not PairingState.AWAITING_APPROVAL or self._pending is None:
                return "phone"
            return self._pending[4]

    @property
    def pending_device_name(self) -> str | None:
        """The name to show in the Allow prompt, while one is waiting."""
        with self._lock:
            if self._refresh() is not PairingState.AWAITING_APPROVAL or self._pending is None:
                return None
            return self._pending[0]

    def qr_uri(self) -> str | None:
        """The QR content, or None once the session is no longer open."""
        with self._lock:
            return self._qr_uri if self._refresh() is PairingState.OPEN else None

    def code_display(self) -> str | None:
        """``XXXX-XXXX-XXXX``, or None once the session is no longer open."""
        with self._lock:
            if self._refresh() is not PairingState.OPEN or self._code is None:
                return None
            return format_code(self._code)

    def manual_entry_text(self) -> str | None:
        """Two lines for pairing without a camera: ``address:port`` (the first
        listed address) and the dashed code. None once not open."""
        with self._lock:
            if self._refresh() is not PairingState.OPEN or self._code is None:
                return None
            return f"{self._manual_address}\n{format_code(self._code)}"

    # -------------------------------------------------------------- state

    def _end(self, state: PairingState) -> None:
        self._state = state
        self._drop_code()
        self._pending = None

    def _drop_code(self) -> None:
        self._code = None
        self._qr_uri = None  # it carries the code

    def _refresh(self) -> PairingState:
        now = self._clock()
        code_expired = self._state is PairingState.OPEN and now >= self._expires_at
        prompt_expired = (
            self._state is PairingState.AWAITING_APPROVAL and now >= self._pending_deadline
        )
        if code_expired or prompt_expired:
            self._end(PairingState.EXPIRED)
        return self._state

    # ------------------------------------------------------------- proofs

    def submit_proof(
        self,
        *,
        nonce: bytes,
        public_key: bytes,
        code_proof: bytes,
        device_name: str,
        device_kind: str = "phone",
    ) -> ProofResult:
        """Handle one ``pair`` request (already decoded from base64url).

        ``nonce`` is the 32 raw bytes of that connection's ``hello.nonce``.
        Check order is PROTOCOL.md's: field shapes, then session, lock, proof,
        and last the device limit. Never raises.
        """
        try:
            return self._submit_proof(nonce, public_key, code_proof, device_name, device_kind)
        except Exception:
            # Fail closed: an unexpected error must not leave a usable code.
            with self._lock:
                if self._state in (PairingState.OPEN, PairingState.AWAITING_APPROVAL):
                    self._end(PairingState.USED)
            return ProofResult(ProofOutcome.PAIRING_EXPIRED)

    def _submit_proof(
        self,
        nonce: bytes,
        public_key: bytes,
        code_proof: bytes,
        device_name: str,
        device_kind: str,
    ) -> ProofResult:
        name = clean_device_name(device_name)
        shapes_ok = (
            name is not None
            and device_kind in DEVICE_KINDS
            and isinstance(nonce, bytes | bytearray)
            and len(nonce) == NONCE_BYTES
            and isinstance(code_proof, bytes | bytearray)
            and len(code_proof) == PROOF_BYTES
        )
        if shapes_ok:
            try:
                load_public_key(public_key)
            except ValueError:
                shapes_ok = False
        if not shapes_ok or name is None:
            return ProofResult(ProofOutcome.BAD_REQUEST)
        nonce = bytes(nonce)
        public_key = bytes(public_key)

        with self._lock:
            state = self._refresh()
            if state is PairingState.LOCKED:
                return ProofResult(ProofOutcome.PAIRING_LOCKED)
            if state is not PairingState.OPEN or self._code is None:
                return ProofResult(ProofOutcome.PAIRING_EXPIRED)

            expected = compute_code_proof(self._code, nonce, self._pin, public_key)
            if not hmac.compare_digest(expected, bytes(code_proof)):
                self._wrong_proofs += 1
                if self._wrong_proofs >= MAX_WRONG_PROOFS:
                    self._end(PairingState.LOCKED)
                    return ProofResult(ProofOutcome.PAIRING_LOCKED)
                return ProofResult(
                    ProofOutcome.BAD_CODE, attempts_left=MAX_WRONG_PROOFS - self._wrong_proofs
                )

            # Correct. The session is over for everyone else from here on,
            # whatever happens next.
            code = self._code
            if self._store.is_full():
                self._end(PairingState.USED)
                return ProofResult(ProofOutcome.CONFLICT)
            device_id = self._store.new_device_id()
            server_proof = compute_server_proof(code, nonce, self._pin, public_key, device_id)
            self._drop_code()
            self._pending = (name, public_key, device_id, server_proof, device_kind)
            self._pending_deadline = self._clock() + APPROVAL_TIMEOUT_SECONDS
            self._state = PairingState.AWAITING_APPROVAL
            return ProofResult(ProofOutcome.ACCEPTED)

    # ----------------------------------------------------------- decision

    def approve(self, device_name: str | None = None) -> PairedDevice | None:
        """The user pressed Allow: store the device and return it with the
        ``serverProof`` for the ``paired`` reply.

        ``device_name`` replaces the name the phone sent, if given. Returns
        None, storing nothing, unless a proof is waiting and its 60 s prompt
        has not run out (and the device list still has room).
        """
        with self._lock:
            if self._refresh() is not PairingState.AWAITING_APPROVAL or self._pending is None:
                return None
            name, public_key, device_id, server_proof, kind = self._pending
            try:
                device = self._store.add(
                    name if device_name is None else device_name,
                    public_key,
                    device_id=device_id,
                    kind=kind,
                )
            except (DeviceLimitError, ValueError):
                self._end(PairingState.USED)
                return None
            self._end(PairingState.APPROVED)
            return PairedDevice(device, server_proof)

    def deny(self) -> bool:
        """The user pressed Deny: nothing is stored. Returns whether a proof
        was waiting (then the reply is ``pairingDenied``, close 4403)."""
        with self._lock:
            if self._refresh() is not PairingState.AWAITING_APPROVAL:
                return False
            self._end(PairingState.DENIED)
            return True

    def cancel(self) -> None:
        """The dialog was closed, a new session replaced this one, or the
        phone left during the prompt: end without storing anything."""
        with self._lock:
            state = self._refresh()
            if state is PairingState.OPEN:
                self._end(PairingState.EXPIRED)
            elif state is PairingState.AWAITING_APPROVAL:
                self._end(PairingState.USED)
