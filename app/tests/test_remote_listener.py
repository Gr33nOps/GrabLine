"""The phone-remote listener over real sockets (P1-D05).

Every test talks to a real ``RemoteListener`` on 127.0.0.1 (loopback test
mode) through a real TLS client that checks the certificate against the pin,
exactly as a phone would. TLS is Python's ``ssl`` on both sides, so nothing
here depends on a Qt TLS backend and nothing is skipped.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest
from PySide6.QtCore import QObject, Slot

from app.db.database import Database
from app.remote import addresses, wire
from app.remote import listener as listener_module
from app.remote.devices import DeviceStore, b64url_decode, b64url_encode
from app.remote.listener import RemoteListener
from app.remote.pairing import compute_server_proof
from app.remote.ratelimit import DeviceLimiter, FailureTracker, TokenBucket
from app.remote.schema import Request
from app.tests.qt_app import ensure_app
from app.tests.remote_client import Client, Phone, mask_frame, pump, settle

pytestmark = pytest.mark.timeout(60)


# ------------------------------------------------------------------ helpers


class FakeClock:
    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset


class Gui(QObject):
    """Stands in for the GUI thread's wiring: records signals and answers
    requests the way ``RemoteHandlers`` would."""

    def __init__(self, target: RemoteListener) -> None:
        super().__init__()
        self.target = target
        self.requests: list[tuple[int, str, Request]] = []
        self.hold = False
        self.started: list[tuple[str, str, int]] = []
        self.pending: list[str] = []
        self.kinds: list[str] = []
        self.finished: list[str] = []
        self.locked = 0
        self.states: list[bool] = []
        self.errors: list[str] = []
        self.thread_ids: set[int] = set()
        target.requestReady.connect(self.on_request)
        target.pairingStarted.connect(self.on_started)
        target.pairingPending.connect(self.on_pending)
        target.pairingFinished.connect(self.on_finished)
        target.pairingLockedWarning.connect(self.on_locked)
        target.stateChanged.connect(self.on_state)
        target.errorOccurred.connect(self.on_error)

    @Slot(int, str, object)
    def on_request(self, token: int, device_id: str, request: Request) -> None:
        self.thread_ids.add(threading.get_ident())
        self.requests.append((token, device_id, request))
        if self.hold:
            return
        if request.type == "ping":
            self.target.reply(token, {"type": "pong", "appRunning": True})
        elif request.type == "subscribe":
            self.target.reply(token, {"type": "subscribed", "state": {"paused": False}})
        else:
            self.target.replyError(token, "unsupported", "unknown type", None)

    @Slot(str, str, int)
    def on_started(self, uri: str, manual: str, expires: int) -> None:
        self.started.append((uri, manual, expires))

    @Slot(str, str)
    def on_pending(self, name: str, kind: str) -> None:
        self.kinds.append(kind)
        self.pending.append(name)

    @Slot(str)
    def on_finished(self, outcome: str) -> None:
        self.finished.append(outcome)

    @Slot()
    def on_locked(self) -> None:
        self.locked += 1

    @Slot(bool)
    def on_state(self, running: bool) -> None:
        self.states.append(running)

    @Slot(str)
    def on_error(self, code: str) -> None:
        self.errors.append(code)


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        ensure_app()
        self.db = Database(tmp_path / "remote-test.db")
        self.store = DeviceStore(self.db)
        self.clock = FakeClock()
        self.listener = RemoteListener(
            self.store,
            port=0,
            pc_name="Study PC",
            test_mode=True,
            identity_dir=tmp_path / "remote",
            clock=self.clock,
        )
        self.gui = Gui(self.listener)
        self.clients: list[Client] = []

    def close(self) -> None:
        for client in self.clients:
            client.close()
        self.listener.shutdown()
        self.db.close()

    def connect(self, **kwargs: Any) -> Client:
        client = Client(self.listener.port, **kwargs)
        self.clients.append(client)
        return client

    def hello(self, pin: str | None = None) -> tuple[Client, bytes]:
        client = self.connect(pin=pin)
        assert client.status == 101
        hello = client.message()
        assert hello["type"] == "hello"
        return client, b64url_decode(hello["nonce"])

    def open_pairing(self) -> dict[str, str]:
        before = len(self.gui.started)
        self.listener.start_pairing()
        assert pump(lambda: len(self.gui.started) > before), self.gui.errors
        uri = self.gui.started[-1][0]
        assert uri.startswith("grabline-pair:?")
        return {k: v[0] for k, v in parse_qs(uri.split("?", 1)[1]).items()}

    def paired_phone(self, name: str = "Pixel 8") -> tuple[Phone, str]:
        """Pair a phone through the real flow; returns it and the pin."""
        qr = self.open_pairing()
        phone = Phone(name)
        client, nonce = self.hello(qr["f"])
        client.send(phone.pair_request(qr["c"], qr["f"], nonce))
        assert client.message()["type"] == "pairPending"
        self.listener.approve_pairing()
        reply = client.message()
        assert reply["type"] == "paired", reply
        phone.device_id = reply["deviceId"]
        client.close()
        settle(0.05)
        return phone, qr["f"]

    def login(self, phone: Phone, pin: str) -> Client:
        client, nonce = self.hello(pin)
        client.send(phone.auth_request(pin, nonce))
        reply = client.message()
        assert reply["type"] == "authOk", reply
        return client


@pytest.fixture()
def rig(tmp_path: Path) -> Iterator[Rig]:
    made = Rig(tmp_path)
    try:
        yield made
    finally:
        made.close()


def _refused(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    except OSError:
        return True
    return False


# ----------------------------------------------------- off by default, binds


def test_no_socket_until_a_pairing_is_opened(rig: Rig) -> None:
    rig.listener.start()
    assert not pump(lambda: rig.listener.running, timeout=0.5)
    assert rig.listener.bound_addresses() == []


def test_bind_list_is_private_only_and_listener_stops_with_last_device(rig: Rig) -> None:
    """QA-11."""
    phone, pin = rig.paired_phone()
    bound = rig.listener.bound_addresses()
    assert "127.0.0.1" in bound  # test mode
    for address in bound:
        assert address not in ("0.0.0.0", "::")
        assert addresses.is_bindable(address, True)
        assert address == "127.0.0.1" or addresses.is_allowed_peer(address, False)
    port = rig.listener.port
    client = rig.login(phone, pin)
    started = time.monotonic()
    assert rig.listener.revoke_device(phone.device_id) is True
    assert client.closed_with() == 4403
    assert pump(lambda: not rig.listener.running, timeout=1.0)
    assert time.monotonic() - started < 1.0
    assert rig.listener.bound_addresses() == []
    assert _refused(port)
    assert rig.gui.states == [True, False]


def test_address_rules() -> None:
    candidates = [
        "8.8.8.8",
        "0.0.0.0",
        "::",
        "100.98.136.91",  # Tailscale IPv4: not private space
        "2400:adc7:122:d00::1",
        "fe80::1",  # link-local without a scope cannot be bound
        "fe80::1%eth0",
        "fd7a:115c:a1e0::6b32:885c",
        "192.168.1.6",
        "172.19.16.1",
        "172.32.0.1",
        "10.1.2.3",
        "169.254.6.113",
        "127.0.0.1",
        "::1",
    ]
    assert addresses.select_bind_addresses(candidates) == [
        "192.168.1.6",
        "172.19.16.1",
        "10.1.2.3",
        "169.254.6.113",
        "fd7a:115c:a1e0::6b32:885c",
        "fe80::1%eth0",
    ]
    with_loopback = addresses.select_bind_addresses(candidates, True)
    assert with_loopback[0] == "127.0.0.1" and "::1" not in with_loopback
    for peer in ("192.168.1.9", "10.0.0.1", "fe80::2%wlan0", "fd00::1", "::ffff:192.168.1.9"):
        assert addresses.is_allowed_peer(peer)
    outside: list[object] = ["8.8.8.8", "100.64.0.1", "2400::1", "::ffff:8.8.8.8", "::1", "", None]
    for stranger in outside:
        assert not addresses.is_allowed_peer(stranger, True)
    assert not addresses.is_allowed_peer("127.0.0.1")
    assert addresses.is_allowed_peer("127.0.0.1", True)
    assert not addresses.is_allowed_peer("127.0.0.2", True)
    listed = addresses.advertised(["127.0.0.1", "fe80::1%eth0", "fd00::1", "192.168.1.6"], True)
    assert listed == ["10.0.2.2", "192.168.1.6", "[fd00::1]", "[fe80::1]"]
    assert addresses.advertised(["127.0.0.1", "192.168.1.6"]) == ["192.168.1.6"]


# ------------------------------------------------------ handshake and hello


def test_hello_on_connect_with_pinned_tls(rig: Rig) -> None:
    qr = rig.open_pairing()
    assert qr["p"] == str(rig.listener.port) and "10.0.2.2" in qr["a"]
    client = rig.connect(pin=qr["f"])
    assert client.tls_version in ("TLSv1.2", "TLSv1.3")
    hello = client.message()
    assert set(hello) == {"type", "protocolVersion", "minClientVersion", "pcId", "pcName", "nonce"}
    assert hello["protocolVersion"] == 6 and hello["minClientVersion"] == 6
    assert hello["pcId"] == rig.store.pc_id() == qr["i"]
    assert hello["pcName"] == "Study PC"
    assert len(b64url_decode(hello["nonce"])) == 32
    _other, nonce = rig.hello(qr["f"])
    assert nonce != b64url_decode(hello["nonce"])


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_tls_below_1_2_is_refused(rig: Rig) -> None:
    rig.open_pairing()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        context.minimum_version = ssl.TLSVersion.TLSv1
        context.maximum_version = ssl.TLSVersion.TLSv1_1
    except (ValueError, ssl.SSLError):
        pytest.skip("this Python cannot offer TLS 1.1")
    raw = socket.create_connection(("127.0.0.1", rig.listener.port), timeout=5)
    with pytest.raises((ssl.SSLError, OSError)):
        context.wrap_socket(raw)
    raw.close()


def test_origin_header_and_wrong_path_are_refused(rig: Rig) -> None:
    rig.open_pairing()
    browser = rig.connect(headers=("Origin: https://evil.example",))
    assert browser.status == 403
    browser.close()
    wrong = rig.connect(path="/")
    assert wrong.status == 404
    wrong.close()
    settle()
    ok = rig.connect()
    assert ok.status == 101 and ok.message()["type"] == "hello"


# ------------------------------------------------------------- before login


def test_preauth_download_is_rejected_with_no_detail(rig: Rig) -> None:
    """QA-05."""
    rig.open_pairing()
    client, _nonce = rig.hello()
    client.send(
        {
            "id": "x-1",
            "type": "download",
            "url": "https://example.com/a.zip",
            "requestId": "req-bad-0000000000000001",
        }
    )
    assert client.message() == {
        "id": "x-1",
        "type": "error",
        "protocolVersion": 6,
        "code": "unauthorized",
        "message": "not authorized",
    }
    assert client.closed_with() == 4401
    settle(0.1)
    assert rig.gui.requests == []
    assert rig.store.count() == 0


def test_auth_with_unknown_device_or_bad_signature_is_the_same_refusal(rig: Rig) -> None:
    phone, pin = rig.paired_phone()
    stranger = Phone()
    stranger.device_id = b64url_encode(os.urandom(16))
    impostor = Phone()
    impostor.device_id = phone.device_id  # right id, wrong key
    replies = []
    for who in (stranger, impostor):
        client, nonce = rig.hello(pin)
        client.send(who.auth_request(pin, nonce))
        replies.append(client.message())
        assert client.closed_with() == 4401
        client.close()
        settle(0.05)
    assert replies[0] == replies[1]
    assert replies[0]["code"] == "unauthorized" and replies[0]["message"] == "not authorized"
    # A signature made for another connection's nonce does not work either.
    client, _nonce = rig.hello(pin)
    client.send(phone.auth_request(pin, os.urandom(32)))
    assert client.message()["code"] == "unauthorized"
    assert client.closed_with() == 4401


def test_old_client_is_told_to_update(rig: Rig) -> None:
    phone, pin = rig.paired_phone()
    client, nonce = rig.hello(pin)
    client.send(phone.auth_request(pin, nonce, version=5))
    reply = client.message()
    assert reply["code"] == "versionTooOld" and reply["minClientVersion"] == 6
    assert client.closed_with() == 4426


def test_auth_timeout(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(listener_module, "AUTH_TIMEOUT_S", 0.3)
    rig.open_pairing()
    client, _nonce = rig.hello()
    assert client.closed_with() == 4408


def test_unauthenticated_connections_are_capped_per_address(rig: Rig) -> None:
    rig.open_pairing()
    held = [rig.hello()[0] for _ in range(3)]
    with pytest.raises((OSError, ssl.SSLError)):
        rig.connect()
    held[0].close()
    settle(0.3)
    assert rig.hello()[0].status == 101


# ------------------------------------------------------------------ pairing


def test_pair_approve_then_login(rig: Rig) -> None:
    """QA-01: Allow stores the device and proves the PC knew the code."""
    qr = rig.open_pairing()
    phone = Phone("Pixel\u202e 8")
    client, nonce = rig.hello(qr["f"])
    client.send(phone.pair_request(qr["c"], qr["f"], nonce))
    assert client.message() == {"type": "pairPending", "protocolVersion": 6, "timeoutSeconds": 60}
    assert pump(lambda: rig.gui.pending == ["Pixel 8"])
    assert rig.store.count() == 0  # nothing is stored before Allow
    rig.listener.approve_pairing()
    reply = client.message()
    assert reply["id"] == "r-1" and reply["type"] == "paired"
    assert reply["pcId"] == qr["i"] and reply["pcName"] == "Study PC"
    phone.device_id = reply["deviceId"]
    expected = compute_server_proof(qr["c"], nonce, qr["f"], phone.public, phone.device_id)
    assert b64url_decode(reply["serverProof"]) == expected
    assert [d.device_id for d in rig.store.list()] == [phone.device_id]
    assert pump(lambda: rig.gui.finished == ["approved"])
    # The same connection logs in.
    client.send(phone.auth_request(qr["f"], nonce))
    ok = client.message()
    assert ok["type"] == "authOk" and ok["id"] == "r-2"
    assert ok["port"] == rig.listener.port and ok["appRunning"] is True
    assert ok["addresses"][0] == "10.0.2.2" and "127.0.0.1" not in ok["addresses"]
    assert ok["features"] == [
        "jobs",
        "control",
        "global",
        "queues",
        "analyze",
        "events",
        "files",
        "fetch",
        "outbox",
        "phoneJobs",
    ]
    # The code is single use.
    second, nonce2 = rig.hello(qr["f"])
    second.send(Phone().pair_request(qr["c"], qr["f"], nonce2))
    assert second.message()["code"] == "pairingExpired"


def test_pair_deny_stores_nothing(rig: Rig) -> None:
    """QA-04."""
    qr = rig.open_pairing()
    port = rig.listener.port
    client, nonce = rig.hello(qr["f"])
    client.send(Phone().pair_request(qr["c"], qr["f"], nonce))
    assert client.message()["type"] == "pairPending"
    rig.listener.deny_pairing()
    reply = client.message()
    assert reply["id"] == "r-1" and reply["code"] == "pairingDenied"
    assert client.closed_with() == 4403
    assert rig.store.count() == 0
    assert pump(lambda: rig.gui.finished == ["denied"])
    # No device and no open pairing: the listener is off again (QA-11).
    assert pump(lambda: not rig.listener.running, timeout=1.0)
    assert _refused(port)


def test_pair_after_expiry(rig: Rig) -> None:
    """QA-02: a code older than 120 s is dead; nothing is stored."""
    rig.paired_phone("Old phone")  # keeps the listener on after the expiry
    qr = rig.open_pairing()
    client, nonce = rig.hello(qr["f"])
    rig.clock.offset += 121
    client.send(Phone().pair_request(qr["c"], qr["f"], nonce))
    assert client.message()["code"] == "pairingExpired"
    assert rig.store.count() == 1
    assert pump(lambda: rig.gui.finished[-1] == "expired")
    assert rig.gui.pending == ["Old phone"]


def test_expired_qr_with_no_device_turns_the_listener_off(rig: Rig) -> None:
    rig.open_pairing()
    port = rig.listener.port
    rig.clock.offset += 121
    assert pump(lambda: not rig.listener.running, timeout=2.0)
    assert rig.gui.finished == ["expired"] and _refused(port)


def test_five_wrong_codes_lock_the_session(rig: Rig) -> None:
    """QA-03."""
    qr = rig.open_pairing()
    wrong = "0" * 12 if qr["c"] != "0" * 12 else "1" * 12
    answers: list[dict[str, Any]] = []
    for _ in range(3):
        client, nonce = rig.hello(qr["f"])
        for _ in range(2):
            if len(answers) < 5:
                client.send(Phone().pair_request(wrong, qr["f"], nonce))
                answers.append(client.message())
        client.close()
        settle(0.1)
    assert [a["code"] for a in answers] == ["badCode"] * 4 + ["pairingLocked"]
    assert [a.get("attemptsLeft") for a in answers] == [4, 3, 2, 1, None]
    assert pump(lambda: rig.gui.locked == 1 and rig.gui.finished == ["locked"])
    # The right code is now refused too, and nothing was stored.
    client, nonce = rig.hello(qr["f"])
    client.send(Phone().pair_request(qr["c"], qr["f"], nonce))
    assert client.message()["code"] == "pairingLocked"
    assert rig.store.count() == 0 and rig.gui.pending == []


def test_phone_leaving_during_the_prompt_stores_nothing(rig: Rig) -> None:
    qr = rig.open_pairing()
    client, nonce = rig.hello(qr["f"])
    client.send(Phone().pair_request(qr["c"], qr["f"], nonce))
    assert client.message()["type"] == "pairPending"
    client.close()
    assert pump(lambda: rig.gui.finished == ["cancelled"])
    rig.listener.approve_pairing()
    settle()
    assert rig.store.count() == 0


# -------------------------------------------------------------- after login


def test_ping_round_trip_through_the_gui(rig: Rig) -> None:
    phone, pin = rig.paired_phone()
    client = rig.login(phone, pin)
    client.send({"id": "p-1", "type": "ping"})
    assert client.message() == {
        "id": "p-1",
        "type": "pong",
        "protocolVersion": 6,
        "appRunning": True,
    }
    _token, device_id, request = rig.gui.requests[0]
    assert device_id == phone.device_id and request.type == "ping"
    assert rig.gui.thread_ids == {threading.get_ident()}  # handled on the GUI thread
    # Refused on the listener thread, never dispatched:
    client.send({"id": "p-2", "type": "download", "url": "https://e.com/a", "path": "C:/x"})
    assert client.message()["code"] == "forbidden"
    client.send({"id": "p-3", "type": "gallery"})
    assert client.message()["code"] == "unsupported"
    # A PC that does not advertise a feature answers its messages "unsupported".
    rig.listener._features = ("jobs", "events")
    client.send({"id": "p-4", "type": "files"})
    assert client.message()["code"] == "unsupported"
    assert len(rig.gui.requests) == 1
    client.send_raw(mask_frame(wire.OP_TEXT, b"not json"))
    assert client.closed_with() == 4400


def test_binary_frame_is_malformed(rig: Rig) -> None:
    phone, pin = rig.paired_phone()
    client = rig.login(phone, pin)
    client.send_raw(mask_frame(wire.OP_BINARY, b"\x00\x01"))
    assert client.closed_with() == 4400


def test_third_connection_replaces_the_oldest(rig: Rig) -> None:
    phone, pin = rig.paired_phone()
    first = rig.login(phone, pin)
    second = rig.login(phone, pin)
    third = rig.login(phone, pin)
    assert first.closed_with() == 4409
    for client in (second, third):
        client.send({"id": "p", "type": "ping"})
        assert client.message()["type"] == "pong"


def test_revoke_while_connected(rig: Rig) -> None:
    """QA-06: the removed phone is cut off at once and cannot come back."""
    phone, pin = rig.paired_phone("Stolen")
    keeper, _pin = rig.paired_phone("Keeper")
    client = rig.login(phone, pin)
    other = rig.login(keeper, pin)
    rig.gui.hold = True
    client.send({"id": "p-1", "type": "ping"})
    assert pump(lambda: len(rig.gui.requests) == 1)
    started = time.monotonic()
    assert rig.listener.revoke_device(phone.device_id) is True
    assert rig.store.get(phone.device_id) is None  # gone before revoke returned
    assert client.closed_with() == 4403
    # The request that was being handled gets no reply.
    rig.listener.reply(rig.gui.requests[0][0], {"type": "pong"})
    again, nonce = rig.hello(pin)
    again.send(phone.auth_request(pin, nonce))
    assert again.message()["code"] == "unauthorized"
    assert again.closed_with() == 4401
    assert time.monotonic() - started < 1.0
    client.close()
    assert client.next()[0] == "eof"
    rig.gui.hold = False
    other.send({"id": "k-1", "type": "ping"})
    assert other.message()["type"] == "pong"
    assert rig.listener.revoke_device(phone.device_id) is False


def test_unpair_from_the_phone(rig: Rig) -> None:
    phone, pin = rig.paired_phone()
    client = rig.login(phone, pin)
    client.send({"id": "u-1", "type": "unpair"})
    assert client.message() == {"id": "u-1", "type": "ok", "protocolVersion": 6}
    assert client.closed_with() == 1000
    assert rig.store.count() == 0
    assert pump(lambda: not rig.listener.running, timeout=1.0)


def test_oversized_frame_closes_1009_and_listener_keeps_running(rig: Rig) -> None:
    """QA-09."""
    phone, pin = rig.paired_phone()
    client = rig.login(phone, pin)
    # A full megabyte is fine after login ...
    client.send({"id": "big", "type": "ping", "pad": "x" * 1_000_000})
    assert client.message()["type"] == "pong"
    # ... a 2 MB frame is refused from its header; the payload is never read.
    client.send_raw(mask_frame(wire.OP_TEXT, b"x" * 65536, declared=2 * 1024 * 1024))
    assert client.closed_with() == 1009
    # Before login the limit is 4 KiB.
    early, _nonce = rig.hello(pin)
    early.send_raw(mask_frame(wire.OP_TEXT, b"x" * 5000))
    assert early.closed_with() == 1009
    assert rig.listener.running
    fresh = rig.login(phone, pin)
    fresh.send({"id": "p-1", "type": "ping"})
    assert fresh.message()["type"] == "pong"
    assert len(rig.gui.requests) == 2


def test_request_burst_is_rate_limited_before_dispatch(rig: Rig) -> None:
    """QA-10: 150 requests at once; the GUI sees only what the bucket allows."""
    phone, pin = rig.paired_phone()
    client = rig.login(phone, pin)
    started = time.monotonic()
    for index in range(150):
        client.send({"id": f"b-{index}", "type": "ping"})
    replies = [client.message() for _ in range(150)]
    elapsed = time.monotonic() - started
    limited = [r for r in replies if r["type"] == "error"]
    assert limited and all(r["code"] == "rateLimited" for r in limited)
    assert all(type(r["retryAfterMs"]) is int and r["retryAfterMs"] > 0 for r in limited)
    served = [r for r in replies if r["type"] == "pong"]
    # At least the 32 a connection may have unanswered, never more than the bucket.
    assert 32 <= len(served) <= 40 + 20 * elapsed + 1
    assert len(rig.gui.requests) == len(served)
    assert {r["id"] for r in replies} == {f"b-{i}" for i in range(150)}
    # The listener is still responsive once the bucket refills.
    time.sleep(0.2)
    client.send({"id": "after", "type": "ping"})
    assert client.message()["type"] == "pong"


def test_unanswered_requests_are_capped(rig: Rig) -> None:
    phone, pin = rig.paired_phone()
    client = rig.login(phone, pin)
    rig.gui.hold = True
    for index in range(33):
        client.send({"id": f"h-{index}", "type": "ping"})
    reply = client.message()
    assert reply["id"] == "h-32" and reply["code"] == "rateLimited"
    assert pump(lambda: len(rig.gui.requests) == 32)


def test_flood_closes_4429_and_refuses_the_device(rig: Rig) -> None:
    phone, pin = rig.paired_phone()
    client = rig.login(phone, pin)
    burst = b"".join(
        mask_frame(wire.OP_TEXT, json.dumps({"id": f"f-{i}", "type": "ping"}).encode())
        for i in range(400)
    )
    client.send_raw(burst)
    code = None
    for _ in range(401):
        kind, value = client.next()
        if kind != "text":
            code = value
            break
    assert code == 4429
    assert len(rig.gui.requests) <= 60
    client.close()
    again, nonce = rig.hello(pin)
    again.send(phone.auth_request(pin, nonce))
    assert again.message()["code"] == "rateLimited"
    assert again.closed_with() == 4429


def test_subscribe_and_event_overload_rules(rig: Rig) -> None:
    phone, pin = rig.paired_phone()
    client = rig.login(phone, pin)
    quiet = rig.login(phone, pin)
    client.send({"id": "s-1", "type": "subscribe", "topics": ["jobs", "nonsense"]})
    assert client.message() == {
        "id": "s-1",
        "type": "subscribed",
        "protocolVersion": 6,
        "state": {"paused": False},
        "topics": ["jobs"],
    }
    rig.listener.broadcast({"event": "global.updated", "state": {}})  # not subscribed
    rig.listener.broadcast({"event": "job.added", "job": {"id": 7}})
    first = client.message()
    assert first["type"] == "event" and first["event"] == "job.added"
    assert first["seq"] == 1 and first["job"] == {"id": 7} and first["protocolVersion"] == 6
    assert type(first["ts"]) is int
    for index in range(60):
        rig.listener.broadcast({"event": "job.progress", "jobId": 7, "downloaded": index})
    rig.listener.broadcast({"event": "job.updated", "job": {"id": 7, "name": "a"}})
    rig.listener.broadcast({"event": "job.updated", "job": {"id": 7, "name": "b"}})
    rig.listener.broadcast({"event": "job.finished", "job": {"id": 7}})
    events = []
    while True:
        event = client.message()
        events.append(event)
        if event["event"] == "job.finished":
            break
    names = [e["event"] for e in events]
    assert names.count("job.progress") == 19  # 20 a second, one was job.added
    assert "job.updated" not in names  # coalesced, then made pointless by the final state
    assert [e["seq"] for e in events] == list(range(2, len(events) + 2))  # no gap
    client.send({"id": "s-2", "type": "unsubscribe"})
    assert client.message()["type"] == "ok"
    rig.listener.broadcast({"event": "job.removed", "jobId": 7})
    quiet.send({"id": "q-1", "type": "ping"})
    assert quiet.message()["type"] == "pong"  # the second connection saw no event
    client.send({"id": "s-3", "type": "ping"})
    assert client.message()["type"] == "pong"


def test_stop_closes_connections_with_1001_and_keeps_pairings(rig: Rig) -> None:
    phone, pin = rig.paired_phone()
    client = rig.login(phone, pin)
    rig.listener.stop()
    assert client.closed_with() == 1001
    assert pump(lambda: not rig.listener.running)
    assert rig.store.count() == 1
    client.close()
    rig.listener.start()
    assert pump(lambda: rig.listener.running)
    again = rig.login(phone, pin)
    again.send({"id": "p", "type": "ping"})
    assert again.message()["type"] == "pong"


def test_port_in_use_is_reported(rig: Rig) -> None:
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    try:
        rig.listener.set_port(blocker.getsockname()[1])
        rig.listener.start_pairing()
        assert pump(lambda: "portInUse" in rig.gui.errors)
        assert "127.0.0.1" not in rig.listener.bound_addresses()
    finally:
        blocker.close()


# -------------------------------------------------------------- pure pieces


def test_token_bucket_and_flood_window() -> None:
    now = [0.0]
    bucket = TokenBucket(clock=lambda: now[0])
    assert [bucket.take() for _ in range(40)] == [0] * 40
    assert bucket.take() == 50  # one token is 50 ms away
    assert bucket.take(5) == 250
    now[0] += 0.25
    assert bucket.take(5) == 0 and bucket.take() > 0
    now[0] += 100
    assert [bucket.take(5) for _ in range(8)] == [0] * 8 and bucket.take() > 0

    limiter = DeviceLimiter(clock=lambda: now[0])
    assert not any(limiter.note_limited() for _ in range(200))
    assert limiter.refused_ms() == 0
    assert limiter.note_limited() is True
    assert limiter.refused_ms() == 30_000
    now[0] += 30
    assert limiter.refused_ms() == 0

    failures = FailureTracker(clock=lambda: now[0])
    for _ in range(9):
        failures.fail("10.0.0.5")
    assert not failures.refused("10.0.0.5")
    failures.fail("10.0.0.5")
    assert failures.refused("10.0.0.5") and not failures.refused("10.0.0.6")
    now[0] += 60
    assert not failures.refused("10.0.0.5")


def test_wire_parser_is_strict() -> None:
    key = base64.b64encode(bytes(16)).decode()
    head = (
        f"GET /remote HTTP/1.1\r\nHost: x\r\nUpgrade: WebSocket\r\nConnection: keep-alive, Upgrade"
        f"\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13"
    )
    status, accept = wire.parse_upgrade(head.encode())
    expected = base64.b64encode(
        hashlib.sha1(key.encode() + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()
    ).decode()
    assert (status, accept) == (101, expected)
    assert wire.parse_upgrade((head + "\r\norigin: null").encode())[0] == 403
    assert wire.parse_upgrade(head.replace("/remote", "/remote/").encode())[0] == 404
    assert wire.parse_upgrade(head.replace("GET", "POST").encode())[0] == 405
    assert wire.parse_upgrade(head.replace(": 13", ": 8").encode())[0] == 426
    assert wire.parse_upgrade(head.replace(key, "short").encode())[0] == 400
    assert wire.parse_upgrade(b"GET /remote HTTP/1.1")[0] == 400

    parser = wire.FrameParser(16, 4400)
    stream = mask_frame(wire.OP_PING, b"hi") + mask_frame(wire.OP_TEXT, b'{"a":1}')
    events: list[tuple[str, object]] = []
    for index in range(len(stream)):  # one byte at a time
        events.extend(parser.feed(stream[index : index + 1]))
    assert events == [("ping", b"hi"), ("text", '{"a":1}')]
    part1 = bytearray(mask_frame(wire.OP_TEXT, b"12345678"))
    part1[0] &= 0x7F  # not final
    assert parser.feed(bytes(part1)) == []
    assert parser.feed(mask_frame(wire.OP_CONTINUATION, b"123456789")) == [("fail", 1009)]
    assert parser.feed(mask_frame(wire.OP_TEXT, b"x")) == []  # dead after a failure
    assert wire.FrameParser(16).feed(wire.encode_frame(wire.OP_TEXT, b"x")) == [("fail", 1002)]
    compressed = bytearray(mask_frame(wire.OP_TEXT, b"x"))
    compressed[0] |= 0x40  # RSV1: permessage-deflate was never negotiated
    assert wire.FrameParser(16).feed(bytes(compressed)) == [("fail", 1002)]
    assert wire.FrameParser(16, 4400).feed(mask_frame(wire.OP_TEXT, b"\xff")) == [("fail", 4400)]
    assert wire.FrameParser(16, 4400).feed(mask_frame(wire.OP_BINARY, b"x")) == [("fail", 4400)]
    assert wire.FrameParser(16).feed(mask_frame(wire.OP_CLOSE, struct.pack(">H", 1000))) == [
        ("close", 1000)
    ]
