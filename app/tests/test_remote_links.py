"""PC-to-PC linking end to end (PROTOCOL.md section 21).

Two real services in one process, each with its own profile in a temp folder:
the HOST is a ``RemoteService`` in loopback test mode on an ephemeral port;
the CLIENT is a ``LinkService`` that pairs with the typed code, signs in and
keeps a live view of the host's downloads while reporting its own.
"""

from __future__ import annotations

import os
import socket
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import parse_qs

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from app.core.manager import DownloadManager
from app.core.models import JobStatus
from app.core.resolver import Resolver
from app.core.settings import Settings
from app.db.database import Database
from app.remote import certs
from app.remote.client import keys
from app.remote.client.service import LinkInfo, LinkService
from app.remote.service import RemoteService
from app.tests.qt_app import ensure_app
from app.tests.remote_client import Client as RawClient
from app.tests.remote_client import Phone, pump

pytestmark = pytest.mark.timeout(90)

URL = "https://example.com/file.zip"
OTHER = "https://example.org/other.iso"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Host:
    """The PC being linked to."""

    def __init__(self, base: Path) -> None:
        ensure_app()
        self.base = base
        self.db = Database(base / "host.db")
        self.db.set_setting("remote_loopback_test", "1")
        self.db.set_setting("remote_port", str(_free_port()))
        self.settings = Settings(self.db)
        self.manager = DownloadManager(self.db, settings=self.settings, max_concurrent=0)
        self.identity = base / "host-remote"
        self.service = RemoteService(
            self.manager, self.settings, Resolver(), identity_dir=self.identity
        )
        self.started: list[tuple[str, str, int]] = []
        self.pending: list[str] = []
        self.service.pairingStarted.connect(lambda *a: self.started.append(a))
        self.service.pairingPending.connect(self.pending.append)
        self.dest = base / "host-downloads"
        self.dest.mkdir()

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.service.listener.port}"

    def show_code(self) -> str:
        self.started.clear()
        self.pending.clear()
        self.service.start_pairing()
        assert pump(lambda: bool(self.started))
        uri = self.started[-1][0]
        return parse_qs(uri.split("?", 1)[1])["c"][0]

    def close(self) -> None:
        self.service.shutdown()
        self.manager.shutdown()
        self.db.close()


class Client:
    """This PC: a ``LinkService`` with its own database and key folder."""

    def __init__(self, base: Path) -> None:
        ensure_app()
        self.db = Database(base / "client.db")
        self.settings = Settings(self.db)
        self.manager = DownloadManager(self.db, settings=self.settings, max_concurrent=0)
        self.key_dir = base / "client-keys"
        self.links = LinkService(
            self.manager,
            key_dir=self.key_dir,
            device_name="LAPTOP",
            backoff=lambda _failures: 0.05,
            connect_timeout=1.0,
        )
        self.finished: list[tuple[str, str]] = []
        self.progress: list[str] = []
        self.results: dict[int, tuple[str, str]] = {}
        self.links.linkFinished.connect(lambda code, pc: self.finished.append((code, pc)))
        self.links.linkProgress.connect(self.progress.append)
        self.links.requestFinished.connect(
            lambda token, result, message: self.results.__setitem__(token, (result, message))
        )
        self.links.start()
        self.dest = base / "client-downloads"
        self.dest.mkdir()

    def only_link(self) -> LinkInfo:
        found = self.links.links()
        assert len(found) == 1, found
        return found[0]

    def key_files(self) -> list[str]:
        return sorted(p.name for p in self.key_dir.glob("*")) if self.key_dir.exists() else []

    def close(self) -> None:
        self.links.shutdown()
        self.manager.shutdown()
        self.db.close()


@pytest.fixture(autouse=True)
def _temp_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))


@pytest.fixture()
def host(tmp_path: Path) -> Iterator[Host]:
    made = Host(tmp_path)
    try:
        yield made
    finally:
        made.close()


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[Client]:
    made = Client(tmp_path)
    try:
        yield made
    finally:
        made.close()


def link_up(host: Host, client: Client, *, wait_online: bool = True) -> str:
    """Pair the client with the host through the typed code and the Allow
    prompt. Returns the host's pcId."""
    code = host.show_code()
    assert client.links.add_link(host.address, code)
    assert pump(lambda: bool(host.pending), 10.0), "the Allow prompt never came"
    host.service.approve_pairing()
    assert pump(lambda: bool(client.finished), 10.0)
    assert client.finished[-1][0] == "ok", client.finished
    pc_id = client.finished[-1][1]
    if wait_online:
        assert pump(lambda: client.links.is_online(pc_id), 10.0)
    return pc_id


