"""The phone's own downloads, as the phone last reported them (PROTOCOL.md
section 20, feature ``phoneJobs``).

Memory only, never the database: the PC keeps the newest list per device with
the time it arrived, replaces (never merges) it on every report, and drops it
when the device is removed. Used from the GUI thread only.

Everything here is plain data. The strings came from the phone, so a UI shows
them as plain text only.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

ACTIONS = ("pause", "resume", "cancel", "retry", "remove", "moveToPc")

#: What the PC itself reports for a command that never reached a result.
RESULT_TIMEOUT = "timeout"  # the phone did not answer in 15 s
RESULT_OFFLINE = "offline"  # no connection of that device is listening
RESULT_BUSY = "busy"  # more than 10 commands a second to that device
#: What the phone reports (section 20 ``phoneCommandResult``).
PHONE_RESULTS = ("ok", "notFound", "refused", "failed")

_FINAL = ("completed", "failed", "cancelled")


@dataclass(frozen=True)
class PhoneJob:
    id: int
    name: str
    url: str
    status: str  # one of the section 20 statuses, or "unknown"
    kind: str
    downloaded: int
    total: int | None
    speed: int
    eta: int | None
    created_at: int
    error: str | None

    @classmethod
    def from_fields(cls, fields: dict[str, Any]) -> PhoneJob:
        """From one job dict that ``schema`` already validated."""
        return cls(
            id=int(fields["id"]),
            name=str(fields["name"]),
            url=str(fields["url"]),
            status=str(fields["status"]),
            kind=str(fields["kind"]),
            downloaded=int(fields["downloaded"]),
            total=None if fields["total"] is None else int(fields["total"]),
            speed=int(fields["speed"]),
            eta=None if fields["eta"] is None else int(fields["eta"]),
            created_at=int(fields["createdAt"]),
            error=None if fields["error"] is None else str(fields["error"]),
        )

    @property
    def fraction(self) -> float | None:
        """0..1, or None when the size is not known."""
        if not self.total:
            return None
        return max(0.0, min(1.0, self.downloaded / self.total))

    def actions(self) -> tuple[str, ...]:
        """The commands that fit this job's state, in the order a user wants
        them. The phone checks again; this only decides what the PC offers."""
        status = self.status
        if status not in ("queued", "downloading", "paused", "waiting", *_FINAL):
            return ()
        if status in ("queued", "downloading", "waiting"):
            base: tuple[str, ...] = ("pause", "cancel")
        elif status == "paused":
            base = ("resume", "cancel")
        elif status == "completed":
            base = ("remove",)
        else:  # failed, cancelled
            base = ("retry", "remove")
        # A copy from a PC has no link to hand over, and nothing to move.
        if self.kind != "transfer" and self.url:
            return (*base, "moveToPc")
        return base


@dataclass(frozen=True)
class PhoneSnapshot:
    """One device's newest report."""

    shared: bool
    jobs: tuple[PhoneJob, ...]
    received_at: int  # Unix seconds on this PC

    def job(self, job_id: int) -> PhoneJob | None:
        for job in self.jobs:
            if job.id == job_id:
                return job
        return None


class PhoneJobStore:
    """Newest ``phoneJobs`` list per device."""

    def __init__(self) -> None:
        self._by_device: dict[str, PhoneSnapshot] = {}

    def replace(
        self, device_id: str, shared: bool, jobs: Iterable[dict[str, Any]], now: float
    ) -> PhoneSnapshot:
        snapshot = PhoneSnapshot(
            bool(shared), tuple(PhoneJob.from_fields(j) for j in jobs), int(now)
        )
        self._by_device[device_id] = snapshot
        return snapshot

    def get(self, device_id: str) -> PhoneSnapshot | None:
        return self._by_device.get(device_id)

    def forget(self, device_id: str) -> None:
        self._by_device.pop(device_id, None)

    def retain(self, device_ids: Iterable[str]) -> list[str]:
        """Drop every list whose device is not in ``device_ids``; returns the
        devices dropped."""
        keep = set(device_ids)
        gone = [device for device in self._by_device if device not in keep]
        for device in gone:
            del self._by_device[device]
        return gone

    def __len__(self) -> int:
        return len(self._by_device)
