"""The client side of PC links, without Qt services: framing and masking, the
handshake, trust on first use (pin and proofs, with ``vectors.json``), the
device key file, back-off and address order, and how a link's thread reacts to
a changed identity, a removed pairing and old protocols.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app.remote import wire
from app.remote.client import keys, pairing, session
from app.remote.client.session import LinkRecord, LinkRunner, backoff_seconds, candidate_addresses
from app.remote.client.transport import (
    Address,
    IdentityMismatch,
    TransportError,
    WsConnection,
    parse_address,
    spki_pin,
)
from app.remote.devices import b64url_decode, b64url_encode
from app.remote.pairing import compute_code_proof, compute_server_proof
from app.tests.fake_host import FakeConn, FakeHost

pytestmark = pytest.mark.timeout(60)

FIXTURES = Path(__file__).resolve().parents[2] / "protocol" / "fixtures"
V = json.loads((FIXTURES / "vectors.json").read_text(encoding="utf-8"))
CODE: str = V["pairingCode"]
NONCE = b64url_decode(V["nonce"])
PIN: str = V["pinnedFingerprint"]
PC_ID: str = V["pcId"]
DEVICE_ID: str = V["deviceId"]
PUBLIC = b64url_decode(V["deviceKey"]["publicKeySpkiDer"])
TEST_KEY = ec.derive_private_key(int(V["deviceKey"]["privateScalarHex"], 16), ec.SECP256R1())


# ------------------------------------------------------------------ framing


def test_client_frames_are_masked_the_rfc_example() -> None:
    """RFC 6455 section 5.7: "Hello" masked with 37 fa 21 3d."""
    frame = wire.encode_client_frame(wire.OP_TEXT, b"Hello", mask=bytes.fromhex("37fa213d"))
    assert frame == bytes.fromhex("818537fa213d7f9f4d5158")


@pytest.mark.parametrize("size", [0, 1, 125, 126, 65535, 65536, 200_000])
def test_a_masked_frame_round_trips_through_the_server_parser(size: int) -> None:
    payload = b"x" * size
    frame = wire.encode_client_frame(wire.OP_TEXT, payload)
    assert frame[1] & 0x80, "the mask bit must be set"
    events = wire.FrameParser(1_048_576, masked=True).feed(frame)
    assert events == [("text", payload.decode())]


def test_the_mask_is_random_per_frame() -> None:
    masks = {wire.encode_client_frame(wire.OP_TEXT, b"same")[2:6] for _ in range(20)}
    assert len(masks) > 1


def test_server_frames_must_not_be_masked_for_the_client_parser() -> None:
    parser = wire.FrameParser(1_048_576, masked=False)
    assert parser.feed(wire.encode_frame(wire.OP_TEXT, b'{"a":1}')) == [("text", '{"a":1}')]
    masked = wire.encode_client_frame(wire.OP_TEXT, b"{}")
    assert wire.FrameParser(1_048_576, masked=False).feed(masked) == [
        ("fail", wire.CLOSE_PROTOCOL_ERROR)
    ]
    # and the server parser still refuses what is not masked (unchanged)
    assert wire.FrameParser(1_048_576, masked=True).feed(
        wire.encode_frame(wire.OP_TEXT, b"{}")
    ) == [("fail", wire.CLOSE_PROTOCOL_ERROR)]


@pytest.mark.parametrize("masked", [True, False])
def test_the_one_mebibyte_limit_holds_both_ways(masked: bool) -> None:
    big = b"y" * (1_048_576 + 1)
    frame = (
        wire.encode_client_frame(wire.OP_TEXT, big)
        if masked
        else wire.encode_frame(wire.OP_TEXT, big)
    )
    # Judged from the header alone: the payload is never needed.
    events = wire.FrameParser(1_048_576, masked=masked).feed(frame[:14])
    assert events == [("fail", wire.CLOSE_TOO_BIG)]


def test_the_client_refuses_to_send_a_message_over_the_limit() -> None:

    connection = WsConnection.__new__(WsConnection)
    connection._closed = False
    with pytest.raises(TransportError):
        connection.send_text("z" * 1_048_577)


def test_the_upgrade_request_and_response() -> None:
    key = wire.new_key()
    request = wire.build_upgrade_request("127.0.0.1:47821", key).decode()
    assert request.startswith("GET /remote HTTP/1.1\r\n")
    assert "origin" not in request.lower() and "extensions" not in request.lower()
    # The server side accepts exactly this request.
    status, accept = wire.parse_upgrade(request.split("\r\n\r\n")[0].encode())
    assert status == 101
    good = wire.upgrade_response(101, accept).split(b"\r\n\r\n")[0]
    assert wire.check_upgrade_response(good, key)
    assert not wire.check_upgrade_response(good, wire.new_key())  # another key's accept
    assert not wire.check_upgrade_response(good + b"\r\nSec-WebSocket-Extensions: x", key)
    assert not wire.check_upgrade_response(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0", key)
    with pytest.raises(ValueError):
        wire.build_upgrade_request("host\r\nX: y", key)


# ---------------------------------------------------------- code and address


@pytest.mark.parametrize(
    "typed",
    ["7K3M9QX2RTBW", "7k3m-9qx2-rtbw", " 7K3M 9QX2 RTBW ", "7K3M-9QX2-RTBW\n"],
)
def test_the_code_is_normalised(typed: str) -> None:
    assert pairing.normalise_code(typed) == CODE


def test_look_alike_letters_are_fixed() -> None:
    assert pairing.normalise_code("OIL0-0000-0000") == "011000000000"


@pytest.mark.parametrize(
    "typed", ["", "7K3M9QX2RTB", "7K3M9QX2RTBWW", "7K3M9QX2RTBU", "7K3M9QX2RTB!"]
)
def test_a_code_that_does_not_look_right(typed: str) -> None:
    assert pairing.normalise_code(typed) is None


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("192.168.1.20", ("192.168.1.20", 47821)),
        ("192.168.1.20:5000", ("192.168.1.20", 5000)),
        ("laptop.local:47821", ("laptop.local", 47821)),
        ("[fd7a:115c:a1e0::20]:47822", ("fd7a:115c:a1e0::20", 47822)),
        ("[fd7a:115c:a1e0::20]", ("fd7a:115c:a1e0::20", 47821)),
        ("  10.0.0.2  ", ("10.0.0.2", 47821)),
    ],
)
def test_addresses_are_parsed(typed: str, expected: tuple[str, int]) -> None:
    assert parse_address(typed) == Address(*expected)


@pytest.mark.parametrize(
    "typed",
    ["", "   ", ":47821", "host:0", "host:70000", "host:abc", "fd7a::20", "[::1", "[]:5", "a b"],
)
def test_bad_addresses(typed: str) -> None:
    assert parse_address(typed) is None


def test_the_host_header_brackets_ipv6() -> None:
    assert Address("fd7a::20", 47821).host_header == "[fd7a::20]:47821"
    assert Address("10.0.0.2", 47821).host_header == "10.0.0.2:47821"


# ----------------------------------------------- the proofs (vectors.json)


def test_the_code_proof_matches_the_shared_vector() -> None:
    assert b64url_encode(compute_code_proof(CODE, NONCE, PIN, PUBLIC)) == V["pair"]["codeProof"]


def _hello() -> dict[str, Any]:
    return {"type": "hello", "pcName": "Study PC", "pcId": PC_ID}


def _paired(**changes: Any) -> dict[str, Any]:
    reply: dict[str, Any] = {
        "id": "pair-1",
        "type": "paired",
        "deviceId": DEVICE_ID,
        "pcId": PC_ID,
        "pcName": "Study PC",
        "serverProof": V["pair"]["serverProof"],
    }
    reply.update(changes)
    return reply


def _verify(reply: dict[str, Any], *, code: str = CODE, pin: str = PIN) -> pairing.PairResult:
    return pairing._verify(
        reply,
        _hello(),
        code,
        NONCE,
        pin,
        PUBLIC,
        PC_ID,
        Address("192.168.1.20", 47821),
        keys.DeviceKey(TEST_KEY),
    )


def test_the_shared_server_proof_verifies_and_yields_the_pairing() -> None:
    result = _verify(_paired())
    assert isinstance(result, pairing.Paired)
    assert (result.device_id, result.pc_id, result.pc_name) == (DEVICE_ID, PC_ID, "Study PC")
    assert result.pin == PIN and result.port == 47821 and result.address == "192.168.1.20"
    assert "key=" not in repr(result), "the key must not show in a repr"


@pytest.mark.parametrize(
    "changes",
    [
        {"serverProof": b64url_encode(bytes(32))},
        {"serverProof": V["pair"]["codeProof"]},  # the other proof is not this proof
        {"serverProof": ""},
        {"serverProof": 7},
        {"serverProof": None},
        {"deviceId": "A" * 22},  # proof bound to the device id
        {"deviceId": "short"},
        {"pcId": "B" * 22},
    ],
)
def test_a_proof_that_does_not_verify_gives_no_pairing(changes: dict[str, Any]) -> None:
    result = _verify(_paired(**changes))
    assert result == pairing.PairFailed(pairing.PROOF_MISMATCH)


def test_the_proof_is_bound_to_the_pin_the_code_and_the_key() -> None:
    other_pin = b64url_encode(bytes(32))
    assert _verify(_paired(), pin=other_pin) == pairing.PairFailed(pairing.PROOF_MISMATCH)
    assert _verify(_paired(), code="0" * 12) == pairing.PairFailed(pairing.PROOF_MISMATCH)


def test_the_comparison_is_constant_time_by_construction() -> None:
    import inspect

    source = inspect.getsource(pairing._verify) + inspect.getsource(
        __import__("app.remote.client.transport", fromlist=["x"]).WsConnection.open
    )
    assert "compare_digest" in source
    assert " == proof" not in source and "proof ==" not in source


# ----------------------------------------------------------- the key file


def test_the_key_file_is_private_and_written_atomically(tmp_path: Path) -> None:
    key = keys.new_key()
    name = keys.save(key, tmp_path / "links")
    path = tmp_path / "links" / name
    assert keys.is_valid_file_name(name) and path.is_file()
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert [p.name for p in path.parent.iterdir()] == [name], "no temp file may be left"
    loaded = keys.load(name, tmp_path / "links")
    assert loaded is not None and loaded.public_der == key.public_der
    assert len(key.public_der) == 91


def test_each_link_gets_its_own_key_and_file(tmp_path: Path) -> None:
    first, second = keys.new_key(), keys.new_key()
    a, b = keys.save(first, tmp_path), keys.save(second, tmp_path)
    assert a != b and first.public_der != second.public_der


def test_a_key_file_that_is_not_ours_is_not_loaded(tmp_path: Path) -> None:
    (tmp_path / "evil.pem").write_bytes(keys.new_key().pem())
    for name in ("evil.pem", "../x.pem", "link-zz.pem", "", "link-" + "0" * 16 + ".pem"):
        assert keys.load(name, tmp_path) is None
    other = ec.generate_private_key(ec.SECP384R1())
    from cryptography.hazmat.primitives import serialization

    pem = other.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    (tmp_path / "link-0123456789abcdef.pem").write_bytes(pem)
    assert keys.load("link-0123456789abcdef.pem", tmp_path) is None  # only P-256
    (tmp_path / "link-fedcba9876543210.pem").write_bytes(b"x" * 100_000)
    assert keys.load("link-fedcba9876543210.pem", tmp_path) is None


def test_deleting_a_key_removes_the_file_and_never_raises(tmp_path: Path) -> None:
    name = keys.save(keys.new_key(), tmp_path)
    keys.delete(name, tmp_path)
    keys.delete(name, tmp_path)
    keys.delete("../../etc/passwd", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_the_key_never_shows_in_a_repr() -> None:
    assert "PRIVATE" not in repr(keys.new_key()) and "ec." not in repr(keys.new_key())


def test_signatures_verify_against_the_public_key() -> None:
    from cryptography.hazmat.primitives import hashes

    key = keys.new_key()
    signature = key.sign(b"transcript")
    key.private.public_key().verify(signature, b"transcript", ec.ECDSA(hashes.SHA256()))
    assert len(signature) <= 72


# ------------------------------------------------------ back-off, addresses


def test_the_backoff_doubles_to_thirty_seconds() -> None:
    lows = [backoff_seconds(n, lambda: 0.0) for n in range(1, 9)]
    assert lows == [1, 2, 4, 8, 16, 30, 30, 30]
    highs = [backoff_seconds(n, lambda: 1.0) for n in range(1, 9)]
    assert highs == [x * 1.25 for x in lows]
    assert backoff_seconds(0, lambda: 0.0) == 1


def test_the_backoff_is_randomised() -> None:
    assert len({backoff_seconds(3) for _ in range(20)}) > 1


def test_the_last_good_address_comes_first_without_duplicates() -> None:
    assert candidate_addresses("10.0.0.2", ["192.168.1.2", "10.0.0.2", "[fd7a::2]"]) == [
        "10.0.0.2",
        "192.168.1.2",
        "[fd7a::2]",
    ]
    assert candidate_addresses(None, ["a", "b", "a"]) == ["a", "b"]
    assert candidate_addresses("", []) == []


# ---------------------------------------------- pairing against a fake host


def _good_pairing_script(host: FakeHost, codes: list[str], *, proof_ok: bool = True):
    def script(conn: FakeConn) -> None:
        conn.hello()
        request = conn.recv()
        assert request is not None and request["type"] == "pair"
        assert request["deviceKind"] == "pc" and request["protocolVersion"] == 6
        public = b64url_decode(request["publicKey"])
        proof = b64url_decode(request["codeProof"])
        assert proof == compute_code_proof(codes[0], conn.nonce, host.pin, public)
        conn.send({"type": "pairPending", "protocolVersion": 6, "timeoutSeconds": 60})
        device_id = DEVICE_ID
        server = compute_server_proof(codes[0], conn.nonce, host.pin, public, device_id)
        if not proof_ok:
            server = bytes(32)
        conn.send(
            {
                "id": request["id"],
                "type": "paired",
                "protocolVersion": 6,
                "deviceId": device_id,
                "pcId": host.pc_id,
                "pcName": host.pc_name,
                "serverProof": b64url_encode(server),
            }
        )
        conn.recv(0.3)

    return script


def test_pairing_trusts_the_key_on_first_use_and_returns_it(tmp_path: Path) -> None:
    holder: list[FakeHost] = []
    host = FakeHost(tmp_path / "h", lambda conn: _good_pairing_script(holder[0], [CODE])(conn))
    holder.append(host)
    try:
        seen: list[str] = []
        result = pairing.pair(
            f"127.0.0.1:{host.port}",
            "7k3m-9qx2-rtbw",
            "LAPTOP",
            on_progress=seen.append,
        )
        assert isinstance(result, pairing.Paired), result
        assert result.pin == host.pin  # the key the host presented, now the pin
        assert result.pc_id == host.pc_id and result.pc_name == "DESKTOP"
        assert seen[0] == "connecting" and "confirm" in seen
        assert not host.errors
    finally:
        host.stop()


def test_a_bad_server_proof_means_nothing_is_returned(tmp_path: Path) -> None:
    holder: list[FakeHost] = []
    host = FakeHost(
        tmp_path / "h", lambda conn: _good_pairing_script(holder[0], [CODE], proof_ok=False)(conn)
    )
    holder.append(host)
    try:
        result = pairing.pair(f"127.0.0.1:{host.port}", CODE, "LAPTOP")
        assert result == pairing.PairFailed(pairing.PROOF_MISMATCH)
    finally:
        host.stop()


def test_a_host_that_does_not_know_the_code_cannot_be_trusted(tmp_path: Path) -> None:
    """A machine in the middle: it answers, but its proof is made with another code."""
    holder: list[FakeHost] = []
    host = FakeHost(tmp_path / "h", lambda conn: _good_pairing_script(holder[0], ["0" * 12])(conn))
    holder.append(host)
    try:
        # the fake host's own assertion on the codeProof fails, so it closes: not trusted either way
        result = pairing.pair(f"127.0.0.1:{host.port}", CODE, "LAPTOP", timeout=3.0)
        assert isinstance(result, pairing.PairFailed)
    finally:
        host.stop()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ({"code": "badCode", "attemptsLeft": 3}, pairing.PairFailed(pairing.BAD_CODE, 3)),
        ({"code": "pairingLocked"}, pairing.PairFailed(pairing.LOCKED)),
        ({"code": "pairingExpired"}, pairing.PairFailed(pairing.EXPIRED)),
        ({"code": "pairingDenied"}, pairing.PairFailed(pairing.DECLINED)),
        ({"code": "conflict"}, pairing.PairFailed(pairing.FULL)),
        ({"code": "versionTooOld"}, pairing.PairFailed(pairing.CLIENT_TOO_OLD)),
        ({"code": "internal"}, pairing.PairFailed(pairing.REJECTED)),
    ],
)
def test_error_replies_are_mapped(
    tmp_path: Path, error: dict[str, Any], expected: pairing.PairFailed
) -> None:
    def script(conn: FakeConn) -> None:
        conn.hello()
        request = conn.recv()
        assert request is not None
        conn.send({"id": request["id"], "type": "error", "protocolVersion": 6, **error})
        conn.recv(0.3)

    host = FakeHost(tmp_path / "h", script)
    try:
        assert pairing.pair(f"127.0.0.1:{host.port}", CODE, "LAPTOP") == expected
    finally:
        host.stop()


def test_a_host_with_an_old_protocol_gets_nothing_sent(tmp_path: Path) -> None:
    received: list[Any] = []

    def script(conn: FakeConn) -> None:
        conn.hello(protocolVersion=5)
        received.append(conn.recv(0.5))

    host = FakeHost(tmp_path / "h", script)
    try:
        assert pairing.pair(f"127.0.0.1:{host.port}", CODE, "L") == pairing.PairFailed(
            pairing.HOST_TOO_OLD
        )
        # The fake host records what it received when its read ends, just after the client left.
        deadline = time.monotonic() + 5
        while not received and time.monotonic() < deadline:
            time.sleep(0.02)
        assert received == [None]
    finally:
        host.stop()


def test_a_host_that_needs_a_newer_client(tmp_path: Path) -> None:
    host = FakeHost(tmp_path / "h", lambda conn: conn.hello(minClientVersion=7))
    try:
        assert pairing.pair(f"127.0.0.1:{host.port}", CODE, "L") == pairing.PairFailed(
            pairing.CLIENT_TOO_OLD
        )
    finally:
        host.stop()


def test_pairing_can_be_cancelled_while_waiting_for_allow(tmp_path: Path) -> None:
    def script(conn: FakeConn) -> None:
        conn.hello()
        request = conn.recv()
        assert request is not None
        conn.send({"type": "pairPending", "protocolVersion": 6, "timeoutSeconds": 60})
        conn.recv(5.0)  # the user never answers

    host = FakeHost(tmp_path / "h", script)
    cancel = threading.Event()
    results: list[pairing.PairResult] = []
    steps: list[str] = []
    try:
        thread = threading.Thread(
            target=lambda: results.append(
                pairing.pair(
                    f"127.0.0.1:{host.port}", CODE, "L", cancel=cancel, on_progress=steps.append
                )
            )
        )
        thread.start()
        deadline = time.monotonic() + 5
        while "confirm" not in steps and time.monotonic() < deadline:
            time.sleep(0.02)
        cancel.set()
        thread.join(5)
        assert results == [pairing.PairFailed(pairing.CANCELLED)]
    finally:
        host.stop()


def test_nobody_listening_is_not_found() -> None:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert pairing.pair(f"127.0.0.1:{port}", CODE, "L") == pairing.PairFailed(pairing.NOT_FOUND)


def test_a_bad_address_or_code_is_refused_before_any_connection() -> None:
    assert pairing.pair("", CODE, "L") == pairing.PairFailed(pairing.BAD_ADDRESS)
    assert pairing.pair("127.0.0.1", "nope", "L") == pairing.PairFailed(pairing.BAD_CODE)


# ------------------------------------------------------------- the pin check


def test_the_pin_is_checked_before_a_single_http_byte_is_sent(tmp_path: Path) -> None:
    seen: list[Any] = []
    host = FakeHost(tmp_path / "h", lambda conn: seen.append("upgraded"))
    try:
        other = spki_pin(
            __import__("ssl").PEM_cert_to_DER_cert((tmp_path / "h" / "remote_cert.pem").read_text())
        )
        assert other == host.pin
        with pytest.raises(IdentityMismatch):
            WsConnection.open(Address("127.0.0.1", host.port), pin=b64url_encode(bytes(32)))
        time.sleep(0.2)
        assert seen == [], "the HTTP upgrade must not be sent to a key that is not the pin"
        connection = WsConnection.open(Address("127.0.0.1", host.port), pin=host.pin)
        assert connection.peer_pin == host.pin
        connection.close()
    finally:
        host.stop()


# --------------------------------------------------------------- the runner


def _record(host: FakeHost, *, pin: str | None = None, pc_id: str | None = None) -> LinkRecord:
    return LinkRecord(
        pc_id=pc_id or host.pc_id,
        name="DESKTOP",
        pin=pin or host.pin,
        device_id=DEVICE_ID,
        addresses=("127.0.0.1",),
        port=host.port,
        last_address="127.0.0.1",
        key_file="link-0000000000000000.pem",
        paired_at=1,
        last_seen_at=1,
    )


class Collected:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []
        self.lock = threading.Lock()

    def __call__(self, kind: str, payload: Any) -> None:
        with self.lock:
            self.events.append((kind, payload))

    def states(self) -> list[str]:
        with self.lock:
            return [p for k, p in self.events if k == "state"]

    def wait_for(self, predicate: Any, timeout: float = 8.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.02)
        return False


def _runner(record: LinkRecord, collected: Collected) -> LinkRunner:
    return LinkRunner(
        record,
        keys.DeviceKey(TEST_KEY),
        collected,
        backoff=lambda _n: 0.05,
        connect_timeout=1.0,
    )


def _auth_ok(host: FakeHost) -> dict[str, Any]:
    return {
        "type": "authOk",
        "protocolVersion": 6,
        "pcId": host.pc_id,
        "pcName": host.pc_name,
        "appVersion": "1.4.0",
        "appRunning": True,
        "serverTime": 1,
        "addresses": ["127.0.0.1"],
        "port": host.port,
        "features": ["jobs", "control", "events", "phoneJobs"],
    }


def test_the_runner_signs_in_with_a_valid_signature(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives import hashes

    from app.remote.devices import auth_transcript

    checked: list[bool] = []
    holder: list[FakeHost] = []

    def script(conn: FakeConn) -> None:
        conn.hello()
        request = conn.recv()
        assert request is not None and request["type"] == "auth"
        assert request["deviceKind"] == "pc" and request["deviceId"] == DEVICE_ID
        transcript = auth_transcript(conn.nonce, holder[0].pin, DEVICE_ID)
        TEST_KEY.public_key().verify(
            b64url_decode(request["signature"]), transcript, ec.ECDSA(hashes.SHA256())
        )
        checked.append(True)
        conn.send({"id": request["id"], "protocolVersion": 6, **_auth_ok(holder[0])})
        conn.recv(5.0)

    host = FakeHost(tmp_path / "h", script)
    holder.append(host)
    collected = Collected()
    runner = _runner(_record(host), collected)
    runner.start()
    try:
        assert collected.wait_for(lambda: any(k == "online" for k, _ in collected.events))
        assert checked == [True] and runner.online
        online = next(p for k, p in collected.events if k == "online")
        assert online["address"] == "127.0.0.1" and online["authOk"]["features"]
        assert runner.send({"id": "x1", "type": "ping"}) is True
    finally:
        runner.stop()
        runner.join()
        host.stop()
    assert not host.errors


def test_a_changed_key_is_identity_changed_and_the_runner_stops(tmp_path: Path) -> None:
    host = FakeHost(tmp_path / "h", lambda conn: conn.hello())
    collected = Collected()
    runner = _runner(_record(host, pin=b64url_encode(bytes(32))), collected)
    runner.start()
    try:
        assert collected.wait_for(lambda: session.IDENTITY_CHANGED in collected.states())
        runner.join(3.0)
        assert not runner.alive, "no automatic retry after an identity change"
        time.sleep(0.3)
        assert host.connections <= 1 and host.errors == []
        assert collected.states().count(session.CONNECTING) == 1
    finally:
        runner.stop()
        host.stop()


def test_another_pc_id_under_the_same_key_is_identity_changed(tmp_path: Path) -> None:
    host = FakeHost(tmp_path / "h", lambda conn: conn.hello())
    collected = Collected()
    runner = _runner(_record(host, pc_id="B" * 22), collected)
    runner.start()
    try:
        assert collected.wait_for(lambda: session.IDENTITY_CHANGED in collected.states())
    finally:
        runner.stop()
        runner.join()
        host.stop()


def test_unauthorized_on_a_pinned_connection_means_removed(tmp_path: Path) -> None:
    def script(conn: FakeConn) -> None:
        conn.hello()
        request = conn.recv()
        assert request is not None
        conn.send(
            {
                "id": request["id"],
                "type": "error",
                "protocolVersion": 6,
                "code": "unauthorized",
                "message": "not authorized",
            }
        )
        conn.close(4401)

    host = FakeHost(tmp_path / "h", script)
    collected = Collected()
    runner = _runner(_record(host), collected)
    runner.start()
    try:
        assert collected.wait_for(lambda: session.REMOVED in collected.states())
        runner.join(3.0)
        assert not runner.alive
    finally:
        runner.stop()
        host.stop()


def test_close_4403_while_online_means_removed(tmp_path: Path) -> None:
    holder: list[FakeHost] = []

    def script(conn: FakeConn) -> None:
        conn.hello()
        request = conn.recv()
        assert request is not None
        conn.send({"id": request["id"], "protocolVersion": 6, **_auth_ok(holder[0])})
        time.sleep(0.2)
        conn.close(4403)

    host = FakeHost(tmp_path / "h", script)
    holder.append(host)
    collected = Collected()
    runner = _runner(_record(host), collected)
    runner.start()
    try:
        assert collected.wait_for(lambda: session.REMOVED in collected.states())
        assert any(k == "online" for k, _ in collected.events)
    finally:
        runner.stop()
        runner.join()
        host.stop()


@pytest.mark.parametrize(
    ("hello", "state"),
    [
        ({"protocolVersion": 5}, session.HOST_TOO_OLD),
        ({"minClientVersion": 7}, session.CLIENT_TOO_OLD),
    ],
)
def test_protocol_mismatch_is_terminal(tmp_path: Path, hello: dict[str, int], state: str) -> None:
    received: list[Any] = []

    def script(conn: FakeConn) -> None:
        conn.hello(**hello)
        received.append(conn.recv(0.5))

    host = FakeHost(tmp_path / "h", script)
    collected = Collected()
    runner = _runner(_record(host), collected)
    runner.start()
    try:
        assert collected.wait_for(lambda: state in collected.states())
        assert received in ([], [None]), "nothing may be sent to an incompatible host"
    finally:
        runner.stop()
        runner.join()
        host.stop()


def test_an_unreachable_host_is_retried_with_backoff(tmp_path: Path) -> None:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    host = FakeHost(tmp_path / "h", lambda conn: None)
    host.stop()
    record = _record(host)
    record = LinkRecord(**{**record.__dict__, "port": port})
    collected = Collected()
    runner = _runner(record, collected)
    runner.start()
    try:
        assert collected.wait_for(lambda: collected.states().count(session.OFFLINE) >= 3)
        assert runner.alive
    finally:
        runner.stop()
        runner.join()


def test_the_session_sends_what_is_queued_and_hands_over_what_arrives(tmp_path: Path) -> None:
    holder: list[FakeHost] = []
    got: list[dict[str, Any]] = []

    def script(conn: FakeConn) -> None:
        conn.hello()
        request = conn.recv()
        assert request is not None
        conn.send({"id": request["id"], "protocolVersion": 6, **_auth_ok(holder[0])})
        ping = conn.recv(5.0)
        if ping is not None:
            got.append(ping)
            conn.send({"id": ping["id"], "type": "pong", "protocolVersion": 6})
        conn.recv(2.0)

    host = FakeHost(tmp_path / "h", script)
    holder.append(host)
    collected = Collected()
    runner = _runner(_record(host), collected)
    runner.start()
    try:
        assert collected.wait_for(lambda: runner.online)
        assert runner.send({"id": "q1", "type": "ping"})
        assert collected.wait_for(
            lambda: any(k == "message" and p.get("type") == "pong" for k, p in collected.events)
        )
        assert got and got[0]["id"] == "q1"
    finally:
        runner.stop()
        runner.join()
        host.stop()
    assert not host.errors


def test_send_is_refused_when_there_is_no_login(tmp_path: Path) -> None:
    host = FakeHost(tmp_path / "h", lambda conn: None)
    runner = _runner(_record(host), Collected())
    try:
        assert runner.send({"id": "x", "type": "ping"}) is False
    finally:
        host.stop()


# ----------------------------------------------- security review fixes


def test_m2_the_runner_stops_reading_while_the_gui_is_behind(tmp_path: Path) -> None:
    from app.remote.client.session import MESSAGE_BACKLOG_LIMIT

    holder: list[FakeHost] = []
    sent = [0]

    def script(conn: FakeConn) -> None:
        conn.hello()
        request = conn.recv()
        assert request is not None
        conn.send({"id": request["id"], "protocolVersion": 6, **_auth_ok(holder[0])})
        for number in range(MESSAGE_BACKLOG_LIMIT * 8):
            conn.send({"type": "event", "event": "job.removed", "jobId": number + 1, "seq": number})
            sent[0] += 1
        conn.recv(4.0)

    host = FakeHost(tmp_path / "h", script)
    holder.append(host)
    collected = Collected()
    runner = _runner(_record(host), collected)
    runner.start()
    try:
        assert collected.wait_for(lambda: runner.online)
        time.sleep(1.0)
        posted = sum(1 for k, _ in collected.events if k == "message")
        # Nothing acknowledged: reading stopped at the limit (a read in flight may add a few).
        assert MESSAGE_BACKLOG_LIMIT <= posted <= MESSAGE_BACKLOG_LIMIT + 8, posted
        assert runner.unhandled == posted
        for _ in range(posted):  # the GUI catches up: reading resumes
            runner.ack()
        assert collected.wait_for(
            lambda: sum(1 for k, _ in collected.events if k == "message") > posted
        )
    finally:
        runner.stop()
        runner.join()
        host.stop()


def test_l3_the_runner_ignores_host_supplied_names_and_public_addresses(tmp_path: Path) -> None:
    host = FakeHost(tmp_path / "h", lambda conn: None)
    try:
        runner = _runner(_record(host), Collected())
        runner._learn(
            {"addresses": ["8.8.8.8", "evil.example", "10.0.0.9", "[fe80::1]"], "port": 47822}
        )
        assert runner._addresses == ["10.0.0.9", "[fe80::1]"]
        runner._learn({"addresses": ["8.8.8.8", "evil.example"]})
        assert runner._addresses == ["10.0.0.9", "[fe80::1]"], "nothing usable: keep what we had"
    finally:
        host.stop()


def test_low2_the_runner_also_stops_reading_at_the_byte_limit(tmp_path: Path) -> None:
    from app.remote.client.session import MESSAGE_BACKLOG_BYTES, MESSAGE_BACKLOG_LIMIT

    holder: list[FakeHost] = []
    pad = "x" * 600_000  # a large but legal frame

    def script(conn: FakeConn) -> None:
        conn.hello()
        request = conn.recv()
        assert request is not None
        conn.send({"id": request["id"], "protocolVersion": 6, **_auth_ok(holder[0])})
        for number in range(40):
            conn.send({"type": "event", "event": "x", "seq": number, "pad": pad})
        conn.recv(4.0)

    host = FakeHost(tmp_path / "h", script)
    holder.append(host)
    collected = Collected()
    runner = _runner(_record(host), collected)
    runner.start()
    try:
        assert collected.wait_for(lambda: runner.online)
        time.sleep(1.5)
        posted = sum(1 for k, _ in collected.events if k == "message")
        assert posted < MESSAGE_BACKLOG_LIMIT, "stopped by bytes, long before 256 messages"
        assert runner.unhandled_bytes < MESSAGE_BACKLOG_BYTES + 700_000
        assert runner.unhandled_bytes >= MESSAGE_BACKLOG_BYTES
        for _ in range(posted):
            runner.ack()
        assert runner.unhandled == 0 and runner.unhandled_bytes == 0
        assert collected.wait_for(
            lambda: sum(1 for k, _ in collected.events if k == "message") > posted
        )
    finally:
        runner.stop()
        runner.join()
        host.stop()