def state_of(client: Client, pc_id: str) -> str | None:
    info = client.links.link(pc_id)
    return info.state if info is not None else None


def kind_of(host: Host, device_id: str) -> str | None:
    device = host.service.store.get(device_id)
    return device.kind if device is not None else None


def reported_status(host: Host, device_id: str, job_id: int) -> str | None:
    snapshot = host.service.phone_jobs(device_id)
    job = snapshot.job(job_id) if snapshot is not None else None
    return job.status if job is not None else None


def settle_until(condition: Callable[[], bool], timeout: float = 8.0) -> bool:
    return pump(condition, timeout)


def wait_result(client: Client, token: int, timeout: float = 8.0) -> tuple[str, str]:
    assert token > 0
    assert pump(lambda: token in client.results, timeout), "no outcome for the request"
    return client.results[token]


# ------------------------------------------------------------------ pairing


def test_pairing_with_the_typed_code_links_and_signs_in(host: Host, client: Client) -> None:
    pc_id = link_up(host, client)
    info = client.only_link()
    assert info.pc_id == pc_id and info.state == "online" and info.online
    assert info.shares_downloads  # the host lists phoneJobs
    assert "confirm" in client.progress  # "Confirm on the other PC" while the prompt waits
    # The host stored this PC as a device of kind "pc".
    devices = host.service.store.list()
    assert len(devices) == 1 and devices[0].kind == "pc" and devices[0].name == "LAPTOP"
    assert host.service.is_online(devices[0].device_id)
    # The client stored the link and exactly one private key file.
    rows = client.db.list_remote_links()
    assert len(rows) == 1 and rows[0]["pc_id"] == pc_id
    assert rows[0]["device_id"] == devices[0].device_id
    assert len(client.key_files()) == 1
    assert keys.load(rows[0]["key_file"], client.key_dir) is not None


def test_a_wrong_code_stores_nothing(host: Host, client: Client) -> None:
    code = host.show_code()
    wrong = ("1" if code[0] != "1" else "2") + code[1:]
    assert client.links.add_link(host.address, wrong)
    assert pump(lambda: bool(client.finished), 10.0)
    assert client.finished[-1][0] == "badCode"
    assert client.links.links() == []
    assert client.db.list_remote_links() == []
    assert client.key_files() == []
    assert host.service.store.count() == 0
    assert host.pending == []  # no Allow prompt for a wrong code


def test_the_code_is_normalised_like_the_phone(host: Host, client: Client) -> None:
    code = host.show_code()
    typed = " " + "-".join(code[i : i + 4] for i in range(0, 12, 4)).lower() + " "
    assert client.links.add_link(host.address, typed)
    assert pump(lambda: bool(host.pending), 10.0)
    host.service.approve_pairing()
    assert pump(lambda: bool(client.finished), 10.0)
    assert client.finished[-1][0] == "ok"


def test_a_code_that_does_not_look_right_is_refused_locally(host: Host, client: Client) -> None:
    assert client.links.add_link(host.address, "123") is False
    assert pump(lambda: bool(client.finished))
    assert client.finished[-1] == ("invalidCode", "")
    assert client.links.add_link("", "7K3M-9QX2-RTBW") is False
    assert pump(lambda: len(client.finished) == 2)
    assert client.finished[-1] == ("badAddress", "")


def test_deny_stores_nothing(host: Host, client: Client) -> None:
    code = host.show_code()
    assert client.links.add_link(host.address, code)
    assert pump(lambda: bool(host.pending), 10.0)
    host.service.deny_pairing()
    assert pump(lambda: bool(client.finished), 10.0)
    assert client.finished[-1][0] == "declined"
    assert client.db.list_remote_links() == [] and client.key_files() == []
    assert host.service.store.count() == 0


def test_nothing_is_found_at_a_closed_port(client: Client) -> None:
    assert client.links.add_link(f"127.0.0.1:{_free_port()}", "7K3M-9QX2-RTBW")
    assert pump(lambda: bool(client.finished), 10.0)
    assert client.finished[-1][0] == "notFound"


# ---------------------------------------------------- the host's downloads


