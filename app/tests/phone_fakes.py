"""A stand-in for ``RemoteService`` with its real public API and real Qt
signals, for the Settings -> Phone page and the Devices page tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from PySide6.QtCore import QObject, Signal

from app.remote.phonejobs import PhoneJob, PhoneSnapshot


def device(
    device_id: str,
    name: str,
    last_seen: int = 0,
    kind: str = "phone",
    client_pc_id: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        device_id=device_id,
        name=name,
        public_key="",
        created_at=1_700_000_000,
        last_seen_at=last_seen,
        kind=kind,
        client_pc_id=client_pc_id,
    )


def phone_job(job_id: int = 1, **changes: Any) -> PhoneJob:
    fields: dict[str, Any] = {
        "id": job_id,
        "name": f"file-{job_id}.iso",
        "url": f"https://example.org/{job_id}.iso",
        "status": "downloading",
        "kind": "direct",
        "downloaded": 250,
        "total": 1000,
        "speed": 1_048_576,
        "eta": 75,
        "createdAt": 1_700_000_000,
        "error": None,
    }
    fields.update(changes)
    return PhoneJob.from_fields(fields)


def snapshot(jobs: list[PhoneJob], *, shared: bool = True) -> PhoneSnapshot:
    return PhoneSnapshot(shared, tuple(jobs), 1_700_000_500)


class FakeStore:
    def __init__(self, devices: list[SimpleNamespace]) -> None:
        self.devices = devices

    def list(self) -> list[SimpleNamespace]:
        return list(self.devices)

    def count(self) -> int:
        return len(self.devices)


class FakeService(QObject):
    pairingStarted = Signal(str, str, int)
    pairingPending = Signal(str)
    pairingLockedWarning = Signal()
    pairingFinished = Signal(str)
    devicesChanged = Signal()
    stateChanged = Signal(bool)
    errorOccurred = Signal(str)
    phoneJobsChanged = Signal(str)
    commandFinished = Signal(int, str, str)
    deviceOnlineChanged = Signal(str)

    def __init__(self, devices: list[SimpleNamespace] | None = None) -> None:
        super().__init__()
        self.store = FakeStore(devices or [])
        self.listener_created = False
        self.calls: list[tuple[str, Any]] = []
        self.online: set[str] = set()
        self.lists: dict[str, PhoneSnapshot] = {}
        self.queued: list[tuple[str, str]] = []
        self.accept_links = True
        self._enabled = True
        self._port = 47821
        self._next_command = 1
        #: What the device behind the Allow prompt said it is.
        self.kind_pending = "phone"

    # settings
    def enabled_setting(self) -> bool:
        return self._enabled

    def port_setting(self) -> int:
        return self._port

    def set_enabled(self, on: bool) -> None:
        self._enabled = on
        self.calls.append(("set_enabled", on))

    def set_port(self, port: int) -> None:
        self._port = port
        self.calls.append(("set_port", port))

    # pairing
    def start_pairing(self) -> None:
        self.calls.append(("start_pairing", None))

    def cancel_pairing(self) -> None:
        self.calls.append(("cancel_pairing", None))

    def pending_kind(self) -> str:
        return self.kind_pending

    def approve_pairing(self) -> None:
        self.calls.append(("approve_pairing", None))

    def deny_pairing(self) -> None:
        self.calls.append(("deny_pairing", None))

    def remove_device(self, device_id: str) -> bool:
        self.calls.append(("remove_device", device_id))
        self.store.devices = [d for d in self.store.devices if d.device_id != device_id]
        self.lists.pop(device_id, None)
        return True

    # section 20
    def is_online(self, device_id: str) -> bool:
        return device_id in self.online

    def phone_jobs(self, device_id: str) -> PhoneSnapshot | None:
        return self.lists.get(device_id)

    def send_phone_command(self, device_id: str, job_id: int, action: str) -> int:
        self.calls.append(("command", (device_id, job_id, action)))
        command = self._next_command
        self._next_command += 1
        return command

    def queue_for_phone(
        self, device_id: str | None, url: str, title: str | None = None, quality: str | None = None
    ) -> int | None:
        self.calls.append(("queue_for_phone", (device_id, url)))
        if not self.accept_links:
            return None
        self.queued.append((str(device_id), url))
        return len(self.queued)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def link(
    pc_id: str,
    name: str,
    state: str = "online",
    *,
    features: tuple[str, ...] = ("jobs", "control", "events", "phoneJobs"),
    last_seen: int = 1_700_000_400,
) -> Any:
    from app.remote.client.service import LinkInfo

    return LinkInfo(
        pc_id=pc_id,
        name=name,
        state=state,
        address="192.168.1.20",
        port=47821,
        paired_at=1_700_000_100,
        last_seen_at=last_seen,
        features=frozenset(features),
        host_version="1.4.0",
    )


class FakeLinks(QObject):
    """A stand-in for ``LinkService`` with its real public API and Qt signals."""

    linksChanged = Signal()
    linkStateChanged = Signal(str)
    hostJobsChanged = Signal(str)
    requestFinished = Signal(int, str, str)
    linkProgress = Signal(str)
    linkFinished = Signal(str, str)

    def __init__(self, infos: list[Any] | None = None) -> None:
        super().__init__()
        self.infos: list[Any] = list(infos or [])
        self.jobs: dict[str, PhoneSnapshot] = {}
        self.calls: list[tuple[str, Any]] = []
        self.shared = True
        self.accept = True
        self.add_starts = True
        self._token = 1

    def links(self) -> list[Any]:
        return list(self.infos)

    def link(self, pc_id: str) -> Any | None:
        return next((i for i in self.infos if i.pc_id == pc_id), None)

    def is_online(self, pc_id: str) -> bool:
        info = self.link(pc_id)
        return info is not None and info.state == "online"

    def host_jobs(self, pc_id: str) -> PhoneSnapshot | None:
        return self.jobs.get(pc_id)

    def _next(self) -> int:
        token = self._token
        self._token += 1
        return token

    def send_control(self, pc_id: str, job_id: int, action: str) -> int:
        self.calls.append(("control", (pc_id, job_id, action)))
        return self._next() if self.accept else 0

    def send_download(self, pc_id: str, url: str, title: str | None = None) -> int:
        self.calls.append(("download", (pc_id, url)))
        return self._next() if self.accept else 0

    def send_links(self, pc_id: str, urls: list[str]) -> int:
        self.calls.append(("links", (pc_id, tuple(urls))))
        return self._next() if self.accept else 0

    def remove_link(self, pc_id: str) -> bool:
        self.calls.append(("remove_link", pc_id))
        self.infos = [i for i in self.infos if i.pc_id != pc_id]
        return True

    def add_link(self, address: str, code: str) -> bool:
        self.calls.append(("add_link", address))  # the code is never recorded
        return self.add_starts

    def cancel_add(self) -> None:
        self.calls.append(("cancel_add", None))

    def retry(self, pc_id: str) -> None:
        self.calls.append(("retry", pc_id))

    def shared_setting(self) -> bool:
        return self.shared

    def set_shared(self, on: bool) -> None:
        self.shared = on
        self.calls.append(("set_shared", on))
