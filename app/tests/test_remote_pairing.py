"""Pairing sessions, the device store and the login check (P1-D04).

Every sample value comes from ``protocol/fixtures``; the device key there is
test-only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)

from app.db.database import Database
from app.remote.devices import (
    MAX_DEVICES,
    Device,
    DeviceLimitError,
    DeviceStore,
    auth_transcript,
    b64url_decode,
    b64url_encode,
    clean_device_name,
    is_valid_id,
    load_public_key,
    pin_bytes,
    verify_auth,
)
from app.remote.pairing import (
    APPROVAL_TIMEOUT_SECONDS,
    CODE_ALPHABET,
    MAX_WRONG_PROOFS,
    SESSION_LIFETIME_SECONDS,
    PairingSession,
    PairingState,
    ProofOutcome,
    build_qr_uri,
    compute_code_proof,
    compute_server_proof,
    format_code,
    generate_code,
)

FIXTURES = Path(__file__).resolve().parents[2] / "protocol" / "fixtures"
T0 = 1794700000


def _fixture(name: str) -> dict[str, Any]:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


V = _fixture("vectors.json")
CODE: str = V["pairingCode"]
NONCE = b64url_decode(V["nonce"])
PIN_TEXT: str = V["pinnedFingerprint"]
PIN = b64url_decode(PIN_TEXT)
PC_ID: str = V["pcId"]
DEVICE_ID: str = V["deviceId"]
PUBLIC_KEY = b64url_decode(V["deviceKey"]["publicKeySpkiDer"])
CODE_PROOF = b64url_decode(V["pair"]["codeProof"])
SERVER_PROOF = b64url_decode(V["pair"]["serverProof"])
SIGNATURE = b64url_decode(V["auth"]["signatureDer"])
ADDRESSES = ["192.168.1.20", "10.8.0.3", "fd7a:115c:a1e0::20"]
PRIVATE_KEY = ec.derive_private_key(int(V["deviceKey"]["privateScalarHex"], 16), ec.SECP256R1())
WRONG_PROOF = b64url_decode(_fixture("invalid/pair.wrongCode.json")["message"]["codeProof"])


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Chooser:
    """Deals out a fixed code, one character per call."""

    def __init__(self, code: str = CODE) -> None:
        self._chars = iter(code)

    def __call__(self, alphabet: str) -> str:
        assert alphabet == CODE_ALPHABET
        return next(self._chars)


def _store(db: Database, clock: Clock, *ids: str) -> DeviceStore:
    queue = [b64url_decode(i) for i in ids]

    def token_bytes(n: int) -> bytes:
        return queue.pop(0) if queue else bytes([len(queue) + 7]) * n

    return DeviceStore(db, clock=clock, token_bytes=token_bytes)


def _session(db: Database, clock: Clock | None = None) -> tuple[PairingSession, DeviceStore, Clock]:
    clock = clock or Clock()
    store = _store(db, clock, DEVICE_ID)
    session = PairingSession(
        pc_id=PC_ID,
        pc_name="Study PC",
        addresses=ADDRESSES,
        port=47821,
        pin=PIN_TEXT,
        store=store,
        clock=clock,
        choice=Chooser(),
    )
    return session, store, clock


def _state(session: PairingSession) -> PairingState:
    return session.state  # through a call, so mypy does not narrow it between steps


def _submit(session: PairingSession, proof: bytes = CODE_PROOF, **changes: Any):
    args: dict[str, Any] = {
        "nonce": NONCE,
        "public_key": PUBLIC_KEY,
        "code_proof": proof,
        "device_name": "Pixel 8",
    }
    args.update(changes)
    return session.submit_proof(**args)


def _other_public_key() -> bytes:
    return (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    )


# ------------------------------------------------------------------ vectors


def test_vector_inputs_and_proofs_match():
    body = NONCE + PIN + bytes.fromhex(V["deviceKey"]["publicKeySpkiDerSha256Hex"])
    assert b"grabline-pair-v1\x00" + body == bytes.fromhex(V["pair"]["codeProofInputHex"])
    assert b"grabline-paired-v1\x00" + body + DEVICE_ID.encode() == bytes.fromhex(
        V["pair"]["serverProofInputHex"]
    )
    assert bytes.fromhex(V["pinnedFingerprintHex"]) == PIN
    assert compute_code_proof(CODE, NONCE, PIN, PUBLIC_KEY) == CODE_PROOF
    assert compute_code_proof(CODE, NONCE, PIN_TEXT, PUBLIC_KEY) == CODE_PROOF
    assert compute_server_proof(CODE, NONCE, PIN, PUBLIC_KEY, DEVICE_ID) == SERVER_PROOF


def test_fixture_messages_match_vectors():
    pair = _fixture("pair.request.json")["message"]
    assert b64url_decode(pair["publicKey"]) == PUBLIC_KEY
    assert b64url_decode(pair["codeProof"]) == CODE_PROOF
    paired = _fixture("pair.reply.paired.json")["message"]
    assert (paired["deviceId"], paired["pcId"]) == (DEVICE_ID, PC_ID)
    assert b64url_decode(paired["serverProof"]) == SERVER_PROOF
    auth = _fixture("auth.request.json")["message"]
    assert auth["deviceId"] == DEVICE_ID and b64url_decode(auth["signature"]) == SIGNATURE


def test_vector_auth_signature_verifies():
    assert auth_transcript(NONCE, PIN, DEVICE_ID) == bytes.fromhex(V["auth"]["transcriptHex"])
    device = Device(DEVICE_ID, "Pixel 8", PUBLIC_KEY, T0, T0)
    assert verify_auth(device, NONCE, PIN, SIGNATURE) is True
    assert verify_auth(device, NONCE, PIN_TEXT, SIGNATURE) is True


def test_wrong_code_fixture_proof_is_not_the_right_one():
    assert len(WRONG_PROOF) == 32 and WRONG_PROOF != CODE_PROOF


# ------------------------------------------------------- code, QR, manual


def test_code_format():
    code = generate_code()
    assert len(code) == 12 and set(code) <= set(CODE_ALPHABET)
    assert len(CODE_ALPHABET) == 32 and not set("ILOU") & set(CODE_ALPHABET)
    assert format_code(CODE) == V["pairingCodeDisplay"]
    with pytest.raises(ValueError):
        generate_code(lambda _alphabet: "I")


def test_codes_differ_between_sessions(db: Database):
    clock = Clock()
    codes = set()
    for _ in range(5):
        session = PairingSession(
            pc_id=PC_ID,
            pc_name="Study PC",
            addresses=ADDRESSES,
            port=47821,
            pin=PIN,
            store=DeviceStore(db, clock=clock),
            clock=clock,
        )
        codes.add(session.code_display())
    assert len(codes) == 5


def test_qr_uri_and_manual_text_match_fixtures(db: Database):
    session, _devices, _clock = _session(db)
    expected = (FIXTURES / "qr.uri.txt").read_text(encoding="utf-8").strip()
    assert session.qr_uri() == expected == V["qrUri"]
    assert session.expires_at == T0 + SESSION_LIFETIME_SECONDS
    manual = (FIXTURES / "qr.manual.txt").read_text(encoding="utf-8").split()
    text = session.manual_entry_text()
    assert text is not None and text.split("\n") == manual
    assert session.code_display() == V["pairingCodeDisplay"]


def test_qr_address_order_and_ipv6_manual_address(db: Database):
    uri = build_qr_uri(
        addresses=["fe80::1%eth0", "[fd00::2]", "192.168.1.5", "192.168.1.5"],
        port=50000,
        pin=PIN,
        code=CODE,
        pc_id=PC_ID,
        pc_name="Büro & PC",
        expires_at=T0,
    )
    assert "&a=192.168.1.5,%5Bfd00::2%5D,%5Bfe80::1%5D&p=50000&" in uri
    assert "&n=B%C3%BCro%20%26%20PC&" in uri
    clock = Clock()
    session = PairingSession(
        pc_id=PC_ID,
        pc_name="PC",
        addresses=["fd00::2"],
        port=50000,
        pin=PIN,
        store=DeviceStore(db, clock=clock),
        clock=clock,
    )
    text = session.manual_entry_text()
    assert text is not None and text.startswith("[fd00::2]:50000\n")


@pytest.mark.parametrize(
    "changes",
    [
        {"addresses": []},
        {"addresses": ["not-an-address"]},
        {"port": 80},
        {"port": 65536},
        {"port": True},
        {"pin": b"short"},
        {"pin": PIN_TEXT + "="},
        {"code": "7K3M9QX2RTBI"},
        {"pc_id": "short"},
        {"pc_name": ""},
        {"pc_name": "x" * 65},
    ],
)
def test_qr_rejects_bad_inputs(changes: dict[str, Any]):
    args: dict[str, Any] = {
        "addresses": ADDRESSES,
        "port": 47821,
        "pin": PIN,
        "code": CODE,
        "pc_id": PC_ID,
        "pc_name": "Study PC",
        "expires_at": T0,
    }
    args.update(changes)
    with pytest.raises(ValueError):
        build_qr_uri(**args)


def test_qr_lists_at_most_eight_addresses():
    uri = build_qr_uri(
        addresses=[f"10.0.0.{n}" for n in range(1, 12)],
        port=47821,
        pin=PIN,
        code=CODE,
        pc_id=PC_ID,
        pc_name="PC",
        expires_at=T0,
    )
    assert uri.split("&a=")[1].split("&")[0].count(",") == 7


# ------------------------------------------------------------ happy path


def test_pair_approve_stores_device_and_returns_vector_server_proof(db: Database):
    session, store, clock = _session(db)
    assert _state(session) is PairingState.OPEN
    result = _submit(session)
    assert result.outcome is ProofOutcome.ACCEPTED and result.attempts_left is None
    assert _state(session) is PairingState.AWAITING_APPROVAL
    assert session.pending_device_name == "Pixel 8"
    assert store.count() == 0  # nothing stored before Allow
    clock.now += 5
    paired = session.approve()
    assert paired is not None
    assert paired.server_proof == SERVER_PROOF
    assert paired.device == Device(DEVICE_ID, "Pixel 8", PUBLIC_KEY, T0 + 5, T0 + 5)
    assert _state(session) is PairingState.APPROVED
    assert store.get(DEVICE_ID) == paired.device
    assert store.authenticate(DEVICE_ID, NONCE, PIN, SIGNATURE) == paired.device
    assert session.approve() is None and store.count() == 1  # only once


def test_approve_can_rename_and_cleans_the_name(db: Database):
    session, store, _clock = _session(db)
    assert _submit(session, device_name="  Pix\x00el\u202e 8\n").outcome is ProofOutcome.ACCEPTED
    assert session.pending_device_name == "Pixel 8"
    paired = session.approve("Kitchen phone")
    assert paired is not None and paired.device.name == "Kitchen phone"
    assert store.list()[0].name == "Kitchen phone"


def test_code_and_qr_disappear_once_a_proof_is_accepted(db: Database):
    session, _devices, _clock = _session(db)
    _submit(session)
    assert session.qr_uri() is None
    assert session.code_display() is None
    assert session.manual_entry_text() is None
    assert CODE not in repr(vars(session))


# ---------------------------------------------------------------- expiry


def test_expiry_boundary(db: Database):
    session, _devices, clock = _session(db)
    clock.now = T0 + SESSION_LIFETIME_SECONDS - 0.001
    assert _state(session) is PairingState.OPEN
    assert _submit(session).outcome is ProofOutcome.ACCEPTED

    session2, store2, clock2 = _session(db, Clock())
    clock2.now = T0 + SESSION_LIFETIME_SECONDS  # the expiry second itself is too late
    assert _submit(session2).outcome is ProofOutcome.PAIRING_EXPIRED
    assert _state(session2) is PairingState.EXPIRED
    assert session2.qr_uri() is None and session2.code_display() is None
    assert session2.approve() is None and session2.deny() is False
    assert store2.count() == 0


def test_expiry_fixture_correct_proof_after_two_minutes(db: Database):
    message = _fixture("invalid/pair.expired.json")["message"]
    session, store, clock = _session(db)
    clock.now += 121
    result = session.submit_proof(
        nonce=NONCE,
        public_key=b64url_decode(message["publicKey"]),
        code_proof=b64url_decode(message["codeProof"]),
        device_name=message["deviceName"],
    )
    assert result.outcome.value == _fixture("invalid/pair.expired.json")["expectError"]
    assert store.count() == 0


def test_wrong_proof_after_expiry_is_expired_not_bad_code(db: Database):
    session, _devices, clock = _session(db)
    clock.now += SESSION_LIFETIME_SECONDS
    assert _submit(session, WRONG_PROOF).outcome is ProofOutcome.PAIRING_EXPIRED


def test_prompt_times_out_after_60_seconds(db: Database):
    session, store, clock = _session(db)
    clock.now += 119
    assert _submit(session).outcome is ProofOutcome.ACCEPTED
    clock.now += APPROVAL_TIMEOUT_SECONDS - 1  # past the code's 120 s, inside the prompt's 60 s
    assert _state(session) is PairingState.AWAITING_APPROVAL
    clock.now += 1
    assert _state(session) is PairingState.EXPIRED
    assert session.pending_device_name is None
    assert session.approve() is None and session.deny() is False
    assert store.count() == 0


# ------------------------------------------------------- lockout (QA-03)


def test_wrong_proofs_count_down_and_fifth_locks(db: Database):
    session, store, _clock = _session(db)
    fixture = _fixture("invalid/pair.wrongCode.json")
    for left in (4, 3, 2, 1):
        result = _submit(session, WRONG_PROOF)
        assert result.outcome.value == fixture["expectError"] == "badCode"
        assert result.attempts_left == left == session.attempts_left
        assert _state(session) is PairingState.OPEN
    fifth = _submit(session, WRONG_PROOF)
    assert fifth.outcome is ProofOutcome.PAIRING_LOCKED and fifth.attempts_left is None
    assert _state(session) is PairingState.LOCKED
    assert MAX_WRONG_PROOFS == 5
    # The right code no longer works, and neither does anything else.
    assert _submit(session).outcome is ProofOutcome.PAIRING_LOCKED
    assert _submit(session, WRONG_PROOF).outcome is ProofOutcome.PAIRING_LOCKED
    assert session.qr_uri() is None and session.code_display() is None
    assert session.approve() is None and session.deny() is False
    assert store.count() == 0


def test_locked_stays_locked_after_the_two_minutes(db: Database):
    session, _devices, clock = _session(db)
    for _ in range(MAX_WRONG_PROOFS):
        _submit(session, WRONG_PROOF)
    clock.now += 500
    assert _state(session) is PairingState.LOCKED
    assert _submit(session).outcome is ProofOutcome.PAIRING_LOCKED


def test_correct_proof_after_four_wrong_still_pairs(db: Database):
    session, _devices, _clock = _session(db)
    for _ in range(MAX_WRONG_PROOFS - 1):
        _submit(session, WRONG_PROOF)
    assert _submit(session).outcome is ProofOutcome.ACCEPTED


@pytest.mark.parametrize(
    "changes",
    [
        {"nonce": bytes(32)},  # a proof made for another connection
        {"public_key": None},  # a proof made for another phone key (filled in below)
    ],
)
def test_proof_is_bound_to_nonce_and_key(db: Database, changes: dict[str, Any]):
    if changes.get("public_key", b"") is None:
        changes = {"public_key": _other_public_key()}
    session, _devices, _clock = _session(db)
    assert _submit(session, **changes).outcome is ProofOutcome.BAD_CODE


def test_proof_is_bound_to_the_pin(db: Database):
    clock = Clock()
    session = PairingSession(
        pc_id=PC_ID,
        pc_name="Study PC",
        addresses=ADDRESSES,
        port=47821,
        pin=bytes(32),  # a machine in the middle has another key
        store=DeviceStore(db, clock=clock),
        clock=clock,
        choice=Chooser(),
    )
    assert _submit(session).outcome is ProofOutcome.BAD_CODE


# ------------------------------------------------------------ single use


def test_second_correct_proof_gets_pairing_expired(db: Database):
    session, store, _clock = _session(db)
    assert _submit(session).outcome is ProofOutcome.ACCEPTED
    assert _submit(session).outcome is ProofOutcome.PAIRING_EXPIRED
    assert _submit(session, WRONG_PROOF).outcome is ProofOutcome.PAIRING_EXPIRED
    assert _state(session) is PairingState.AWAITING_APPROVAL  # the first one still waits
    assert session.approve() is not None
    assert _submit(session).outcome is ProofOutcome.PAIRING_EXPIRED
    assert store.count() == 1


# ----------------------------------------------------------- deny (QA-04)


def test_deny_stores_nothing(db: Database):
    session, store, _clock = _session(db)
    assert session.deny() is False  # nothing to deny yet
    assert _state(session) is PairingState.OPEN
    _submit(session)
    assert session.deny() is True
    assert _state(session) is PairingState.DENIED
    assert store.count() == 0 and store.list() == [] and store.get(DEVICE_ID) is None
    assert db.count_remote_devices() == 0
    assert session.approve() is None  # Allow after Deny does nothing
    assert _submit(session).outcome is ProofOutcome.PAIRING_EXPIRED
    assert store.count() == 0
    assert store.authenticate(DEVICE_ID, NONCE, PIN, SIGNATURE) is None


def test_cancel_stores_nothing(db: Database):
    session, store, _clock = _session(db)
    session.cancel()
    assert _state(session) is PairingState.EXPIRED
    assert _submit(session).outcome is ProofOutcome.PAIRING_EXPIRED

    session2, store2, _clock2 = _session(db)
    _submit(session2)
    session2.cancel()  # the phone left during the prompt
    assert _state(session2) is PairingState.USED
    assert session2.approve() is None
    assert store.count() == 0 and store2.count() == 0


# ------------------------------------------------------- malformed input


def test_bad_public_key_fixture_is_bad_request_and_costs_no_attempt(db: Database):
    fixture = _fixture("invalid/pair.badPublicKey.json")
    message = fixture["message"]
    session, _devices, _clock = _session(db)
    result = session.submit_proof(
        nonce=NONCE,
        public_key=b64url_decode(message["publicKey"]),
        code_proof=b64url_decode(message["codeProof"]),
        device_name=message["deviceName"],
    )
    assert result.outcome.value == fixture["expectError"] == "badRequest"
    assert session.attempts_left == MAX_WRONG_PROOFS
    assert _state(session) is PairingState.OPEN


@pytest.mark.parametrize(
    "changes",
    [
        {"nonce": NONCE[:31]},
        {"nonce": "x" * 32},
        {"code_proof": CODE_PROOF[:31]},
        {"code_proof": CODE_PROOF + b"\x00"},
        {"code_proof": None},
        {"device_name": ""},
        {"device_name": "\x00\x01"},
        {"device_name": "x" * 65},
        {"device_name": 7},
        {"public_key": PUBLIC_KEY[:-1]},
        {"public_key": "text"},
    ],
)
def test_malformed_pair_fields_are_bad_request(db: Database, changes: dict[str, Any]):
    session, _devices, _clock = _session(db)
    assert _submit(session, **changes).outcome is ProofOutcome.BAD_REQUEST
    assert _state(session) is PairingState.OPEN and session.attempts_left == MAX_WRONG_PROOFS


def _point_der(x: int, y: int) -> bytes:
    return PUBLIC_KEY[:27] + x.to_bytes(32, "big") + y.to_bytes(32, "big")


def test_public_key_validation():
    assert isinstance(load_public_key(PUBLIC_KEY), ec.EllipticCurvePublicKey)
    numbers = load_public_key(PUBLIC_KEY).public_numbers()
    assert _point_der(numbers.x, numbers.y) == PUBLIC_KEY
    bad: list[Any] = [
        b"",
        b"FAKE-KEY",
        PUBLIC_KEY[:-1],
        PUBLIC_KEY + b"\x00",
        _point_der(numbers.x, numbers.y ^ 1),  # not on the curve
        _point_der(0, 0),
        PUBLIC_KEY[:26] + b"\x02" + PUBLIC_KEY[27:],  # claims a compressed point
        b64url_encode(PUBLIC_KEY),  # text, not bytes
        None,
    ]
    other_curves: list[ec.EllipticCurve] = [ec.SECP384R1(), ec.SECP256K1()]
    for curve in other_curves:
        bad.append(
            ec.generate_private_key(curve)
            .public_key()
            .public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )
    for value in bad:
        with pytest.raises(ValueError):
            load_public_key(value)


def test_base64url_is_strict():
    assert b64url_decode(b64url_encode(b"\x00\xff\xfe")) == b"\x00\xff\xfe"
    for text in ("AA==", "A", "+/8A", "AA A", "AB", "AAECAw\n"):
        with pytest.raises(ValueError):
            b64url_decode(text)
    assert pin_bytes(PIN_TEXT) == PIN == pin_bytes(PIN)
    assert is_valid_id(DEVICE_ID) and is_valid_id(PC_ID)
    assert not is_valid_id(DEVICE_ID[:-1]) and not is_valid_id(DEVICE_ID[:-1] + "x")
    assert not is_valid_id(None) and not is_valid_id(DEVICE_ID + "A")


def test_clean_device_name():
    assert clean_device_name(" Pixel 8 ") == "Pixel 8"
    assert clean_device_name("a\u200eb\x1b[31m") == "ab[31m"
    assert clean_device_name("x" * 64) == "x" * 64
    assert clean_device_name("\U0001f600" * 33) is None  # 66 UTF-16 units
    assert clean_device_name(None) is None


# ---------------------------------------------------------- device store


def test_device_devicesroundtrip(db: Database):
    clock = Clock()
    store = DeviceStore(db, clock=clock)
    assert store.count() == 0 and store.list() == []
    first = store.add("Pixel 8", PUBLIC_KEY)
    assert is_valid_id(first.device_id)
    assert (first.created_at, first.last_seen_at) == (T0, T0)
    clock.now += 10
    other_key = _other_public_key()
    second = store.add("Tablet", other_key, device_id=DEVICE_ID)
    assert store.count() == 2
    assert store.list() == [first, second]
    assert store.get(DEVICE_ID) == second
    assert store.get("uD_okqTpI9aCLgizY9Xbdg") is None and store.get("bad id") is None

    clock.now += 90
    assert store.touch(DEVICE_ID) is True
    touched = store.get(DEVICE_ID)
    assert touched is not None
    assert (touched.created_at, touched.last_seen_at) == (T0 + 10, T0 + 100)
    assert store.touch("uD_okqTpI9aCLgizY9Xbdg") is False

    assert store.remove(DEVICE_ID) is True
    assert store.remove(DEVICE_ID) is False  # already gone
    assert store.remove("nonsense") is False
    assert store.get(DEVICE_ID) is None and store.count() == 1
    assert store.touch(DEVICE_ID) is False


def test_device_devicespersists_across_reopen(tmp_path: Path):
    path = tmp_path / "devices.db"
    first = Database(path)
    store = DeviceStore(first, clock=Clock())
    pc_id = store.pc_id()
    device = store.add("Pixel 8", PUBLIC_KEY, device_id=DEVICE_ID)
    first.close()

    second = Database(path)
    try:
        reopened = DeviceStore(second, clock=Clock(T0 + 1000))
        assert reopened.list() == [device]
        assert reopened.pc_id() == pc_id
        assert reopened.authenticate(DEVICE_ID, NONCE, PIN, SIGNATURE) == device
    finally:
        second.close()


def test_device_devicesrejects_bad_rows(db: Database):
    store = DeviceStore(db, clock=Clock())
    with pytest.raises(ValueError):
        store.add("Pixel 8", b"FAKE-KEY")
    with pytest.raises(ValueError):
        store.add("\x00", PUBLIC_KEY)
    with pytest.raises(ValueError):
        store.add("Pixel 8", PUBLIC_KEY, device_id="not-an-id")
    store.add("Pixel 8", PUBLIC_KEY, device_id=DEVICE_ID)
    with pytest.raises(DeviceLimitError):
        store.add("Again", PUBLIC_KEY, device_id=DEVICE_ID)  # the id is taken
    assert [d.name for d in store.list()] == ["Pixel 8"]


def test_at_most_eight_devices(db: Database):
    clock = Clock()
    store = DeviceStore(db, clock=clock)
    for n in range(MAX_DEVICES):
        store.add(f"Phone {n}", PUBLIC_KEY)
    assert store.count() == MAX_DEVICES == 8 and store.is_full()
    with pytest.raises(DeviceLimitError):
        store.add("Ninth", PUBLIC_KEY)
    assert store.count() == 8

    # A ninth pairing is refused before the prompt, and the code is spent.
    session = PairingSession(
        pc_id=PC_ID,
        pc_name="Study PC",
        addresses=ADDRESSES,
        port=47821,
        pin=PIN,
        store=store,
        clock=clock,
        choice=Chooser(),
    )
    assert (
        _submit(session, WRONG_PROOF).outcome is ProofOutcome.BAD_CODE
    )  # no hint without the code
    assert _submit(session).outcome is ProofOutcome.CONFLICT
    assert _state(session) is PairingState.USED
    assert _submit(session).outcome is ProofOutcome.PAIRING_EXPIRED
    assert store.count() == 8


def test_list_fills_between_proof_and_allow(db: Database):
    session, store, _clock = _session(db)
    _submit(session)
    filler = DeviceStore(db)
    for n in range(MAX_DEVICES):
        filler.add(f"Phone {n}", _other_public_key())
    assert session.approve() is None
    assert _state(session) is PairingState.USED
    assert store.count() == MAX_DEVICES and store.get(DEVICE_ID) is None


def test_pc_id_is_stable_and_outlives_a_settings_reset(db: Database):
    store = DeviceStore(db)
    pc_id = store.pc_id()
    assert is_valid_id(pc_id) and len(pc_id) == 22
    assert store.pc_id() == pc_id
    assert DeviceStore(db).pc_id() == pc_id
    db.reset_settings()
    assert store.pc_id() == pc_id
    assert pc_id not in db.all_settings().values()  # not exported with settings


def test_pc_id_uses_the_injected_rng(db: Database):
    store = _store(db, Clock(), PC_ID)
    assert store.pc_id() == PC_ID


# ------------------------------------------------------------------ auth


def _paired(db: Database) -> tuple[DeviceStore, Device]:
    store = DeviceStore(db, clock=Clock())
    return store, store.add("Pixel 8", PUBLIC_KEY, device_id=DEVICE_ID)


def test_auth_accepts_a_fresh_signature_from_the_device_key(db: Database):
    store, device = _paired(db)
    nonce = bytes(range(32, 64))
    signature = PRIVATE_KEY.sign(auth_transcript(nonce, PIN, DEVICE_ID), ec.ECDSA(hashes.SHA256()))
    assert verify_auth(device, nonce, PIN, signature) is True
    assert store.authenticate(DEVICE_ID, nonce, PIN, signature) == device
    assert verify_auth(device, NONCE, PIN, signature) is False  # not replayable on another nonce


def test_auth_bad_signature_fixture_fails(db: Database):
    store, device = _paired(db)
    message = _fixture("invalid/auth.badSignature.json")["message"]
    signature = b64url_decode(message["signature"])
    assert verify_auth(device, NONCE, PIN, signature) is False
    assert store.authenticate(message["deviceId"], NONCE, PIN, signature) is None


def test_auth_unknown_device_fixture_fails(db: Database):
    store, device = _paired(db)
    message = _fixture("invalid/auth.unknownDevice.json")["message"]
    signature = b64url_decode(message["signature"])
    assert store.get(message["deviceId"]) is None
    assert store.authenticate(message["deviceId"], NONCE, PIN, signature) is None
    assert verify_auth(None, NONCE, PIN, signature) is False
    # The right key under another id is still not that device.
    renamed = Device(message["deviceId"], device.name, device.public_key, T0, T0)
    assert verify_auth(renamed, NONCE, PIN, signature) is False


def test_auth_revoked_device_fails(db: Database):
    store, device = _paired(db)
    assert store.authenticate(DEVICE_ID, NONCE, PIN, SIGNATURE) == device
    assert store.remove(DEVICE_ID) is True
    assert store.authenticate(DEVICE_ID, NONCE, PIN, SIGNATURE) is None
    assert verify_auth(store.get(DEVICE_ID), NONCE, PIN, SIGNATURE) is False


def test_auth_is_bound_to_nonce_pin_and_key(db: Database):
    _devices, device = _paired(db)
    assert verify_auth(device, bytes(32), PIN, SIGNATURE) is False
    assert verify_auth(device, NONCE, bytes(32), SIGNATURE) is False  # made for another PC
    stranger = Device(DEVICE_ID, "Pixel 8", _other_public_key(), T0, T0)
    assert verify_auth(stranger, NONCE, PIN, SIGNATURE) is False


def test_auth_signature_must_be_der(db: Database):
    _devices, device = _paired(db)
    r, s = decode_dss_signature(SIGNATURE)
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")  # the raw r||s form is not accepted
    assert verify_auth(device, NONCE, PIN, raw) is False
    assert verify_auth(device, NONCE, PIN, encode_dss_signature(r, s)) is True
    for signature in (b"", SIGNATURE[:-1], SIGNATURE + b"\x00", b"\x30\x00", bytes(73)):
        assert verify_auth(device, NONCE, PIN, signature) is False


def test_auth_never_raises_on_malformed_input(db: Database):
    store, device = _paired(db)
    bad: Any = None
    assert verify_auth(device, bad, PIN, SIGNATURE) is False
    assert verify_auth(device, NONCE[:5], PIN, SIGNATURE) is False
    assert verify_auth(device, NONCE, b"short", SIGNATURE) is False
    assert verify_auth(device, NONCE, "not a pin", SIGNATURE) is False
    assert verify_auth(device, NONCE, PIN, bad) is False
    assert store.authenticate(bad, NONCE, PIN, SIGNATURE) is None
    assert store.authenticate(7, NONCE, PIN, SIGNATURE) is None
    assert store.authenticate("", NONCE, PIN, SIGNATURE) is None


def test_auth_rejects_a_stored_key_that_is_malformed(db: Database):
    # A row that was tampered with on disk must not verify anything.
    for key in (b"FAKE-KEY", PUBLIC_KEY[:-1], b""):
        assert (
            verify_auth(Device(DEVICE_ID, "Pixel 8", key, T0, T0), NONCE, PIN, SIGNATURE) is False
        )
    db.add_remote_device(DEVICE_ID, "Pixel 8", b"FAKE-KEY", T0, limit=8)
    assert DeviceStore(db).authenticate(DEVICE_ID, NONCE, PIN, SIGNATURE) is None


def test_old_client_fixture_signature_is_valid_so_the_version_check_decides(db: Database):
    # invalid/auth.oldClient.json fails on protocolVersion (the listener's
    # check, after these two); its signature itself is the good one.
    store, device = _paired(db)
    message = _fixture("invalid/auth.oldClient.json")["message"]
    signature = b64url_decode(message["signature"])
    assert store.authenticate(message["deviceId"], NONCE, PIN, signature) == device
    assert message["protocolVersion"] < 6