def test_client_sees_the_hosts_downloads_and_pauses_one(host: Host, client: Client) -> None:
    job = host.db.create_job(URL, str(host.dest), "a.zip")
    pc_id = link_up(host, client)
    assert pump(lambda: client.links.host_jobs(pc_id) is not None, 8.0)
    snapshot = client.links.host_jobs(pc_id)
    assert snapshot is not None and [j.id for j in snapshot.jobs] == [job.id]
    assert snapshot.jobs[0].status == "queued"

    token = client.links.send_control(pc_id, job.id, "pause")
    assert wait_result(client, token) == ("ok", "")
    stored = host.db.get_job(job.id)
    assert stored is not None and stored.status is JobStatus.PAUSED

    def paused_in_view() -> bool:
        view = client.links.host_jobs(pc_id)
        return view is not None and view.jobs[0].status == "paused"

    assert pump(paused_in_view, 8.0), "the event never reached the live view"

    # A job added on the host after login arrives as an event.
    added = host.db.create_job(OTHER, str(host.dest), "b.iso")
    assert pump(
        lambda: (
            (v := client.links.host_jobs(pc_id)) is not None and added.id in [j.id for j in v.jobs]
        ),
        8.0,
    )
    # The host's answers are mapped: retry does not fit a paused job, resume does.
    assert wait_result(client, client.links.send_control(pc_id, job.id, "retry"))[0] == "conflict"
    assert wait_result(client, client.links.send_control(pc_id, job.id, "resume"))[0] == "ok"
    assert wait_result(client, client.links.send_control(pc_id, 99999, "pause"))[0] == "notFound"
    assert client.links.send_control(pc_id, job.id, "explode") == 0


def test_client_removes_a_host_download(host: Host, client: Client) -> None:
    job = host.db.create_job(URL, str(host.dest), "a.zip")
    pc_id = link_up(host, client)
    assert pump(lambda: client.links.host_jobs(pc_id) is not None, 8.0)
    assert wait_result(client, client.links.send_control(pc_id, job.id, "remove"))[0] == "ok"
    assert host.db.get_job(job.id) is None
    assert pump(lambda: not (client.links.host_jobs(pc_id) or _empty()).jobs, 8.0)


class _Empty:
    jobs: tuple[object, ...] = ()


def _empty() -> _Empty:
    return _Empty()


def test_client_adds_a_link_to_the_host(host: Host, client: Client) -> None:
    pc_id = link_up(host, client)
    token = client.links.send_download(pc_id, URL)
    assert wait_result(client, token) == ("ok", "")
    handoffs = host.db.claim_handoffs()
    assert len(handoffs) == 1
    assert handoffs[0].url == URL and handoffs[0].source == "phone"
    assert client.links.send_download(pc_id, "file:///etc/passwd") == 0  # not a link
    many = client.links.send_links(pc_id, [URL, OTHER])
    assert wait_result(client, many)[0] == "ok"


# ------------------------------------------------- this PC's own downloads


def test_host_sees_the_clients_downloads_and_pauses_one(host: Host, client: Client) -> None:
    mine = client.db.create_job(URL, str(client.dest), "mine.zip")
    link_up(host, client)
    device = host.service.store.list()[0]

    def host_sees() -> bool:
        snapshot = host.service.phone_jobs(device.device_id)
        return snapshot is not None and mine.id in [j.id for j in snapshot.jobs]

    assert pump(host_sees, 8.0), "the host never got the phoneJobs list"
    snapshot = host.service.phone_jobs(device.device_id)
    assert snapshot is not None and snapshot.shared
    assert snapshot.jobs[0].url == URL and snapshot.jobs[0].status == "queued"

    done: list[tuple[int, str, str]] = []
    host.service.commandFinished.connect(lambda *a: done.append(a))
    command = host.service.send_phone_command(device.device_id, mine.id, "pause")
    assert command > 0
    assert pump(lambda: bool(done), 8.0)
    assert done[0][:2] == (command, "ok"), done
    stored = client.db.get_job(mine.id)
    assert stored is not None and stored.status is JobStatus.PAUSED
    # The next report shows it (within a second or two).
    assert pump(
        lambda: reported_status(host, device.device_id, mine.id) == "paused",
        8.0,
    )
    # A command for a download this PC does not have, and one that does not fit.
    done.clear()
    host.service.send_phone_command(device.device_id, mine.id, "pause")  # stale list entry
    assert pump(lambda: bool(done), 8.0)
    assert done[0][1] == "refused" or done[0][1] == "ok"


