"""Linking to another PC with its typed code (PROTOCOL.md sections 5.3, 5.4, 21).

A PC has no camera, so this is always the manual form: the address
(``host[:port]``) and the 12-character code the other PC shows. Trust on first
use, exactly as section 5.4:

* the TLS key the other PC presents is recorded as the candidate pin and goes
  into ``codeProof``; a machine in the middle has another key, so its relayed
  proof fails on the real PC and it cannot make a ``serverProof`` for us;
* the candidate becomes the pin only after ``serverProof`` verifies, in
  constant time; if it does not, nothing is returned and nothing is stored.

``pair`` is blocking: it runs on a worker thread (``LinkService`` starts it)
and only returns a result. It never writes a file, a database row or a log
line; the caller stores the link when, and only when, the result is
``Paired``. The code, the proofs, the nonce and the key are never logged.
"""

from __future__ import annotations

import hmac
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.native_host.host import PROTOCOL_VERSION
from app.remote.client import keys
from app.remote.client.transport import (
    Address,
    ConnectionClosed,
    IdentityMismatch,
    TransportError,
    WsConnection,
    parse_address,
)
from app.remote.devices import b64url_decode, b64url_encode, clean_device_name, is_valid_id
from app.remote.pairing import (
    CODE_ALPHABET,
    CODE_LENGTH,
    compute_code_proof,
    compute_server_proof,
)

REQUEST_ID = "pair-1"
PAIR_TIMEOUT_S = 90.0  # section 2: the reply to ``pair`` may wait for the Allow prompt
HELLO_TIMEOUT_S = 10.0
MIN_HOST_PROTOCOL = 6
MAX_IGNORED_FRAMES = 8

#: Failure codes (``PairFailed.code``); the UI words them.
BAD_ADDRESS = "badAddress"
BAD_CODE = "badCode"  # the other PC did not accept the proof
LOCKED = "locked"
EXPIRED = "expired"
DECLINED = "declined"
HOST_TOO_OLD = "hostTooOld"
CLIENT_TOO_OLD = "clientTooOld"
NOT_FOUND = "notFound"
TIMEOUT = "timeout"
PROOF_MISMATCH = "proofMismatch"
CONNECTION_LOST = "connectionLost"
FULL = "full"
REJECTED = "rejected"
CANCELLED = "cancelled"

_CODE_FIX = str.maketrans({"O": "0", "I": "1", "L": "1"})
_NO_SEPARATORS = re.compile(r"[\s-]")


def normalise_code(text: str) -> str | None:
    """Section 5.4: remove spaces and dashes, upper-case, ``O`` to ``0``, ``I``
    and ``L`` to ``1``; the result must be 12 characters of the alphabet."""
    code = _NO_SEPARATORS.sub("", text).upper().translate(_CODE_FIX)
    if len(code) != CODE_LENGTH or any(ch not in CODE_ALPHABET for ch in code):
        return None
    return code


@dataclass(frozen=True)
class Paired:
    """``serverProof`` verified. The caller stores this, after which it logs in."""

    device_id: str
    pc_id: str
    pc_name: str
    pin: str
    address: str  # the host part that worked, as typed (no port)
    port: int
    key: keys.DeviceKey = field(repr=False)


@dataclass(frozen=True)
class PairFailed:
    code: str
    attempts_left: int | None = None


PairResult = Paired | PairFailed


def _hello_problem(hello: dict[str, object]) -> str | None:
    version = hello.get("protocolVersion")
    if hello.get("type") != "hello" or type(version) is not int or version < MIN_HOST_PROTOCOL:
        return HOST_TOO_OLD
    minimum = hello.get("minClientVersion")
    if type(minimum) is int and minimum > PROTOCOL_VERSION:
        return CLIENT_TOO_OLD
    return None


def _refused(error: dict[str, object]) -> PairFailed:
    code = error.get("code")
    if code == "pairingExpired":
        return PairFailed(EXPIRED)
    if code == "pairingLocked":
        return PairFailed(LOCKED)
    if code == "pairingDenied":
        return PairFailed(DECLINED)
    if code == "badCode":
        left = error.get("attemptsLeft")
        return PairFailed(BAD_CODE, left if type(left) is int and left >= 0 else None)
    if code == "versionTooOld":
        return PairFailed(CLIENT_TOO_OLD)
    if code == "conflict":
        return PairFailed(FULL)
    return PairFailed(REJECTED)


def _closed(code: int | None) -> PairFailed:
    if code == 4403:
        return PairFailed(DECLINED)
    if code == 4426:
        return PairFailed(CLIENT_TOO_OLD)
    return PairFailed(CONNECTION_LOST)


