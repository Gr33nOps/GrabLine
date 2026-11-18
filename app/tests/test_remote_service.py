"""The phone-remote service wired to a real manager and database (P1-D06).

A real ``RemoteService`` runs on the GUI thread with a real ``DownloadManager``
and ``Database`` in a temp dir; a real TLS WebSocket client (the shared
``remote_client`` helpers) plays the phone over loopback test mode.
"""

from __future__ import annotations

import os
import socket
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from app.core.manager import DownloadManager
from app.core.models import JobStatus
from app.core.resolver import Resolver
from app.core.settings import Settings
from app.db.database import Database
from app.remote.service import RemoteService
from app.tests.qt_app import ensure_app
from app.tests.remote_client import Client, Phone, pump, settle

pytestmark = pytest.mark.timeout(60)

URL = "https://example.com/file.zip"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    return port


class Env:
    def __init__(self, tmp_path: Path) -> None:
        ensure_app()
        self.db = Database(tmp_path / "service-test.db")
        self.db.set_setting("remote_loopback_test", "1")
        self.db.set_setting("remote_port", str(_free_port()))
        self.settings = Settings(self.db)
        self.manager = DownloadManager(self.db, settings=self.settings, max_concurrent=0)
        self.service = RemoteService(
            self.manager, self.settings, Resolver(), identity_dir=tmp_path / "remote"
        )
        self.started: list[tuple[str, str, int]] = []
        self.pending: list[str] = []
        self.service.pairingStarted.connect(lambda *a: self.started.append(a))
        self.service.pairingPending.connect(lambda name, _kind: self.pending.append(name))
        self.clients: list[Client] = []
        self.dest = tmp_path / "downloads"
        self.dest.mkdir()

    def close(self) -> None:
        for client in self.clients:
            client.close()
        self.service.shutdown()
        self.manager.shutdown()
        self.db.close()

    def connect(self, pin: str) -> tuple[Client, bytes]:
        from app.remote.devices import b64url_decode

        client = Client(self.service.listener.port, pin=pin)
        self.clients.append(client)
        assert client.status == 101
        hello = client.message()
        assert hello["type"] == "hello"
        return client, b64url_decode(hello["nonce"])

    def pair(self) -> tuple[Phone, str]:
        self.service.start_pairing()
        assert pump(lambda: bool(self.started))
        uri = self.started[-1][0]
        qr = {k: v[0] for k, v in parse_qs(uri.split("?", 1)[1]).items()}
        phone = Phone()
        client, nonce = self.connect(qr["f"])
        client.send(phone.pair_request(qr["c"], qr["f"], nonce))
        assert client.message()["type"] == "pairPending"
        assert pump(lambda: bool(self.pending))
        self.service.approve_pairing()
        reply = client.message()
        assert reply["type"] == "paired", reply
        phone.device_id = reply["deviceId"]
        client.close()
        settle(0.05)
        return phone, qr["f"]

    def login(self, phone: Phone, pin: str) -> Client:
        client, nonce = self.connect(pin)
        client.send(phone.auth_request(pin, nonce))
        reply = client.message()
        assert reply["type"] == "authOk", reply
        return client

    def ask(self, client: Client, message: dict[str, Any]) -> dict[str, Any]:
        client.send(message)
        return client.message()


@pytest.fixture(autouse=True)
def _temp_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))


@pytest.fixture()
def env(tmp_path: Path) -> Iterator[Env]:
    made = Env(tmp_path)
    try:
        yield made
    finally:
        made.close()


def _download(request_id: str) -> dict[str, Any]:
    return {"id": "d-" + request_id[-4:], "type": "download", "url": URL, "requestId": request_id}


# ------------------------------------------------------------------- lazy


def test_listener_is_lazy_until_a_pairing_starts(env: Env) -> None:
    env.service.start()
    assert env.service.listener_created is False
    env.service.start_pairing()
    assert env.service.listener_created is True
    assert pump(lambda: bool(env.started))


# ---------------------------------------------------- pair, auth, download


def test_pair_through_the_service(env: Env) -> None:
    phone, _pin = env.pair()
    assert phone.device_id
    assert env.service.store.count() == 1


def test_download_becomes_a_phone_handoff_and_dedupes(env: Env) -> None:
    """QA-14."""
    phone, pin = env.pair()
    client = env.login(phone, pin)
    request_id = "req-service-000000000001"
    first = env.ask(client, _download(request_id))
    assert first["type"] == "queued" and first["duplicate"] is False
    again = env.ask(client, _download(request_id))
    assert again["duplicate"] is True and again["handoffId"] == first["handoffId"]
    handoffs = env.db.claim_handoffs()
    assert len(handoffs) == 1
    handoff = handoffs[0]
    assert handoff.id == first["handoffId"]
    assert handoff.url == URL
    assert handoff.source == "phone"
    assert handoff.headers == {}
    assert handoff.options["requestId"] == request_id


# ----------------------------------------------------------- subscribe, jobs


def test_subscribe_then_job_events_arrive(env: Env) -> None:
    """QA-13 / QA-16."""
    phone, pin = env.pair()
    client = env.login(phone, pin)
    reply = env.ask(client, {"id": "s-1", "type": "subscribe"})
    assert reply["type"] == "subscribed" and "state" in reply
    job = env.db.create_job(URL, str(env.dest), "a.zip")
    seen = _wait_event(client, lambda e: e["event"] in ("job.added", "job.updated"))
    assert seen["job"]["id"] == job.id
    env.db.set_job_status(job.id, JobStatus.PAUSED)
    seen = _wait_event(client, lambda e: e["event"] == "job.updated")
    assert seen["job"]["id"] == job.id


def _wait_event(client: Client, match: Any, timeout: float = 2.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            message = client.message(max(0.05, deadline - time.monotonic()))
        except AssertionError:
            break
        if message.get("type") == "event" and match(message):
            return message
    raise AssertionError("no matching job event within the time limit")


def test_control_pause_on_a_queued_job(env: Env) -> None:
    phone, pin = env.pair()
    client = env.login(phone, pin)
    job = env.db.create_job(URL, str(env.dest), "b.zip")
    reply = env.ask(client, {"id": "c-1", "type": "control", "jobId": job.id, "action": "pause"})
    assert reply["type"] == "ok", reply
    stored = env.db.get_job(job.id)
    assert stored is not None and stored.status is JobStatus.PAUSED


# ------------------------------------------------------------- revoke, exit


def test_remove_device_closes_the_connection(env: Env) -> None:
    """QA-06."""
    phone, pin = env.pair()
    client = env.login(phone, pin)
    started = time.monotonic()
    assert env.service.remove_device(phone.device_id) is True
    assert client.closed_with(1.0) == 4403
    assert time.monotonic() - started < 1.0
    assert env.service.store.count() == 0


def test_shutdown_is_idempotent(env: Env) -> None:
    phone, pin = env.pair()
    client = env.login(phone, pin)
    env.service.shutdown()
    env.service.shutdown()
    assert pump(lambda: client.next(1.0)[0] in ("close", "eof"), timeout=2.0)


# --------------------------------------------------------------- main window


def test_main_window_creates_the_service_but_no_listener(
    tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_app()
    from app.ui.main_window import MainWindow

    settings = Settings(db)
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    window = MainWindow(manager, settings)
    try:
        service = window._remote_service
        assert isinstance(service, RemoteService)
        assert service.listener_created is False
    finally:
        window.shutdown()
        manager.shutdown()