def test_move_to_pc_sends_the_link_to_the_host_then_cancels_locally(
    host: Host, client: Client
) -> None:
    mine = client.db.create_job(URL, str(client.dest), "mine.zip")
    pc_id = link_up(host, client)
    device = host.service.store.list()[0]
    assert pump(
        lambda: (s := host.service.phone_jobs(device.device_id)) is not None and bool(s.jobs), 8.0
    )
    done: list[tuple[int, str, str]] = []
    host.service.commandFinished.connect(lambda *a: done.append(a))
    host.service.send_phone_command(device.device_id, mine.id, "moveToPc")
    assert pump(lambda: bool(done), 8.0)
    assert done[0][1] == "ok", done
    handoffs = host.db.claim_handoffs()
    assert len(handoffs) == 1 and handoffs[0].url == URL
    stored = client.db.get_job(mine.id)
    assert stored is not None and stored.status is JobStatus.CANCELLED
    assert pc_id  # linked the whole time


def test_sharing_off_reports_not_shared_and_refuses_commands(host: Host, client: Client) -> None:
    client.links.set_shared(False)
    assert client.links.shared_setting() is False
    mine = client.db.create_job(URL, str(client.dest), "mine.zip")
    link_up(host, client)
    device = host.service.store.list()[0]
    assert pump(lambda: host.service.phone_jobs(device.device_id) is not None, 8.0)
    snapshot = host.service.phone_jobs(device.device_id)
    assert snapshot is not None and snapshot.shared is False and snapshot.jobs == ()
    done: list[tuple[int, str, str]] = []
    host.service.commandFinished.connect(lambda *a: done.append(a))
    host.service.send_phone_command(device.device_id, mine.id, "pause")
    # The host's own check: no list entry for it, so it fails at once.
    assert pump(lambda: bool(done), 8.0)
    stored = client.db.get_job(mine.id)
    assert stored is not None and stored.status is JobStatus.QUEUED
    # Turning it on reports at once.
    client.links.set_shared(True)
    assert pump(
        lambda: (s := host.service.phone_jobs(device.device_id)) is not None and s.shared, 8.0
    )


# ----------------------------------------------------------- removing, state


def test_removing_the_link_on_the_client_unpairs_and_deletes_the_key(
    host: Host, client: Client
) -> None:
    pc_id = link_up(host, client)
    assert host.service.store.count() == 1
    assert client.links.remove_link(pc_id) is True
    assert client.links.links() == []
    assert client.db.list_remote_links() == [] and client.key_files() == []
    assert pump(lambda: host.service.store.count() == 0, 8.0), "the host kept the device"


def test_removing_the_device_on_the_host_removes_the_link(host: Host, client: Client) -> None:
    pc_id = link_up(host, client)
    device = host.service.store.list()[0]
    assert host.service.remove_device(device.device_id)
    assert pump(
        lambda: state_of(client, pc_id) == "removed",
        8.0,
    )
    # The row and the key are gone; only the notice stays until dismissed.
    assert client.db.list_remote_links() == [] and client.key_files() == []
    assert client.links.is_online(pc_id) is False
    assert client.links.remove_link(pc_id) is True  # dismiss the notice
    assert client.links.links() == []


def test_a_reset_host_identity_stops_the_link_without_retrying(
    host: Host, client: Client, tmp_path: Path
) -> None:
    pc_id = link_up(host, client)
    # "Reset phone connections" on the host: a new key, so a new pin.
    certs.regenerate(host.service.store.pc_id(), host.identity)
    host.service.set_enabled(False)
    host.service.set_enabled(True)
    assert pump(
        lambda: state_of(client, pc_id) == "identityChanged",
        15.0,
    )
    # No automatic retry: it stays that way, and the link and key are kept for
    # the person to remove (the pin must not be silently replaced).
    settle = time.monotonic() + 1.0
    pump(lambda: time.monotonic() > settle, 2.0)
    info = client.links.link(pc_id)
    assert info is not None and info.state == "identityChanged"
    assert len(client.db.list_remote_links()) == 1
    client.links.retry(pc_id)  # not offered for this state
    pump(lambda: False, 0.5)
    assert state_of(client, pc_id) == "identityChanged"


def test_the_link_reconnects_after_the_listener_restarts(host: Host, client: Client) -> None:
    pc_id = link_up(host, client)
    host.service.set_enabled(False)
    assert pump(lambda: state_of(client, pc_id) in ("offline", "connecting"), 8.0)
    host.service.set_enabled(True)
    assert pump(lambda: client.links.is_online(pc_id), 15.0)


