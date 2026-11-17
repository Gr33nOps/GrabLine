"""``deviceKind`` on the host side (PROTOCOL.md section 21): validation in
``pair`` and ``auth``, the stored kind, the database migration and the limit
of 8 devices counting PCs and phones together."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.db.database import Database
from app.remote.devices import MAX_DEVICES, DeviceLimitError, DeviceStore, b64url_decode
from app.remote.pairing import PairingSession, ProofOutcome, compute_code_proof
from app.remote.schema import RequestError, validate

FIXTURES = Path(__file__).resolve().parents[2] / "protocol" / "fixtures"


def _fixture(name: str) -> dict[str, Any]:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


V = _fixture("vectors.json")
PUBLIC_KEY = b64url_decode(V["deviceKey"]["publicKeySpkiDer"])
PIN = V["pinnedFingerprint"]
NONCE = b64url_decode(V["nonce"])
CODE = V["pairingCode"]


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "kind.db")


# ------------------------------------------------------------------ schema


@pytest.mark.parametrize("name", ["pair.request.json", "auth.request.json"])
def test_a_request_without_a_kind_is_a_phone(name: str) -> None:
    request = validate(_fixture(name)["message"])
    assert request.fields["deviceKind"] == "phone"


@pytest.mark.parametrize("name", ["pair.request.pc.json", "auth.request.pc.json"])
def test_the_pc_fixtures_validate_as_pc(name: str) -> None:
    request = validate(_fixture(name)["message"])
    assert request.fields["deviceKind"] == "pc"


@pytest.mark.parametrize("kind", ["tablet", "", "PC", "Phone", 1, True, ["pc"], {"a": 1}])
@pytest.mark.parametrize("name", ["pair.request.json", "auth.request.json"])
def test_any_other_kind_is_bad_request(name: str, kind: Any) -> None:
    message = _fixture(name)["message"]
    message["deviceKind"] = kind
    with pytest.raises(RequestError) as caught:
        validate(message)
    assert caught.value.code == "badRequest"


def test_explicit_phone_and_null_are_accepted() -> None:
    message = _fixture("pair.request.json")["message"]
    message["deviceKind"] = "phone"
    assert validate(message).fields["deviceKind"] == "phone"
    message["deviceKind"] = None
    assert validate(message).fields["deviceKind"] == "phone"


# ------------------------------------------------------------ the store


def test_the_store_keeps_the_kind(db: Database) -> None:
    store = DeviceStore(db)
    pc = store.add("LAPTOP", PUBLIC_KEY, kind="pc")
    assert pc.kind == "pc"
    stored = store.get(pc.device_id)
    assert stored is not None and stored.kind == "pc"
    assert [d.kind for d in store.list()] == ["pc"]


def test_a_device_is_a_phone_unless_told_otherwise(db: Database) -> None:
    store = DeviceStore(db)
    assert store.add("Pixel", PUBLIC_KEY).kind == "phone"


def test_an_unknown_kind_is_refused_by_the_store(db: Database) -> None:
    with pytest.raises(ValueError):
        DeviceStore(db).add("X", PUBLIC_KEY, kind="toaster")
    assert DeviceStore(db).count() == 0


def test_the_limit_counts_pcs_and_phones_together(db: Database) -> None:
    store = DeviceStore(db)
    for index in range(MAX_DEVICES):
        store.add(f"d{index}", PUBLIC_KEY, kind="pc" if index % 2 else "phone")
    assert store.is_full()
    with pytest.raises(DeviceLimitError):
        store.add("ninth", PUBLIC_KEY, kind="pc")


# ------------------------------------------------------- the pairing session


def _session(db: Database) -> PairingSession:
    chars = iter(CODE)
    return PairingSession(
        pc_id=V["pcId"],
        pc_name="Study PC",
        addresses=["192.168.1.20"],
        port=47821,
        pin=PIN,
        store=DeviceStore(db),
        choice=lambda _alphabet: next(chars),
    )


def test_the_allow_prompt_knows_the_kind_and_approval_stores_it(db: Database) -> None:
    session = _session(db)
    proof = compute_code_proof(CODE, NONCE, PIN, PUBLIC_KEY)
    result = session.submit_proof(
        nonce=NONCE,
        public_key=PUBLIC_KEY,
        code_proof=proof,
        device_name="LAPTOP",
        device_kind="pc",
    )
    assert result.outcome is ProofOutcome.ACCEPTED
    assert session.pending_device_kind == "pc"
    paired = session.approve()
    assert paired is not None and paired.device.kind == "pc"
    assert DeviceStore(db).list()[0].kind == "pc"


def test_a_pairing_without_a_kind_is_a_phone(db: Database) -> None:
    session = _session(db)
    proof = compute_code_proof(CODE, NONCE, PIN, PUBLIC_KEY)
    session.submit_proof(nonce=NONCE, public_key=PUBLIC_KEY, code_proof=proof, device_name="Pixel")
    assert session.pending_device_kind == "phone"
    paired = session.approve()
    assert paired is not None and paired.device.kind == "phone"


def test_a_bad_kind_never_reaches_a_prompt(db: Database) -> None:
    session = _session(db)
    proof = compute_code_proof(CODE, NONCE, PIN, PUBLIC_KEY)
    result = session.submit_proof(
        nonce=NONCE,
        public_key=PUBLIC_KEY,
        code_proof=proof,
        device_name="X",
        device_kind="toaster",
    )
    assert result.outcome is ProofOutcome.BAD_REQUEST
    assert DeviceStore(db).count() == 0


# -------------------------------------------------------------- migration


def test_existing_devices_become_phones(tmp_path: Path) -> None:
    """A database made before section 21 has no ``kind`` column; opening it
    adds the column and every existing device is a phone."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE remote_devices (
            device_id    TEXT PRIMARY KEY,
            name         TEXT NOT NULL,
            public_key   BLOB NOT NULL,
            created_at   INTEGER NOT NULL,
            last_seen_at INTEGER NOT NULL
        );
        """
    )
    old.execute(
        "INSERT INTO remote_devices VALUES (?, ?, ?, ?, ?)",
        ("KsucBZHeyJguN6H0BzJQiw", "Pixel 8", PUBLIC_KEY, 1, 2),
    )
    old.commit()
    old.close()
    migrated = Database(path)
    try:
        devices = DeviceStore(migrated).list()
        assert [(d.name, d.kind) for d in devices] == [("Pixel 8", "phone")]
        DeviceStore(migrated).add("LAPTOP", PUBLIC_KEY, kind="pc")
        assert sorted(d.kind for d in DeviceStore(migrated).list()) == ["pc", "phone"]
    finally:
        migrated.close()
    Database(path).close()  # opening twice is harmless


def test_the_pc_fixtures_carry_real_proofs() -> None:
    from app.remote.devices import Device, verify_auth

    pair = _fixture("pair.request.pc.json")["message"]
    assert b64url_decode(pair["codeProof"]) == compute_code_proof(CODE, NONCE, PIN, PUBLIC_KEY)
    auth = _fixture("auth.request.pc.json")["message"]
    device = Device(auth["deviceId"], "LAPTOP", PUBLIC_KEY, 0, 0, "pc")
    assert verify_auth(device, NONCE, PIN, b64url_decode(auth["signature"]))