def pair(
    address_text: str,
    code_text: str,
    device_name: str,
    *,
    app_version: str | None = None,
    on_progress: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
    timeout: float = PAIR_TIMEOUT_S,
    hello_timeout: float = HELLO_TIMEOUT_S,
    connect_timeout: float = 2.0,
) -> PairResult:
    """Link to the PC at ``address_text`` with the code it shows.

    ``on_progress`` gets ``"connecting"``, ``"sending"`` and ``"confirm"`` (the
    other PC is asking its user to press Allow). ``cancel`` stops the attempt.
    Every outcome except ``Paired`` leaves nothing behind."""
    address = parse_address(address_text)
    code = normalise_code(code_text)
    if address is None:
        return PairFailed(BAD_ADDRESS)
    if code is None:
        return PairFailed(BAD_CODE)  # the caller words "doesn't look right" itself
    name = clean_device_name(device_name) or "GrabLine PC"

    def note(text: str) -> None:
        if on_progress is not None:
            on_progress(text)

    def stopped() -> bool:
        return cancel is not None and cancel.is_set()

    note("connecting")
    try:
        connection = WsConnection.open(address, pin=None, timeout=connect_timeout)
    except IdentityMismatch:  # cannot happen without a pin; fail closed anyway
        return PairFailed(PROOF_MISMATCH)
    except TransportError:
        return PairFailed(NOT_FOUND)
    try:
        return _exchange(
            connection, address, code, name, app_version, note, stopped, timeout, hello_timeout
        )
    finally:
        connection.close()


def _exchange(
    connection: WsConnection,
    address: Address,
    code: str,
    device_name: str,
    app_version: str | None,
    note: Callable[[str], None],
    stopped: Callable[[], bool],
    timeout: float,
    hello_timeout: float,
) -> PairResult:
    pin = connection.peer_pin  # the candidate: it becomes the pin only if the proofs verify
    hello = _wait_for(connection, stopped, hello_timeout, lambda m: m.get("type") == "hello")
    if isinstance(hello, PairFailed):
        return hello
    problem = _hello_problem(hello)
    if problem is not None:
        return PairFailed(problem)
    try:
        nonce = b64url_decode(str(hello.get("nonce")))
        if len(nonce) != 32:
            return PairFailed(REJECTED)
    except ValueError:
        return PairFailed(REJECTED)
    pc_id = hello.get("pcId")
    if not isinstance(pc_id, str) or not is_valid_id(pc_id):
        return PairFailed(REJECTED)

    key = keys.new_key()  # in memory only until the pairing is verified
    public = key.public_der
    proof = compute_code_proof(code, nonce, pin, public)
    request: dict[str, object] = {
        "id": REQUEST_ID,
        "type": "pair",
        "protocolVersion": PROTOCOL_VERSION,
        "deviceName": device_name,
        "publicKey": b64url_encode(public),
        "codeProof": b64url_encode(proof),
        "deviceKind": "pc",
    }
    if app_version:
        request["appVersion"] = app_version[:32]
    note("sending")
    try:
        connection.send_json(request)
    except TransportError:
        return PairFailed(CONNECTION_LOST)

    deadline = time.monotonic() + timeout
    ignored = 0
    while True:
        if stopped():
            return PairFailed(CANCELLED)
        if time.monotonic() > deadline:
            return PairFailed(TIMEOUT)
        try:
            messages = connection.read(0.2)
        except ConnectionClosed as closed:
            return _closed(closed.code)
        except TransportError:
            return PairFailed(CONNECTION_LOST)
        for message in messages:
            kind = message.get("type")
            if kind == "pairPending":
                note("confirm")
            elif kind == "error" and message.get("id") in (None, REQUEST_ID):
                return _refused(message)
            elif kind == "paired" and message.get("id") == REQUEST_ID:
                return _verify(message, hello, code, nonce, pin, public, pc_id, address, key)
            else:
                ignored += 1
        if ignored > MAX_IGNORED_FRAMES:
            return PairFailed(REJECTED)


def _wait_for(
    connection: WsConnection,
    stopped: Callable[[], bool],
    timeout: float,
    wanted: Callable[[dict[str, object]], bool],
) -> dict[str, object] | PairFailed:
    deadline = time.monotonic() + timeout
    while time.monotonic() <= deadline:
        if stopped():
            return PairFailed(CANCELLED)
        try:
            for message in connection.read(0.2):
                if wanted(message):
                    return message
        except ConnectionClosed as closed:
            return _closed(closed.code)
        except TransportError:
            return PairFailed(CONNECTION_LOST)
    return PairFailed(TIMEOUT)


def _verify(
    reply: dict[str, object],
    hello: dict[str, object],
    code: str,
    nonce: bytes,
    pin: str,
    public: bytes,
    pc_id: str,
    address: Address,
    key: keys.DeviceKey,
) -> PairResult:
    """``serverProof`` first, in constant time, before anything from the
    reply is used. A wrong proof means the other side does not know the code."""
    device_id = reply.get("deviceId")
    proof_text = reply.get("serverProof")
    if not isinstance(device_id, str) or not is_valid_id(device_id):
        return PairFailed(PROOF_MISMATCH)
    try:
        proof = b64url_decode(str(proof_text))
        expected = compute_server_proof(code, nonce, pin, public, device_id)
    except ValueError:
        return PairFailed(PROOF_MISMATCH)
    if not hmac.compare_digest(proof, expected) or reply.get("pcId") != pc_id:
        return PairFailed(PROOF_MISMATCH)
    pc_name = clean_device_name(reply.get("pcName")) or clean_device_name(hello.get("pcName"))
    return Paired(
        device_id=device_id,
        pc_id=pc_id,
        pc_name=pc_name or "PC",
        pin=pin,
        address=address.host,
        port=address.port,
        key=key,
    )
