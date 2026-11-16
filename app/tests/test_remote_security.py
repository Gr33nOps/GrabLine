"""Scripted attackers against the phone-remote listener (P1-D11).

Every test here is a hostile or broken client talking to a real listener over
real TLS sockets in loopback test mode on a free port. Two rigs are used: the
bare ``RemoteListener`` with a stand-in GUI (``Rig`` from
test_remote_listener.py) and the real ``RemoteService`` over a real manager
and database (``Env`` from test_remote_service.py), where a wrong answer would
show up as a row in the ``handoffs`` table.

One test per row of SPEC.md "Security and privacy" and per QA-01..QA-11. A
case test_remote_listener.py already asserts identically is not repeated; the
table says where each one lives ("listener:" is test_remote_listener.py,
"service:" is test_remote_service.py, no prefix is this file).

| QA / SPEC row | Test |
| --- | --- |
| QA-01 pair, Allow | listener: test_pair_approve_then_login |
| QA-02 expired code | listener: test_pair_after_expiry; test_fixture_pair_expired |
| QA-03 five wrong codes | listener: test_five_wrong_codes_lock_the_session; |
|  | test_fixture_pair_wrong_code |
| QA-04 Deny | listener: test_pair_deny_stores_nothing |
| QA-05 unpaired client | listener: test_preauth_download_is_rejected_with_no_detail; |
|  | test_fixture_preauth_requests, |
|  | test_every_other_type_before_login_is_refused_with_no_detail |
| QA-06 removed while connected | listener: test_revoke_while_connected; service: |
|  | test_remove_device_closes_the_connection; |
|  | test_unknown_revoked_and_bad_signature_are_indistinguishable |
| QA-07 new PC identity | test_new_identity_changes_the_pin_and_breaks_old_pairings |
| QA-08 folder tricks | test_folder_and_queue_tricks_write_nothing, |
|  | test_path_fields_are_forbidden_on_every_message_type, |
|  | test_paths_in_place_of_job_ids_are_bad_requests, |
|  | test_only_folder_ids_the_pc_issued_are_accepted |
| QA-09 2 MB message | listener: test_oversized_frame_closes_1009_and_listener_keeps_running |
|  | (header only); test_fixture_oversize_two_megabyte_frame (the whole frame), |
|  | test_frame_limit_is_exactly_one_mebibyte, |
|  | test_fragmented_message_over_the_limit_is_1009 |
| QA-10 100 requests a second | listener: test_request_burst_is_rate_limited_before_dispatch, |
|  | test_flood_closes_4429_and_refuses_the_device; |
|  | test_fixture_flood_100_a_second_for_3_seconds, |
|  | test_flood_closes_every_connection_and_refuses_for_30_seconds |
| QA-11 outside probe, last phone | listener: test_no_socket_until_a_pairing_is_opened, |
|  | test_bind_list_is_private_only_and_listener_stops_with_last_device, |
|  | test_expired_qr_with_no_device_turns_the_listener_off, test_address_rules; |
|  | test_only_private_and_link_local_addresses_are_bound, |
|  | test_loopback_is_bound_only_in_test_mode, |
|  | test_peers_outside_the_local_network_are_dropped_before_tls, |
|  | test_listener_stays_on_until_the_last_device_or_qr_is_gone |
| Same Wi-Fi sends commands | QA-05 and QA-11 rows, plus test_preauth_frame_cap_is_4096_bytes, |
|  | test_fifth_preauth_message_is_refused, |
|  | test_auth_deadline_is_ten_seconds_and_chatter_does_not_extend_it, |
|  | test_allow_prompt_suspends_the_auth_deadline, |
|  | test_at_most_eight_unauthenticated_connections, |
|  | test_half_open_sockets_count_against_the_address_cap, |
|  | test_ten_failures_from_an_address_refuse_it; listener: test_auth_timeout, |
|  | test_unauthenticated_connections_are_capped_per_address |
| A fake PC impersonates yours | QA-07 row, test_proofs_made_for_another_pc_key_are_rejected |
| Pairing code guessed or reused | QA-01..04 rows, |
|  | test_captured_pair_and_auth_frames_cannot_be_replayed |
| A stolen phone keeps access | QA-06 row |
| The phone writes anywhere | QA-08 row |
| Oversized or malformed messages | QA-09 and QA-10 rows, test_fixture_not_json, |
|  | test_malformed_frames_close_the_connection, |
|  | test_deep_nesting_is_a_bad_request_not_a_crash; listener: |
|  | test_binary_frame_is_malformed, test_wire_parser_is_strict |
| Listener exposed to the internet | QA-11 row, |
|  | test_listener_package_has_no_upnp_or_datagram_code; |
|  | listener: test_origin_header_and_wrong_path_are_refused |
| Downloaded files are dangerous | test_phone_handoff_carries_no_browser_headers_and_runs_nothing |
| No secrets in logs | test_no_secret_reaches_the_log |

The fixtures test_protocol_fixtures.py defers to the listener (its
``SKIP_LISTENER``) are each sent over the wire by the ``test_fixture_*`` tests;
``WIRE_TESTS`` maps one to the other and a test keeps the two in step.
"""

from __future__ import annotations

import contextlib
import functools
import ipaddress
import itertools
import json
import logging
import os
import re
import socket
import sqlite3
import ssl
import statistics
import struct
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from PySide6 import QtNetwork
from PySide6.QtCore import QTimer
from PySide6.QtNetwork import QHostAddress, QNetworkInterface, QTcpServer

from app.core.models import JobStatus
from app.remote import certs, folders, ratelimit, schema, wire
from app.remote import devices as devices_module
from app.remote import listener as listener_module
from app.remote.certs import pin_from_spki
from app.remote.devices import auth_transcript, b64url_decode, b64url_encode
from app.remote.pairing import PairingSession, format_code
from app.remote.ratelimit import DeviceLimiter
from app.tests.remote_client import Client, Phone, mask_frame, pump, settle
from app.tests.test_protocol_fixtures import FIXTURES, SKIP_LISTENER, load
from app.tests.test_remote_listener import Rig
from app.tests.test_remote_service import Env

pytestmark = pytest.mark.timeout(90)

VECTORS = json.loads((FIXTURES / "vectors.json").read_text(encoding="utf-8"))
URL = "https://example.com/file.zip"

#: SKIP_LISTENER fixture -> the test here that sends it over the wire.
WIRE_TESTS = {
    "invalid/auth.badSignature.json": "test_fixture_auth_bad_signature",
    "invalid/auth.oldClient.json": "test_fixture_auth_old_client",
    "invalid/auth.unknownDevice.json": "test_fixture_auth_unknown_device",
    "invalid/pair.expired.json": "test_fixture_pair_expired",
    "invalid/pair.wrongCode.json": "test_fixture_pair_wrong_code",
    "invalid/preauth.download.json": "test_fixture_preauth_requests",
    "invalid/preauth.jobs.json": "test_fixture_preauth_requests",
    "invalid/oversize.marker.json": "test_fixture_oversize_two_megabyte_frame",
    "invalid/notJson.marker.json": "test_fixture_not_json",
    "invalid/flood.marker.json": "test_fixture_flood_100_a_second_for_3_seconds",
    "invalid/fetch.unfinishedJob.json": "test_fixture_fetch_unfinished_job",
    "fetch.ack.json": "test_fixture_fetch_ack_gets_no_reply",
}

#: A valid body for every message type that needs ``authOk`` first.
AUTHED_REQUESTS: dict[str, dict[str, Any]] = {
    "ping": {},
    "download": {"url": URL, "requestId": "req-attack-000000000001"},
    "status": {"urls": [URL]},
    "recent": {},
    "focus": {"target": "settings"},
    "links": {"urls": [URL], "requestId": "req-attack-000000000002"},
    "jobs": {},
    "control": {"jobId": 1, "action": "remove"},
    "global": {"action": "pauseAll"},
    "queues": {},
    "analyze": {"url": URL},
    "subscribe": {},
    "unsubscribe": {},
    "unpair": {},
    "files": {},
    "fetch": {"jobId": 1},
    "fetchCancel": {"streamId": 1},
    "fileHash": {"jobId": 1},
    "outbox": {},
    "outboxAck": {"itemIds": [1]},
}


# ------------------------------------------------------------------ helpers


@pytest.fixture(autouse=True)
def _temp_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))


@pytest.fixture()
def rig(tmp_path: Path) -> Iterator[Rig]:
    made = Rig(tmp_path)
    try:
        yield made
    finally:
        made.close()


@pytest.fixture()
def env(tmp_path: Path) -> Iterator[Env]:
    made = Env(tmp_path)
    try:
        yield made
    finally:
        made.close()


