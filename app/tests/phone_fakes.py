"""A stand-in for ``RemoteService`` with its real public API and real Qt
signals, for the Settings -> Phone page and the Devices page tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from PySide6.QtCore import QObject, Signal

from app.remote.phonejobs import PhoneJob, PhoneSnapshot


def device(device_id: str, name: str, last_seen: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        device_id=device_id,
        name=name,
        public_key="",
        created_at=1_700_000_000,
        last_seen_at=last_seen,
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