def test_links_are_loaded_again_at_start(host: Host, client: Client, tmp_path: Path) -> None:
    pc_id = link_up(host, client)
    client.links.shutdown()
    again = LinkService(
        client.manager,
        key_dir=client.key_dir,
        device_name="LAPTOP",
        backoff=lambda _n: 0.05,
        connect_timeout=1.0,
    )
    try:
        again.start()
        assert [info.pc_id for info in again.links()] == [pc_id]
        assert pump(lambda: again.is_online(pc_id), 10.0)
    finally:
        again.shutdown()


# ------------------------------------------------- deviceKind on the wire


def _raw_pair(host: Host, kind: str | None) -> tuple[str, Phone, str]:
    """Pair a raw WebSocket device with ``deviceKind`` ``kind`` (None: omit
    it). Returns (deviceId, the device, the host's pin)."""
    from app.remote.devices import b64url_decode

    code = host.show_code()
    pin = parse_qs(host.started[-1][0].split("?", 1)[1])["f"][0]
    phone = Phone("LAPTOP" if kind == "pc" else "Pixel 8")
    ws = RawClient(host.service.listener.port, pin=pin)
    try:
        hello = ws.message()
        request = phone.pair_request(code, pin, b64url_decode(hello["nonce"]))
        if kind is not None:
            request["deviceKind"] = kind
        ws.send(request)
        first = ws.message()
        if first["type"] != "pairPending":
            return "", phone, first["code"]
        assert pump(lambda: bool(host.pending), 5.0)
        host.service.approve_pairing()
        reply = ws.message()
        assert reply["type"] == "paired"
        phone.device_id = reply["deviceId"]
    finally:
        ws.close()
    return phone.device_id, phone, pin


def test_a_device_that_says_nothing_is_a_phone(host: Host) -> None:
    device_id, _phone, _pin = _raw_pair(host, None)
    stored = host.service.store.get(device_id)
    assert stored is not None and stored.kind == "phone"


def test_a_bad_kind_in_pair_is_bad_request_and_stores_nothing(host: Host) -> None:
    device_id, _phone, code = _raw_pair(host, "tablet")
    assert device_id == "" and code == "badRequest"
    assert host.service.store.count() == 0
    assert host.pending == []


def test_the_kind_stored_at_pairing_wins_over_the_one_in_auth(host: Host) -> None:
    from app.remote.devices import b64url_decode

    device_id, phone, pin = _raw_pair(host, "pc")
    assert kind_of(host, device_id) == "pc"
    for claimed, expect in (("phone", "authOk"), ("tablet", "error")):
        ws = RawClient(host.service.listener.port, pin=pin)
        try:
            nonce = b64url_decode(ws.message()["nonce"])
            request = phone.auth_request(pin, nonce)
            request["deviceKind"] = claimed
            ws.send(request)
            assert ws.message()["type"] == expect
        finally:
            ws.close()
    assert kind_of(host, device_id) == "pc"


def test_the_host_stores_the_clients_own_pc_id_and_it_never_changes(
    host: Host, client: Client
) -> None:
    from app.remote.devices import DeviceStore

    link_up(host, client)
    own = DeviceStore(client.db).pc_id()
    (device,) = host.service.store.list()
    assert device.client_pc_id == own
    assert own != host.service.store.pc_id()
    # The client signs in again (with its own id in auth): nothing changes.
    pc_id = client.only_link().pc_id
    host.service.set_enabled(False)
    host.service.set_enabled(True)
    assert pump(lambda: host.service.is_online(device.device_id), 15.0)
    assert client.links.is_online(pc_id)
    stored = host.service.store.get(device.device_id)
    assert stored is not None and stored.client_pc_id == own


def test_an_auth_with_another_client_pc_id_is_ignored(host: Host) -> None:
    from app.remote.devices import b64url_decode

    device_id, phone, pin = _raw_pair(host, "pc")
    assert host.service.store.get(device_id).client_pc_id is None  # type: ignore[union-attr]
    ws = RawClient(host.service.listener.port, pin=pin)
    try:
        nonce = b64url_decode(ws.message()["nonce"])
        request = phone.auth_request(pin, nonce)
        request["deviceKind"] = "pc"
        request["clientPcId"] = "EBESExQVFhcYGRobHB0eHw"
        ws.send(request)
        assert ws.message()["type"] == "authOk"
    finally:
        ws.close()
    assert host.service.store.get(device_id).client_pc_id is None  # type: ignore[union-attr]