def refusal(request_id: str | None) -> dict[str, Any]:
    """The one answer to anything that is not allowed before login."""
    reply: dict[str, Any] = {} if request_id is None else {"id": request_id}
    reply.update(type="error", protocolVersion=6, code="unauthorized", message="not authorized")
    return reply


def fresh[T](make: Callable[[], T], timeout: float = 3.0) -> T:
    """``make()`` a connection, waiting out the last one's slot in the
    per-address cap (a closed connection lingers until the peer is gone)."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return make()
        except (OSError, ssl.SSLError):
            if time.monotonic() >= deadline:
                raise
            settle(0.05)


def quiet(client: Client, seconds: float = 0.4) -> bool:
    """True if the listener sends nothing for ``seconds``."""
    try:
        client.next(seconds)
    except AssertionError:
        return True
    return False


def text_frame(payload: bytes) -> bytes:
    return mask_frame(wire.OP_TEXT, payload)


def fragment(payload: bytes, *, first: bool, final: bool) -> bytes:
    frame = bytearray(mask_frame(wire.OP_TEXT if first else wire.OP_CONTINUATION, payload))
    if not final:
        frame[0] &= 0x7F
    return bytes(frame)


def push(client: Client, data: bytes) -> None:
    """Send bytes the listener may stop reading half way through."""
    with contextlib.suppress(OSError):
        client.send_raw(data)


def padded(message: dict[str, Any], field: str, char: str, total: int) -> bytes:
    """``message`` as compact JSON of exactly ``total`` bytes (fixtures README)."""
    filled = {**message, field: ""}
    empty = len(json.dumps(filled, separators=(",", ":")).encode())
    filled[field] = char * (total - empty)
    data = json.dumps(filled, separators=(",", ":")).encode()
    assert len(data) == total
    return data


def refused(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    except OSError:
        return True
    return False


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def gone(raw: socket.socket, timeout: float = 3.0) -> bool:
    """True once the listener has dropped this plain TCP connection without
    ever sending a byte."""
    raw.settimeout(timeout)
    try:
        return raw.recv(4096) == b""
    except (ConnectionResetError, ConnectionAbortedError):
        return True


def dropped_before_tls(port: int) -> bool:
    """Offer a TLS ClientHello; True if the listener closes without one byte."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = context.wrap_bio(incoming, outgoing)
    with contextlib.suppress(ssl.SSLWantReadError):
        tls.do_handshake()
    raw = socket.create_connection(("127.0.0.1", port), timeout=3)
    try:
        with contextlib.suppress(ConnectionResetError, ConnectionAbortedError):
            raw.sendall(outgoing.read())
        return gone(raw)
    finally:
        raw.close()


def is_private(address: str) -> bool:
    """PROTOCOL.md section 3, written out again so the listener's own
    classifier is not what judges it."""
    ip = ipaddress.ip_address(address.strip("[]").split("%")[0])
    nets = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"]
    if ip.version == 6:
        nets = ["fc00::/7", "fe80::/10"]
    return any(ip in ipaddress.ip_network(net) for net in nets)


def handoff_rows(tmp_path: Path) -> list[tuple[Any, ...]]:
    """The ``handoffs`` table, read through a second connection (claiming
    them through the app's API would delete them)."""
    with contextlib.closing(sqlite3.connect(tmp_path / "service-test.db")) as conn:
        return list(conn.execute("SELECT * FROM handoffs"))


def ask(client: Client, message: dict[str, Any]) -> dict[str, Any]:
    """One request and its reply, waiting when the bucket says to."""
    for _ in range(40):
        client.send(message)
        reply = client.message()
        if reply.get("code") != "rateLimited":
            return reply
        settle(reply["retryAfterMs"] / 1000 + 0.02)
    raise AssertionError("still rate limited")


def service_login(env: Env) -> tuple[Phone, str, Client]:
    phone, pin = env.pair()
    return phone, pin, env.login(phone, pin)


def fixture_device(rig: Rig) -> ec.EllipticCurvePrivateKey:
    """Pair the fixtures' TEST-ONLY phone (vectors.json) by storing its row,
    which also turns the listener on. Returns its private key."""
    key = ec.derive_private_key(int(VECTORS["deviceKey"]["privateScalarHex"], 16), ec.SECP256R1())
    public = b64url_decode(VECTORS["deviceKey"]["publicKeySpkiDer"])
    assert public == key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    rig.store.add("Pixel 8", public, device_id=VECTORS["deviceId"])
    rig.listener.start()
    assert pump(lambda: rig.listener.running)
    return key


class Shift:
    """A clock that tests can move forward: real time plus ``offset``."""

    def __init__(self, base: Callable[[], float]) -> None:
        self._base = base
        self.offset = 0.0

    def __call__(self) -> float:
        return self._base() + self.offset


class ShiftedTime:
    """Stands in for the ``time`` module inside listener.py, so its 10 s and
    60 s monotonic deadlines can be reached without waiting for them."""

    def __init__(self) -> None:
        self.monotonic = Shift(time.monotonic)
        self.time = time.time


# ------------------------------------------- the deferred protocol fixtures


def test_every_listener_fixture_has_a_wire_test() -> None:
    assert set(WIRE_TESTS) == set(SKIP_LISTENER)
    for name in WIRE_TESTS.values():
        assert callable(globals()[name])


def test_fixture_auth_bad_signature(rig: Rig) -> None:
    fixture = load("invalid/auth.badSignature.json")
    fixture_device(rig)  # the device is known; only its signature is wrong
    client, _nonce = rig.hello()
    client.send(fixture["message"])
    assert client.message() == refusal(fixture["message"]["id"])
    assert client.closed_with() == fixture["expectClose"] == 4401
    assert fixture["expectError"] == "unauthorized"


def test_fixture_auth_unknown_device(rig: Rig) -> None:
    fixture = load("invalid/auth.unknownDevice.json")
    fixture_device(rig)
    assert rig.store.get(fixture["message"]["deviceId"]) is None
    client, _nonce = rig.hello()
    client.send(fixture["message"])
    assert client.message() == refusal(fixture["message"]["id"])
    assert client.closed_with() == fixture["expectClose"] == 4401


def test_fixture_auth_old_client(rig: Rig) -> None:
    """The fixture's signature is over the fixture nonce and pin, so it is
    made again for this connection; everything else is sent as stored."""
    fixture = load("invalid/auth.oldClient.json")
    message = dict(fixture["message"])
    key = fixture_device(rig)
    # As stored the signature is stale: the version is not even looked at.
    stale, _nonce = rig.hello()
    stale.send(message)
    assert stale.message() == refusal(message["id"])
    assert stale.closed_with() == 4401
    stale.close()
    client, nonce = fresh(rig.hello)
    transcript = auth_transcript(nonce, client.pin, message["deviceId"])
    message["signature"] = b64url_encode(key.sign(transcript, ec.ECDSA(hashes.SHA256())))
    client.send(message)
    reply = client.message()
    assert reply["id"] == message["id"] and reply["type"] == "error"
    assert reply["code"] == fixture["expectError"] == "versionTooOld"
    assert reply["minClientVersion"] == 6
    assert client.closed_with() == fixture["expectClose"] == 4426
    settle(0.1)
    assert rig.gui.requests == []


def test_fixture_pair_expired(rig: Rig) -> None:
    """QA-02, the "no QR is showing" half: a paired phone keeps the listener
    on, but there is no pairing session."""
    fixture = load("invalid/pair.expired.json")
    fixture_device(rig)
    client, _nonce = rig.hello()
    client.send(fixture["message"])
    reply = client.message()
    assert reply["id"] == fixture["message"]["id"]
    assert reply["code"] == fixture["expectError"] == "pairingExpired"
    assert "expectClose" not in fixture and quiet(client)
    assert rig.store.count() == 1 and rig.gui.pending == []


def test_fixture_pair_wrong_code(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """QA-03. The session really has the fixtures' code, as the fixture asks."""
    fixture = load("invalid/pair.wrongCode.json")
    letters = itertools.cycle(VECTORS["pairingCode"])
    monkeypatch.setattr(
        listener_module,
        "PairingSession",
        functools.partial(PairingSession, choice=lambda _alphabet: next(letters)),
    )
    qr = rig.open_pairing()
    assert qr["c"] == VECTORS["pairingCode"]
    client, _nonce = rig.hello(qr["f"])
    client.send(fixture["message"])
    reply = client.message()
    assert reply["id"] == fixture["message"]["id"]
    assert reply["code"] == fixture["expectError"] == "badCode"
    assert reply["attemptsLeft"] == 4
    assert "expectClose" not in fixture and quiet(client)
    assert rig.store.count() == 0 and rig.gui.pending == []


@pytest.mark.parametrize("name", ["invalid/preauth.download.json", "invalid/preauth.jobs.json"])
def test_fixture_preauth_requests(rig: Rig, name: str) -> None:
    """QA-05."""
    fixture = load(name)
    rig.open_pairing()
    client, _nonce = rig.hello()
    client.send(fixture["message"])
    assert client.message() == refusal(fixture["message"]["id"])
    assert client.closed_with() == fixture["expectClose"] == 4401
    settle(0.1)
    assert rig.gui.requests == [] and rig.store.count() == 0


def test_fixture_oversize_two_megabyte_frame(rig: Rig) -> None:
    """QA-09 with the whole 2 MB frame really sent, not just its header."""
    fixture = load("invalid/oversize.marker.json")
    gen = fixture["generate"]
    payload = padded(fixture["message"], gen["padField"], gen["padChar"], gen["totalBytes"])
    assert len(payload) == 2 * 1024 * 1024
    phone, pin = rig.paired_phone()
    client = rig.login(phone, pin)
    push(client, text_frame(payload))
    # No error reply: the frame is never parsed.
    assert client.closed_with() == fixture["expectClose"] == 1009
    client.close()
    assert rig.listener.running
    again = rig.login(phone, pin)
    again.send({"id": "p-1", "type": "ping"})
    assert again.message()["type"] == "pong"
    assert [request.type for _t, _d, request in rig.gui.requests] == ["ping"]


def test_fixture_not_json(rig: Rig) -> None:
    fixture = load("invalid/notJson.marker.json")
    phone, pin = rig.paired_phone()
    client = rig.login(phone, pin)
    client.send_raw(text_frame(fixture["raw"].encode()))
    assert client.closed_with() == fixture["expectClose"] == 4400  # and no reply
    settle(0.1)
    assert rig.gui.requests == [] and rig.listener.running


def test_fixture_flood_100_a_second_for_3_seconds(rig: Rig) -> None:
    """QA-10: the fixture's flood, paced from another thread while this (the
    GUI) thread only runs its event loop and times a 10 ms QTimer."""
    fixture = load("invalid/flood.marker.json")
    rate, seconds = fixture["generate"]["repeatPerSecond"], fixture["generate"]["seconds"]
    total = rate * seconds
    assert (rate, seconds) == (100, 3)
    phone, pin = rig.paired_phone()
    client = rig.login(phone, pin)

    def flood() -> None:
        begin = time.monotonic()
        for index in range(total):
            wait = begin + index / rate - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                client.send({**fixture["message"], "id": f"x-22-{index}"})
            except OSError:
                return  # closed for flooding

    ticks: list[float] = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    sender = threading.Thread(target=flood, daemon=True)
    started = time.monotonic()
    ticks.append(started)
    timer.start()
    sender.start()
    replies: list[dict[str, Any]] = []
    closed: object = None
    try:
        while len(replies) < total and closed is None:
            kind, value = client.next(5.0)
            if kind == "text":
                replies.append(value)
            else:
                closed = value if kind == "close" else kind
    finally:
        timer.stop()
    elapsed = time.monotonic() - started
    ticks.append(time.monotonic())
    sender.join(5)
    assert not sender.is_alive()

    served = [r for r in replies if r["type"] == "pong"]
    limited = [r for r in replies if r["type"] == "error"]
    assert len(served) + len(limited) == len(replies)
    for reply in limited:
        assert set(reply) == {"id", "type", "protocolVersion", "code", "message", "retryAfterMs"}
        assert reply["code"] == fixture["expectError"] == "rateLimited"
        assert type(reply["retryAfterMs"]) is int and reply["retryAfterMs"] > 0
    # "At least the first 40 succeed": the burst.
    assert {f"x-22-{index}" for index in range(40)} <= {r["id"] for r in served}
    # Dispatch to the GUI side is bounded by the bucket, not by the flood.
    budget = 40 + 20 * elapsed + 1
    assert len(served) <= budget and len(served) <= len(rig.gui.requests) <= budget
    # 300 requests against a budget of about 100 sits right on the flood line
    # (more than 200 refusals in 10 s): either every request was answered, or
    # the 201st refusal closed the connection.
    if closed is None:
        assert len(replies) == total and len(limited) <= 200
    else:
        assert closed == 4429 and len(limited) == 200
    # The window stayed responsive: the GUI thread's timer never stalled.
    assert len(ticks) > 50
    assert max(b - a for a, b in itertools.pairwise(ticks)) < 0.2
    assert rig.listener.running
    other, _nonce = rig.hello(pin)
    assert other.status == 101


def test_fixture_fetch_unfinished_job(env: Env, tmp_path: Path) -> None:
    """``invalid/fetch.unfinishedJob.json`` (QA-40): a job that is still
    downloading is never served. The fixture's job 41 is the one made here."""
    fixture = load("invalid/fetch.unfinishedJob.json")
    assert fixture["expectError"] == "conflict"
    _phone, _pin, client = service_login(env)
    job = env.db.create_job(URL, str(env.dest), "running.bin")
    env.db.set_job_status(job.id, JobStatus.DOWNLOADING, None)
    (env.dest / "running.bin").write_bytes(b"x" * 100)
    reply = ask(client, {**fixture["message"], "jobId": job.id})
    assert reply["id"] == fixture["message"]["id"] and reply["code"] == "conflict"
    assert not [f for f in client.frames if f[0] == wire.OP_BINARY]


def test_fixture_fetch_path(env: Env) -> None:
    fixture = load("invalid/fetch.path.json")
    _phone, _pin, client = service_login(env)
    reply = ask(client, fixture["message"])
    assert reply["id"] == fixture["message"]["id"] and reply["code"] == fixture["expectError"]


def test_fixture_fetch_ack_gets_no_reply(rig: Rig) -> None:
    fixture = load("fetch.ack.json")
    assert "id" not in fixture["message"]
    phone, pin = rig.paired_phone()
    client = rig.login(phone, pin)
    client.send(fixture["message"])
    assert quiet(client)


# ------------------------------------------------------ login cannot be probed


def test_unknown_revoked_and_bad_signature_are_indistinguishable(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA-06 / section 6: the same bytes, the same close, the same work."""
    monkeypatch.setattr(ratelimit, "FAILURE_LIMIT", 10**6)  # all from one address
    verifications: list[int] = []
    real_verify = devices_module._ecdsa_verifies

    def counting_verify(*args: Any) -> bool:
        verifications.append(1)
        return real_verify(*args)

    monkeypatch.setattr(devices_module, "_ecdsa_verifies", counting_verify)
    keeper, pin = rig.paired_phone("Keeper")
    revoked, _pin = rig.paired_phone("Revoked")
    assert rig.listener.revoke_device(revoked.device_id) is True
    stranger = Phone()
    stranger.device_id = b64url_encode(os.urandom(16))
    impostor = Phone()
    impostor.device_id = keeper.device_id  # a real id, somebody else's key
    attackers = {"unknown": stranger, "revoked": revoked, "badSignature": impostor}

    wire_bytes: set[tuple[tuple[int, bytes], ...]] = set()
    reply_delay: dict[str, list[float]] = {name: [] for name in attackers}
    close_delay: dict[str, list[float]] = {name: [] for name in attackers}
    for _round in range(10):
        for name, who in attackers.items():
            client, nonce = fresh(lambda: rig.hello(pin))
            request = who.auth_request(pin, nonce)
            before = len(verifications)
            sent = time.monotonic()
            client.send(request)
            assert client.message() == refusal("r-2")
            assert client.closed_with() == 4401
            client.close()
            # Exactly one signature check, known device or not.
            assert len(verifications) - before == 1, name
            hello, reply, close = client.frames[:3]
            assert hello[0] == wire.OP_TEXT
            wire_bytes.add(((reply[0], reply[1]), (close[0], close[1])))
            reply_delay[name].append(reply[2] - sent)
            close_delay[name].append(close[2] - reply[2])
    # Byte for byte the same error frame and the same close frame (no reason text).
    assert len(wire_bytes) == 1
    ((_opcode, error), (_close_opcode, close_payload)) = next(iter(wire_bytes))
    assert json.loads(error) == refusal("r-2")
    assert close_payload == struct.pack(">H", 4401)
    # No case answers, or hangs up, noticeably sooner than another.
    replies = [statistics.median(times) for times in reply_delay.values()]
    closes = [statistics.median(times) for times in close_delay.values()]
    assert max(replies) - min(replies) < 0.03, reply_delay
    assert max(closes) - min(closes) < 0.03, close_delay
    # A signature that is not even DER gets the same frames.
    client, _nonce = fresh(lambda: rig.hello(pin))
    client.send(
        {
            "id": "r-2",
            "type": "auth",
            "protocolVersion": 6,
            "deviceId": keeper.device_id,
            "signature": b64url_encode(os.urandom(70)),
        }
    )
    assert client.message() == refusal("r-2") and client.closed_with() == 4401
    assert (client.frames[1][:2], client.frames[2][:2]) in wire_bytes
    # An old version tells a stranger nothing either: no "update your app".
    client, nonce = fresh(lambda: rig.hello(pin))
    client.send(stranger.auth_request(pin, nonce, version=5))
    assert client.message() == refusal("r-2") and client.closed_with() == 4401
    assert rig.store.count() == 1 and rig.gui.requests == []


def test_proofs_made_for_another_pc_key_are_rejected(rig: Rig) -> None:
    """A machine in the middle has its own TLS key. A phone that pinned (or,
    in manual entry, saw) that key makes proofs the real PC refuses."""
    middle = ec.generate_private_key(ec.SECP256R1()).public_key()
    middle_pin = pin_from_spki(
        middle.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    qr = rig.open_pairing()
    assert middle_pin != qr["f"]
    client, nonce = rig.hello(qr["f"])
    client.send(Phone().pair_request(qr["c"], middle_pin, nonce))  # the right code
    reply = client.message()
    assert reply["code"] == "badCode" and reply["attemptsLeft"] == 4
    assert rig.store.count() == 0 and rig.gui.pending == []
    client.close()
    phone, pin = rig.paired_phone()
    relayed, nonce = fresh(lambda: rig.hello(pin))
    relayed.send(phone.auth_request(middle_pin, nonce))
    assert relayed.message() == refusal("r-2")
    assert relayed.closed_with() == 4401


def test_captured_pair_and_auth_frames_cannot_be_replayed(rig: Rig) -> None:
    qr = rig.open_pairing()
    phone = Phone()
    first, nonce = rig.hello(qr["f"])
    pair = phone.pair_request(qr["c"], qr["f"], nonce)
    # The same frame on another connection: the proof is bound to the nonce.
    second, _other_nonce = rig.hello(qr["f"])
    second.send(pair)
    assert second.message()["code"] == "badCode"
    second.close()
    first.send(pair)
    assert first.message()["type"] == "pairPending"
    rig.listener.approve_pairing()
    reply = first.message()
    assert reply["type"] == "paired"
    phone.device_id = reply["deviceId"]
    # Used once: the very same frame again, anywhere, is dead.
    third, _nonce3 = fresh(lambda: rig.hello(qr["f"]))
    third.send(pair)
    assert third.message()["code"] == "pairingExpired"
    third.close()
    auth = phone.auth_request(qr["f"], nonce)
    first.send(auth)
    assert first.message()["type"] == "authOk"
    fourth, _nonce4 = fresh(lambda: rig.hello(qr["f"]))
    fourth.send(auth)
    assert fourth.message() == refusal("r-2")
    assert fourth.closed_with() == 4401
    assert rig.store.count() == 1


def test_new_identity_changes_the_pin_and_breaks_old_pairings(rig: Rig, tmp_path: Path) -> None:
    """QA-07, PC side: after "Reset phone connections" or a reinstall the PC
    presents another key, so a phone pinned to the old one stops in the TLS
    handshake, and its login would not verify even if it carried on."""
    phone, old_pin = rig.paired_phone()
    rig.listener.stop()
    assert pump(lambda: not rig.listener.running)
    identity = certs.regenerate(rig.store.pc_id(), tmp_path / "remote")
    assert identity.pin != old_pin
    rig.listener.start()
    assert pump(lambda: rig.listener.running)
    with pytest.raises(AssertionError, match="does not match the pin"):
        rig.connect(pin=old_pin)
    # A careless client that skips the pin check: its signature is bound to
    # the key it paired with, and this PC has another.
    careless, nonce = fresh(rig.hello)
    assert careless.pin == identity.pin
    careless.send(phone.auth_request(old_pin, nonce))
    assert careless.message() == refusal("r-2")
    assert careless.closed_with() == 4401
    settle(0.1)
    assert rig.gui.requests == []


# ------------------------------------------------------------- before login


def test_every_other_type_before_login_is_refused_with_no_detail(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA-05 for every message type, against the real handlers and database:
    one generic error, close 4401, nothing read and nothing written."""
    monkeypatch.setattr(ratelimit, "FAILURE_LIMIT", 10**6)
    assert set(AUTHED_REQUESTS) | {"pair", "auth"} == set(schema.VALIDATORS)
    for kind, body in AUTHED_REQUESTS.items():
        assert schema.validate({"id": "ok", "type": kind, **body}).type == kind
    job = env.db.create_job(URL, str(env.dest), "secret-holiday-video.mp4")
    env.service.start_pairing()
    assert pump(lambda: bool(env.started))
    port = env.service.listener.port
    probes: list[dict[str, Any]] = [
        {"id": f"x-{kind}", "type": kind, **body} for kind, body in AUTHED_REQUESTS.items()
    ]
    probes += [
        {"id": "x-gallery", "type": "gallery", "urls": [URL]},
        {"type": "fetchAck", "streamId": 1, "received": 0},
        {"id": "x-unknown", "type": "nonsense"},
        {"id": "x-case", "type": "PAIR"},
        {"id": "x-number", "type": 7},
        {"id": "x-list", "type": ["pair"]},
        {"id": "x-none", "type": None},
        {"id": "x-missing"},
        {},
    ]
    for probe in probes:
        client = fresh(lambda: Client(port))
        env.clients.append(client)
        assert client.message()["type"] == "hello"
        client.send(probe)
        assert client.message() == refusal(probe.get("id")), probe
        assert client.closed_with() == 4401, probe
        assert b"secret-holiday" not in client.frames[1][1]
        client.close()
    settle(0.2)
    assert handoff_rows(tmp_path) == []
    kept = env.db.get_job(job.id)
    assert kept is not None and kept.status is JobStatus.QUEUED  # not removed, not paused
    assert env.service.store.count() == 0 and env.pending == []
    assert env.service.listener.running


def test_preauth_frame_cap_is_4096_bytes(rig: Rig) -> None:
    rig.open_pairing()
    probe = {"id": "x-1", "type": "jobs", "pad": ""}
    exact, _nonce = rig.hello()
    exact.send_raw(text_frame(padded(probe, "pad", "A", 4096)))
    assert exact.message() == refusal("x-1")  # read, then refused like any other
    assert exact.closed_with() == 4401
    exact.close()
    over, _nonce = fresh(rig.hello)
    over.send_raw(text_frame(padded(probe, "pad", "A", 4097)))
    assert over.closed_with() == 1009  # and no reply
    over.close()
    # Pieces that add up to more than the cap are no way round it.
    pieces, _nonce = fresh(rig.hello)
    head = b'{"id":"x-1","type":"jobs","pad":"'
    pieces.send_raw(fragment(head + b"A" * 2100, first=True, final=False))
    push(pieces, fragment(b"A" * 2100, first=False, final=False))
    push(pieces, fragment(b'A"}', first=False, final=True))
    assert pieces.closed_with() == 1009
    settle(0.1)
    assert rig.gui.requests == [] and rig.listener.running


def test_fifth_preauth_message_is_refused(rig: Rig) -> None:
    fixture_device(rig)
    message = load("invalid/pair.expired.json")["message"]
    client, _nonce = rig.hello()
    for index in range(4):
        client.send({**message, "id": f"p-{index}"})
        assert client.message()["code"] == "pairingExpired"
    client.send({**message, "id": "p-4"})
    assert client.message() == refusal("p-4")
    assert client.closed_with() == 4401


def test_auth_deadline_is_ten_seconds_and_chatter_does_not_extend_it(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 6 with the real constant, on a clock the test moves."""
    assert listener_module.AUTH_TIMEOUT_S == 10.0
    clock = ShiftedTime()
    monkeypatch.setattr(listener_module, "time", clock)
    rig.open_pairing()
    client, _nonce = rig.hello()
    # A second connection that never even starts TLS.
    silent = socket.create_connection(("127.0.0.1", rig.listener.port), timeout=3)
    try:
        settle(0.3)  # accepted, so its 10 s have begun
        clock.monotonic.offset = 8.5
        settle(0.7)  # several housekeeping ticks
        client.send_raw(mask_frame(wire.OP_PING, b"still here"))
        assert client.next() == ("pong", b"still here")
        clock.monotonic.offset = 10.2
        assert client.closed_with(3.0) == 4408
        assert gone(silent)
    finally:
        silent.close()


def test_allow_prompt_suspends_the_auth_deadline(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """A phone waiting on the Allow prompt has the prompt's 60 s instead, and
    a fresh 10 s to log in once it is allowed."""
    clock = ShiftedTime()
    monkeypatch.setattr(listener_module, "time", clock)
    qr = rig.open_pairing()
    phone = Phone()
    client, nonce = rig.hello(qr["f"])
    client.send(phone.pair_request(qr["c"], qr["f"], nonce))
    assert client.message()["type"] == "pairPending"
    clock.monotonic.offset = 45.0
    settle(0.7)
    client.send_raw(mask_frame(wire.OP_PING, b"waiting"))
    assert client.next() == ("pong", b"waiting")
    rig.listener.approve_pairing()
    assert client.message()["type"] == "paired"
    clock.monotonic.offset = 45.0 + 8.5
    settle(0.7)
    client.send_raw(mask_frame(wire.OP_PING, b"allowed"))
    assert client.next() == ("pong", b"allowed")
    clock.monotonic.offset = 45.0 + 10.2
    assert client.closed_with(3.0) == 4408  # paired, but it never logged in


def test_at_most_eight_unauthenticated_connections(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every test client comes from 127.0.0.1; lift the per-address cap so the
    # total is what stops the ninth.
    monkeypatch.setattr(listener_module, "MAX_UNAUTHENTICATED_PER_ADDRESS", 100)
    phone, pin = rig.paired_phone()
    logged_in = rig.login(phone, pin)  # does not count
    held = [rig.hello(pin) for _ in range(8)]
    with pytest.raises((OSError, ssl.SSLError)):
        rig.connect()
    logged_in.send({"id": "p-1", "type": "ping"})
    assert logged_in.message()["type"] == "pong"
    # One of the eight logs in and so leaves the unauthenticated count.
    client, nonce = held[0]
    client.send(phone.auth_request(pin, nonce))
    assert client.message()["type"] == "authOk"
    ninth, _nonce = rig.hello(pin)
    assert ninth.status == 101
    with pytest.raises((OSError, ssl.SSLError)):
        rig.connect()


def test_half_open_sockets_count_against_the_address_cap(rig: Rig) -> None:
    """Three TCP connections that never start TLS use up the address's three
    slots; they cannot be used to hold more than that."""
    rig.open_pairing()
    port = rig.listener.port
    idle = [socket.create_connection(("127.0.0.1", port), timeout=3) for _ in range(3)]
    try:
        settle(0.3)
        with pytest.raises((OSError, ssl.SSLError)):
            rig.connect()
    finally:
        for raw in idle:
            raw.close()
    client, _nonce = fresh(rig.hello)
    assert client.status == 101


def test_ten_failures_from_an_address_refuse_it(rig: Rig) -> None:
    rig.open_pairing()
    for index in range(10):
        client, _nonce = fresh(rig.hello)
        client.send({"id": f"g-{index}", "type": "jobs"})
        assert client.message() == refusal(f"g-{index}")
        assert client.closed_with() == 4401
        client.close()
    settle(0.3)
    for _ in range(3):
        with pytest.raises((OSError, ssl.SSLError)):
            rig.connect()
        settle(0.1)
    assert rig.listener.running and rig.gui.requests == []


# --------------------------------------------------- the phone names no path

_PATHS = [
    "../../Windows",
    "..\\..\\Windows",
    "C:\\Windows\\System32",
    "C:/Windows/System32",
    "/etc",
    "\\\\evil\\share",
    "//evil/share",
    "f/../../Windows",
    "~",
    "%2e%2e%2fWindows",
]


def test_folder_and_queue_tricks_write_nothing(env: Env, tmp_path: Path) -> None:
    """QA-08: relative paths, absolute paths and UNC names where an id belongs."""
    for name in ("invalid/download.folderTraversal.json", "invalid/download.folderAbsolute.json"):
        assert load(name)["expectError"] == "badRequest"
    phone, _pin, client = service_login(env)
    tree = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")}
    cases: list[tuple[str, object, str]] = [("folderId", path, "badRequest") for path in _PATHS]
    cases += [
        ("folderId", "", "badRequest"),
        ("folderId", "f" + "A" * 32, "badRequest"),
        ("folderId", "fAAAAAAAAAAAAAA\n", "badRequest"),
        ("folderId", 7, "badRequest"),
        ("folderId", ["../../Windows"], "badRequest"),
        ("folderId", {"path": "C:\\Windows"}, "badRequest"),
        ("folderId", "Windows", "notFound"),  # well formed, but never issued
        ("folderId", "fAAAAAAAAAAAAAA", "notFound"),
        ("queueId", "../../Windows", "badRequest"),
        ("queueId", "C:\\Windows", "badRequest"),
        ("queueId", True, "badRequest"),
        ("queueId", 0, "badRequest"),
        ("queueId", 424242, "notFound"),
        ("url", "file:///C:/Windows/win.ini", "badRequest"),
        ("url", "C:\\Windows\\win.ini", "badRequest"),
        ("url", "\\\\evil\\share\\a.exe", "badRequest"),
        ("url", "/etc/passwd", "badRequest"),
        ("pageUrl", "file:///etc/passwd", "badRequest"),
        ("requestId", "../../../../Windows/x", "badRequest"),
    ]
    for index, (field, value, expected) in enumerate(cases):
        request_id = f"req-trick-{index:012d}"
        message = {
            "id": f"t-{index}",
            "type": "download",
            "url": URL,
            "requestId": request_id,
            field: value,
        }
        reply = ask(client, message)
        assert reply["id"] == f"t-{index}" and reply["type"] == "error", (field, value)
        assert reply["code"] == expected, (field, value, reply)
        if isinstance(value, str) and value:
            assert value not in reply["message"]  # nothing is echoed back
        if field != "requestId":
            assert env.service.requests.lookup(phone.device_id, request_id) is None
    reply = ask(
        client,
        {
            "id": "l-1",
            "type": "links",
            "urls": ["file:///etc/passwd", "C:\\x"],
            "requestId": "r" * 16,
        },
    )
    assert reply["code"] == "badRequest"
    reply = ask(
        client,
        {"id": "l-2", "type": "links", "urls": [URL], "requestId": "r" * 16, "folderId": "/etc"},
    )
    assert reply["code"] == "badRequest"
    assert handoff_rows(tmp_path) == [] and env.db.list_jobs() == []
    after = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")}
    assert {p for p in after - tree if not p.endswith(("-wal", "-shm", "-journal"))} == set()
    assert list(env.dest.iterdir()) == []


def test_path_fields_are_forbidden_on_every_message_type(env: Env, tmp_path: Path) -> None:
    """QA-08 / QA-40: a location named in any request is refused outright,
    whatever the type, and the request has no effect."""
    assert load("invalid/download.pathField.json")["expectError"] == "forbidden"
    assert load("invalid/fetch.path.json")["expectError"] == "forbidden"
    _phone, _pin, client = service_login(env)
    job = env.db.create_job(URL, str(env.dest), "keep-me.zip")
    bodies = {**AUTHED_REQUESTS, "pair": {}, "auth": {}}
    bodies["control"] = {"jobId": job.id, "action": "remove"}
    probes: list[tuple[str, str, object]] = [
        (kind, field, "C:\\Windows\\System32")
        for kind in sorted(bodies)
        for field in ("path", "folder", "filename")
    ]
    probes += [("download", field, "/etc") for field in sorted(schema.PATH_FIELDS)]
    empties: list[object] = [None, 0, {}, [], ""]
    probes += [("download", "path", value) for value in empties]
    for index, (kind, field, value) in enumerate(probes):
        message = {"id": f"f-{index}", "type": kind, **bodies[kind], field: value}
        reply = ask(client, message)
        assert reply["id"] == f"f-{index}" and reply["type"] == "error", message
        assert reply["code"] == "forbidden", (kind, field, reply)
        assert "Windows" not in reply["message"] and "etc" not in reply["message"]
    # Nothing ran: no handoff, the job is still there, the phone is still
    # paired (``unpair`` was refused too) and still connected.
    assert handoff_rows(tmp_path) == []
    assert env.db.get_job(job.id) is not None
    assert env.service.store.count() == 1
    assert ask(client, {"id": "p-1", "type": "ping"})["type"] == "pong"


def test_paths_in_place_of_job_ids_are_bad_requests(env: Env, tmp_path: Path) -> None:
    """Files are only ever named by the PC's own job ids (section 7)."""
    fixture = load("invalid/control.pathAsJobId.json")
    assert fixture["expectError"] == "badRequest"
    _phone, _pin, client = service_login(env)
    job = env.db.create_job(URL, str(env.dest), "keep-me.zip")
    assert ask(client, fixture["message"])["code"] == "badRequest"
    target = str(env.dest / "keep-me.zip")
    probes: list[dict[str, Any]] = []
    for value in (target, "../keep-me.zip", str(job.id), [job.id], {"id": job.id}, float(job.id)):
        probes.append({"type": "control", "jobId": value, "action": "remove"})
        probes.append({"type": "fetch", "jobId": value})
        probes.append({"type": "fileHash", "jobId": value})
    probes += [
        {"type": "fetchCancel", "streamId": target},
        {"type": "outboxAck", "itemIds": [target]},
        {"type": "jobs", "cursor": "../../Windows"},
        {"type": "files", "cursor": "C:\\Windows"},
        {"type": "control", "jobId": job.id, "action": "../../remove"},
    ]
    for index, probe in enumerate(probes):
        reply = ask(client, {"id": f"j-{index}", **probe})
        assert reply["code"] == "badRequest", (probe, reply)
        assert "keep-me" not in reply["message"]
    assert env.db.get_job(job.id) is not None
    assert handoff_rows(tmp_path) == []


def test_only_folder_ids_the_pc_issued_are_accepted(env: Env, tmp_path: Path) -> None:
    """SPEC: "Folders come only from the PC's own list"."""
    env.settings.download_dir = env.dest
    _phone, _pin, client = service_login(env)
    listed = ask(client, {"id": "q-1", "type": "queues"})
    assert str(tmp_path) not in json.dumps(listed) and "\\\\" not in json.dumps(listed)
    folder = listed["folders"][0]
    assert folder["isDefault"] is True and folder["name"] == "downloads"
    issued = folder["id"]

    def download(index: int, folder_id: str) -> dict[str, Any]:
        request = {"url": f"{URL}?n={index}", "requestId": f"req-folder-{index:012d}"}
        return ask(
            client, {"id": f"d-{index}", "type": "download", **request, "folderId": folder_id}
        )

    forged = issued[:-1] + ("A" if issued[-1] != "A" else "B")
    foreign = folders.folder_id(os.urandom(32), env.dest)  # right path, another PC's secret
    for index, folder_id in enumerate((forged, foreign, issued.lower(), issued + "A")):
        assert download(index, folder_id)["code"] == "notFound", folder_id
    assert download(10, str(env.dest))["code"] == "badRequest"
    assert handoff_rows(tmp_path) == []
    assert download(11, issued)["type"] == "queued"
    (handoff,) = env.db.claim_handoffs()
    assert handoff.source == "phone" and handoff.options["folderId"] == issued


def test_phone_handoff_carries_no_browser_headers_and_runs_nothing(
    env: Env, tmp_path: Path
) -> None:
    """A phone link enters the desktop's normal, advisory-checked pipeline as
    a plain handoff: no cookies or headers, no claimed source, nothing started
    or opened by the listener itself."""
    _phone, _pin, client = service_login(env)
    reply = ask(
        client,
        {
            "id": "d-1",
            "type": "download",
            "url": URL,
            "requestId": "req-headers-000000001",
            "cookie": "sid=stolen-session",
            "referer": "https://evil.example/referer",
            "userAgent": "EvilAgent/1.0",
            "headers": {"Cookie": "sid=stolen-session", "Authorization": "Bearer stolen"},
            "fallbackUrls": ["https://evil.example/fallback.exe"],
            "source": "extension",
            "onlyIfRunning": True,
            "unattended": False,
            "open": True,
        },
    )
    assert reply["type"] == "queued" and reply["duplicate"] is False
    assert env.db.list_jobs() == []  # the app adds it later, through its own checks
    (row,) = handoff_rows(tmp_path)
    stored = json.dumps([str(column) for column in row])
    for smuggled in ("stolen", "evil.example", "EvilAgent", "fallback"):
        assert smuggled not in stored
    (handoff,) = env.db.claim_handoffs()
    assert handoff.url == URL and handoff.source == "phone" and handoff.headers == {}
    assert handoff.options["unattended"] is True
    assert set(handoff.options) == {
        "requestId",
        "deviceId",
        "queueId",
        "folderId",
        "title",
        "quality",
        "unattended",
    }


# ----------------------------------------------- oversized and malformed frames


def test_frame_limit_is_exactly_one_mebibyte(rig: Rig) -> None:
    """QA-09 at the boundary: 1,048,576 bytes is served, one more is not."""
    phone, pin = rig.paired_phone()
    client = rig.login(phone, pin)
    probe = {"id": "big", "type": "ping", "pad": ""}
    client.send_raw(text_frame(padded(probe, "pad", "x", 1_048_576)))
    assert client.message()["type"] == "pong"
    push(client, text_frame(padded(probe, "pad", "x", 1_048_577)))
    assert client.closed_with() == 1009
    client.close()
    again = rig.login(phone, pin)
    again.send({"id": "p-1", "type": "ping"})
    assert again.message()["type"] == "pong"
    assert len(rig.gui.requests) == 2 and rig.listener.running


def test_fragmented_message_over_the_limit_is_1009(rig: Rig) -> None:
    phone, pin = rig.paired_phone()
    # In pieces and inside the limit: served like any other message.
    client = rig.login(phone, pin)
    client.send_raw(
        fragment(b'{"id":"p-1",', first=True, final=False)
        + fragment(b'"type":', first=False, final=False)
        + fragment(b'"ping"}', first=False, final=True)
    )
    assert client.message() == {
        "id": "p-1",
        "type": "pong",
        "protocolVersion": 6,
        "appRunning": True,
    }
    # Two 600 KB pieces: refused at the second header, 1.2 MB never buffered.
    half = 600_000
    client.send_raw(
        fragment(b'{"id":"p-2","type":"ping","pad":"' + b"x" * half, first=True, final=False)
    )
    push(client, fragment(b"x" * half + b'"}', first=False, final=True))
    assert client.closed_with() == 1009
    client.close()
    # A thousand small pieces do no better.
    drip = rig.login(phone, pin)
    drip.send_raw(fragment(b'{"id":"p-3","type":"ping","pad":"', first=True, final=False))
    piece = fragment(b"x" * 2000, first=False, final=False)
    push(drip, piece * 600)
    assert drip.closed_with() == 1009
    drip.close()
    after = rig.login(phone, pin)
    after.send({"id": "p-4", "type": "ping"})
    assert after.message()["type"] == "pong"
    assert [request.id for _t, _d, request in rig.gui.requests] == ["p-1", "p-4"]


_PING = b'{"id":"a","type":"ping"}'
_RESERVED = bytearray(text_frame(_PING))
_RESERVED[0] |= 0x40

_MALFORMED: dict[str, tuple[bytes, int]] = {
    "array": (text_frame(b'[{"id":"a","type":"ping"}]'), 4400),
    "string": (text_frame(b'"ping"'), 4400),
    "number": (text_frame(b"7"), 4400),
    "null": (text_frame(b"null"), 4400),
    "true": (text_frame(b"true"), 4400),
    "empty": (text_frame(b""), 4400),
    "truncated": (text_frame(_PING[:-1]), 4400),
    "two objects": (text_frame(_PING + _PING), 4400),
    "invalid utf-8": (text_frame(b'{"id":"a","type":"ping","x":"\xff\xfe"}'), 4400),
    "overlong utf-8": (text_frame(b'{"id":"a","type":"ping","x":"\xc0\xaf"}'), 4400),
    "utf-8 surrogate": (text_frame(b'{"id":"a","type":"ping","x":"\xed\xa0\x80"}'), 4400),
    "utf-8 cut by a fragment": (
        fragment(b'{"id":"a","type":"ping","x":"\xe2\x82', first=True, final=False)
        + fragment(b'"}', first=False, final=True),
        4400,
    ),
    "binary": (mask_frame(wire.OP_BINARY, _PING), 4400),
    "unmasked": (wire.encode_frame(wire.OP_TEXT, _PING), 1002),
    "reserved bit": (bytes(_RESERVED), 1002),
}


@pytest.mark.parametrize("case", sorted(_MALFORMED))
def test_malformed_frames_close_the_connection(rig: Rig, case: str) -> None:
    """Section 2: not an object, not JSON, not UTF-8 or not text closes 4400
    with no reply, before and after login; the listener keeps running."""
    data, code = _MALFORMED[case]
    phone, pin = rig.paired_phone()
    client = rig.login(phone, pin)
    client.send_raw(data)
    assert client.closed_with() == code
    client.close()
    early, _nonce = fresh(lambda: rig.hello(pin))
    early.send_raw(data)
    assert early.closed_with() == code
    early.close()
    assert rig.gui.requests == [] and rig.listener.running
    again = fresh(lambda: rig.login(phone, pin))
    again.send({"id": "p-1", "type": "ping"})
    assert again.message()["type"] == "pong"


def test_deep_nesting_is_a_bad_request_not_a_crash(rig: Rig) -> None:
    def nested(lists: int, kind: str = "ping", request_id: str = "n") -> bytes:
        body = "[" * lists + "]" * lists
        return text_frame(f'{{"id":"{request_id}","type":"{kind}","x":{body}}}'.encode())

    phone, pin = rig.paired_phone()
    client = rig.login(phone, pin)
    client.send_raw(nested(7, request_id="n-8"))  # the object plus 7 lists: 8 levels
    assert client.message()["type"] == "pong"
    client.send_raw(nested(8, request_id="n-9"))
    reply = client.message()
    assert (reply["id"], reply["code"]) == ("n-9", "badRequest")
    for lists in (500, 5_000, 400_000):  # past any parser's recursion limit
        client.send_raw(nested(lists, request_id="n-deep"))
        reply = client.message()
        assert reply["type"] == "error" and reply["code"] == "badRequest", lists
        assert reply.get("id") in (None, "n-deep")
    client.send({"id": "n-after", "type": "ping"})
    assert client.message()["type"] == "pong"
    assert [request.id for _t, _d, request in rig.gui.requests] == ["n-8", "n-after"]
    # Before login the same trick gets a bare error and never a parse.
    early, _nonce = rig.hello(pin)
    early.send_raw(nested(12, kind="pair", request_id="n-pair"))
    assert early.message() == {
        "id": "n-pair",
        "type": "error",
        "protocolVersion": 6,
        "code": "badRequest",
        "message": "bad request",
    }
    early.send_raw(nested(1900, kind="pair", request_id="n-pair"))
    reply = early.message()
    assert reply["type"] == "error" and reply["code"] in ("badRequest", "unauthorized")
    assert rig.listener.running and rig.store.count() == 1


# ------------------------------------------------------------------- flooding


def test_flood_closes_every_connection_and_refuses_for_30_seconds(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA-10 / section 9: the 201st refusal in 10 s closes all of the device's
    connections with 4429 and the device stays out for 30 s; other phones are
    not affected."""
    clock = Shift(time.monotonic)
    monkeypatch.setattr(listener_module, "DeviceLimiter", lambda: DeviceLimiter(clock=clock))
    phone, pin = rig.paired_phone("Flooder")
    bystander, _pin = rig.paired_phone("Bystander")
    client = rig.login(phone, pin)
    second = rig.login(phone, pin)
    other = rig.login(bystander, pin)
    burst = b"".join(
        text_frame(json.dumps({"id": f"f-{index}", "type": "ping"}).encode())
        for index in range(300)
    )
    push(client, burst)
    served = limited = 0
    code: object = None
    while code is None:
        kind, value = client.next()
        if kind != "text":
            code = value
        elif value["type"] == "pong":
            served += 1
        else:
            assert value["code"] == "rateLimited" and value["retryAfterMs"] > 0
            limited += 1
    assert code == 4429
    assert limited == 200  # the 201st closes instead of answering
    assert served <= 41 and len(rig.gui.requests) <= 41
    assert second.closed_with() == 4429
    client.close()
    second.close()
    other.send({"id": "o-1", "type": "ping"})
    assert other.message()["type"] == "pong"

    def login_attempt() -> tuple[Client, dict[str, Any]]:
        attempt, nonce = fresh(lambda: rig.hello(pin))
        attempt.send(phone.auth_request(pin, nonce))
        return attempt, attempt.message()

    for offset in (0.0, 29.0):
        clock.offset = offset
        attempt, reply = login_attempt()
        assert reply["code"] == "rateLimited", offset
        assert 0 < reply["retryAfterMs"] <= 30_000 - offset * 1000
        assert attempt.closed_with() == 4429
        attempt.close()
    clock.offset = 30.5
    attempt, reply = login_attempt()
    assert reply["type"] == "authOk"
    attempt.send({"id": "back", "type": "ping"})
    assert attempt.message()["type"] == "pong"


# ------------------------------------------------------- binds and outside peers


class _Ip:
    def __init__(self, text: str) -> None:
        self._text = text

    def toString(self) -> str:
        return self._text


class _Entry:
    def __init__(self, text: str) -> None:
        self._text = text

    def ip(self) -> _Ip:
        return _Ip(self._text)


class _Interface:
    def __init__(self, texts: list[str], up: bool = True) -> None:
        self._texts = texts
        self._up = up

    def flags(self) -> QNetworkInterface.InterfaceFlag:
        flag = QNetworkInterface.InterfaceFlag
        return (flag.IsUp | flag.IsRunning) if self._up else flag.IsUp

    def addressEntries(self) -> list[_Entry]:
        return [_Entry(text) for text in self._texts]


class _SpoofedSocket:
    """An accepted socket whose remote address is what the test says. Every
    real test connection comes from 127.0.0.1; this is how one "arrives" from
    8.8.8.8 or from a phone on the LAN."""

    def __init__(self, real: Any, peer: str) -> None:
        self._real = real
        self._peer = peer

    def peerAddress(self) -> QHostAddress:
        return QHostAddress(self._peer) if self._peer else QHostAddress()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class Net:
    """A made-up network for the listener: the interface list it reads, and
    servers that note the address they were asked to bind and then really
    listen on 127.0.0.1 (the made-up addresses do not exist on this PC)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.interfaces: list[str] = []
        self.down: list[str] = ["192.168.99.9"]
        self.asked: list[str] = []
        self.ports: dict[str, int] = {}
        self.peers: deque[str] = deque()
        net = self

        class Interfaces:
            InterfaceFlag = QNetworkInterface.InterfaceFlag

            @staticmethod
            def allInterfaces() -> list[_Interface]:
                return [_Interface(list(net.interfaces)), _Interface(list(net.down), up=False)]

        class Server(QTcpServer):
            def listen(self, address: Any = None, port: int | None = 0) -> bool:
                text = address.toString()
                net.asked.append(text)
                if not super().listen(QHostAddress(QHostAddress.SpecialAddress.LocalHost), 0):
                    return False
                net.ports[text] = int(self.serverPort())
                return True

            def nextPendingConnection(self) -> Any:
                sock = super().nextPendingConnection()
                if sock is not None and net.peers:
                    return _SpoofedSocket(sock, net.peers.popleft())
                return sock

        monkeypatch.setattr(QtNetwork, "QNetworkInterface", Interfaces)
        monkeypatch.setattr(listener_module, "QTcpServer", Server)
        monkeypatch.setattr(listener_module, "REFRESH_INTERVAL_MS", 100)


_INTERFACES = [
    "8.8.8.8",
    "192.168.77.5",
    "100.64.1.2",  # CGNAT / Tailscale IPv4: not private space
    "100.127.255.254",
    "10.9.8.7",
    "0.0.0.0",
    "::",
    "127.0.0.1",
    "::1",
    "172.16.5.4",
    "172.32.0.1",  # just past 172.16/12
    "169.254.10.20",
    "169.255.0.1",
    "2001:4860:4860::8888",
    "fd7a:115c:a1e0::5",
    "fe80::1c2d%7",
    "fe80::9",  # link-local without its interface cannot be bound
    "fec0::1",
    "192.169.0.1",
    "11.0.0.1",
    "224.0.0.1",
    "255.255.255.255",
    "203.0.113.7",
]
_PRIVATE = [
    "192.168.77.5",
    "10.9.8.7",
    "172.16.5.4",
    "169.254.10.20",
    "fd7a:115c:a1e0::5",
    "fe80::1c2d%7",
]


def test_only_private_and_link_local_addresses_are_bound(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA-11 with test mode off and an interface list holding public, CGNAT,
    wildcard, loopback and private addresses."""
    net = Net(monkeypatch)
    net.interfaces = list(_INTERFACES)
    rig.listener.set_test_mode(False)
    qr = rig.open_pairing()
    assert rig.listener.bound_addresses() == _PRIVATE
    assert net.asked == _PRIVATE  # nothing else was even tried
    for address in rig.listener.bound_addresses():
        assert is_private(address)
        assert address not in ("0.0.0.0", "::", "127.0.0.1", "::1", "192.168.99.9")
    listed = "192.168.77.5,10.9.8.7,172.16.5.4,169.254.10.20,[fd7a:115c:a1e0::5],[fe80::1c2d]"
    assert qr["a"] == listed  # the QR lists the same, no loopback, no 10.0.2.2
    # The interface list changes: sockets follow, still private only.
    net.interfaces = ["203.0.113.7", "192.168.77.5", "100.64.1.2", "10.20.30.40", "0.0.0.0"]
    assert pump(lambda: rig.listener.bound_addresses() == ["192.168.77.5", "10.20.30.40"])
    assert set(net.asked) == {*_PRIVATE, "10.20.30.40"}
    # The same list in test mode adds 127.0.0.1 and nothing else.
    rig.listener.set_test_mode(True)
    with_loopback = ["10.20.30.40", "127.0.0.1", "192.168.77.5"]
    assert pump(lambda: sorted(rig.listener.bound_addresses()) == with_loopback)
    assert set(net.asked) == {*_PRIVATE, "10.20.30.40", "127.0.0.1"}


def test_loopback_is_bound_only_in_test_mode(rig: Rig) -> None:
    """Real sockets on this PC's real interfaces: leaving test mode closes the
    loopback socket and every loopback connection."""
    phone, pin = rig.paired_phone()
    client = rig.login(phone, pin)
    port = rig.listener.port
    assert "127.0.0.1" in rig.listener.bound_addresses()
    rig.listener.set_test_mode(False)
    assert client.closed_with() == 1001
    assert pump(lambda: "127.0.0.1" not in rig.listener.bound_addresses())
    assert refused(port)
    for address in rig.listener.bound_addresses():
        assert is_private(address), address
    assert rig.store.count() == 1  # pairings are kept


def test_peers_outside_the_local_network_are_dropped_before_tls(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA-11: test mode off. A peer outside the private and link-local ranges
    is closed before the listener reads or writes one TLS byte."""
    net = Net(monkeypatch)
    net.interfaces = ["192.168.77.5"]
    rig.listener.set_test_mode(False)
    qr = rig.open_pairing()
    port = net.ports["192.168.77.5"]
    # As it really is: this test's own connection comes from 127.0.0.1.
    assert dropped_before_tls(port)
    outside = [
        "8.8.8.8",
        "100.64.0.1",
        "100.127.255.254",
        "172.32.0.1",
        "192.169.1.1",
        "169.255.0.1",
        "2001:4860:4860::8888",
        "::ffff:8.8.8.8",
        "::ffff:127.0.0.1",
        "127.0.0.1",
        "127.0.0.2",
        "::1",
        "0.0.0.0",
        "::",
        "fec0::1",
        "",
    ]
    for peer in outside:
        net.peers.append(peer)
        assert dropped_before_tls(port), peer
        assert not net.peers
    inside = [
        "192.168.77.9",
        "10.0.0.2",
        "172.31.255.254",
        "169.254.1.1",
        "::ffff:192.168.1.9",
        "fd00::1",
        "fe80::2%7",
    ]
    for peer in inside:
        net.peers.append(peer)
        client = Client(port, pin=qr["f"])
        rig.clients.append(client)
        assert client.status == 101 and client.message()["type"] == "hello", peer
        client.close()
        settle(0.05)
    # A phone on the LAN pairs and logs in; what it is told lists no loopback.
    net.peers.append("192.168.77.9")
    phone = Phone()
    client = Client(port, pin=qr["f"])
    rig.clients.append(client)
    nonce = b64url_decode(client.message()["nonce"])
    client.send(phone.pair_request(qr["c"], qr["f"], nonce))
    assert client.message()["type"] == "pairPending"
    rig.listener.approve_pairing()
    phone.device_id = client.message()["deviceId"]
    client.send(phone.auth_request(qr["f"], nonce))
    ok = client.message()
    assert ok["type"] == "authOk" and ok["addresses"] == ["192.168.77.5"]
    assert rig.gui.requests == []


def test_listener_stays_on_until_the_last_device_or_qr_is_gone(rig: Rig) -> None:
    """QA-11: no socket before a pairing; on while any phone is paired; off
    within 1 s of the last one going, and when a QR is closed with none."""
    port = free_port()
    rig.listener.set_port(port)
    rig.listener.start()
    assert not pump(lambda: rig.listener.running, timeout=0.3)
    assert refused(port)
    first, pin = rig.paired_phone("First")
    second, _pin = rig.paired_phone("Second")
    assert rig.listener.port == port
    assert rig.listener.revoke_device(first.device_id) is True
    assert not pump(lambda: not rig.listener.running, timeout=0.5)  # one phone is left
    client = rig.login(second, pin)
    started = time.monotonic()
    assert rig.listener.revoke_device(second.device_id) is True
    assert client.closed_with() == 4403
    assert pump(lambda: not rig.listener.running, timeout=1.0)
    assert time.monotonic() - started < 1.0
    assert refused(port) and rig.listener.bound_addresses() == []
    # A QR that is closed with nobody paired.
    rig.open_pairing()
    assert rig.listener.running and not refused(port)
    started = time.monotonic()
    rig.listener.cancel_pairing()
    assert pump(lambda: not rig.listener.running, timeout=1.0)
    assert time.monotonic() - started < 1.0
    assert refused(port) and rig.gui.finished[-1] == "cancelled"
    assert rig.store.count() == 0


def test_listener_package_has_no_upnp_or_datagram_code() -> None:
    """SPEC: "never opened in the router (no UPnP for it)". There is nothing
    to observe on the wire, so this checks that app/remote has no means to."""
    banned = re.compile(
        r"upnp|ssdp|nat-?pmp|\bigd\b|\bpcp\b|QUdpSocket|SOCK_DGRAM|239\.255\.255\.250|"
        r"AnyIPv[46]|SpecialAddress\.Any",
        re.IGNORECASE,
    )
    package = Path(listener_module.__file__).parent
    sources = sorted(package.glob("*.py"))
    assert len(sources) >= 10
    for source in sources:
        found = banned.search(source.read_text(encoding="utf-8"))
        assert found is None, f"{source.name}: {found.group(0) if found else ''}"


# --------------------------------------------------------------------- secrets


def test_no_secret_reaches_the_log(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    """A wrong code, a pairing, a failed login, a login, requests and a
    removal with every logger at DEBUG: no code, proof, nonce, signature, key,
    device id or URL query string is in any record."""
    caplog.set_level(logging.DEBUG)
    env.service.start_pairing()
    assert pump(lambda: bool(env.started))
    uri, manual, _expires = env.started[-1]
    qr = {k: v[0] for k, v in parse_qs(uri.split("?", 1)[1]).items()}
    code, pin = qr["c"], qr["f"]
    assert format_code(code) in manual
    phone = Phone("Pixel 8")
    client, nonce = env.connect(pin)
    wrong = phone.pair_request("0" * 12 if code != "0" * 12 else "1" * 12, pin, nonce)
    client.send(wrong)
    assert client.message()["code"] == "badCode"
    pair = phone.pair_request(code, pin, nonce)
    client.send(pair)
    assert client.message()["type"] == "pairPending"
    assert pump(lambda: bool(env.pending))
    env.service.approve_pairing()
    paired = client.message()
    assert paired["type"] == "paired"
    phone.device_id = paired["deviceId"]
    impostor = Phone()
    impostor.device_id = phone.device_id
    failing, failing_nonce = env.connect(pin)
    bad_auth = impostor.auth_request(pin, failing_nonce)
    failing.send(bad_auth)
    assert failing.message() == refusal("r-2")
    auth = phone.auth_request(pin, nonce)
    client.send(auth)
    assert client.message()["type"] == "authOk"
    assert ask(client, {"id": "p-1", "type": "ping"})["type"] == "pong"
    token = "tok-9f3c2a7d5e1b"
    download = {
        "url": f"https://example.com/a.zip?auth={token}",
        "requestId": "req-secret-0000000001",
    }
    assert ask(client, {"id": "d-1", "type": "download", **download})["type"] == "queued"
    assert ask(client, {"id": "s-1", "type": "subscribe"})["type"] == "subscribed"
    assert ask(client, {"id": "x-1", "type": "download", "url": "nope"})["code"] == "badRequest"
    assert env.service.remove_device(phone.device_id) is True
    assert client.closed_with() == 4403
    settle(0.3)

    secrets_sent = {
        "code": code,
        "code as shown": format_code(code),
        "wrong codeProof": wrong["codeProof"],
        "codeProof": pair["codeProof"],
        "publicKey": pair["publicKey"],
        "serverProof": paired["serverProof"],
        "nonce": b64url_encode(nonce),
        "nonce hex": nonce.hex(),
        "second nonce": b64url_encode(failing_nonce),
        "second nonce hex": failing_nonce.hex(),
        "signature": auth["signature"],
        "failed signature": bad_auth["signature"],
        "deviceId": phone.device_id,
        "pin": pin,
        "url query": token,
    }
    assert all(len(value) >= 12 for value in secrets_sent.values())
    # Records from the listener thread are captured too (the identity was
    # created there), so an empty result below is not an empty capture.
    assert any(record.name == "app.remote.certs" for record in caplog.records)
    formatter = logging.Formatter("%(name)s %(levelname)s %(message)s")
    for record in caplog.records:
        line = formatter.format(record) + repr(record.args)
        for what, value in secrets_sent.items():
            assert value not in line, f"{what} is logged by {record.name}"
