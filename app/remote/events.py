"""Live job events for subscribed phones (PROTOCOL.md section 15).

``EventTracker`` is fed the manager's job snapshot (``JobView`` list) on the
GUI thread, at most every 0.5 s and only while a phone is subscribed. It diffs
against the previous snapshot and returns the events to broadcast:

- ``job.added`` / ``job.removed`` as jobs appear and disappear;
- ``job.finished`` / ``job.failed`` when the status becomes completed / failed;
- ``job.updated`` for other status, name, total or queue changes (at most
  5 per second per job, coalesced to the latest);
- ``job.progress`` while bytes move (at most 1 per second per job);
- ``global.updated`` when pause state, limit or counts change (at most 1/s).

It also measures speed (bytes per second, smoothed) and ETA per job, which the
handlers use for job objects and the global state.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from app.core.manager import JobView
from app.core.models import JobStatus
from app.remote.handlers import LiveFigures

PROGRESS_INTERVAL = 1.0
UPDATE_INTERVAL = 0.2
GLOBAL_INTERVAL = 1.0
_SMOOTHING = 0.4  # weight of the newest sample in the speed average


@dataclass
class _Seen:
    status: JobStatus
    name: str
    total: int | None
    queue_id: int | None
    downloaded: int
    sample_at: float
    speed: float = 0.0
    progress_sent_at: float = 0.0
    progress_sent_bytes: int = -1
    update_sent_at: float = 0.0
    update_pending: bool = False


class EventTracker:
    def __init__(
        self,
        job_object: Callable[[int], dict[str, Any] | None],
        global_state: Callable[[], dict[str, Any]],
    ) -> None:
        self._job_object = job_object
        self._global_state = global_state
        self._seen: dict[int, _Seen] = {}
        self._primed = False
        self._last_global: dict[str, Any] | None = None
        self._global_sent_at = 0.0
        self._global_pending = False

    # ------------------------------------------------------------ figures

    def live(self, job_id: int) -> LiveFigures:
        seen = self._seen.get(job_id)
        if seen is None or seen.status is not JobStatus.DOWNLOADING:
            return LiveFigures()
        speed = int(seen.speed)
        eta = None
        if speed > 0 and seen.total and seen.total > seen.downloaded:
            eta = int((seen.total - seen.downloaded) / speed)
        return LiveFigures(speed=speed, eta=eta)

    # -------------------------------------------------------------- diffing

    def update(self, views: Iterable[JobView], now: float) -> list[dict[str, Any]]:
        """Return events (without seq/ts/protocolVersion) for this snapshot."""
        events: list[dict[str, Any]] = []
        current = {view.id: view for view in views}
        first = not self._primed
        self._primed = True
        for job_id in [j for j in self._seen if j not in current]:
            del self._seen[job_id]
            if not first:
                events.append({"event": "job.removed", "jobId": job_id})
        for job_id, view in current.items():
            name = view.display_name
            seen = self._seen.get(job_id)
            if seen is None:
                self._seen[job_id] = _Seen(
                    view.status, name, view.total_size, view.queue_id, view.downloaded, now
                )
                if not first:
                    events.extend(self._job_event("job.added", job_id))
                continue
            self._sample(seen, view.downloaded, now)
            status_changed = view.status is not seen.status
            meta_changed = (
                name != seen.name or view.total_size != seen.total or view.queue_id != seen.queue_id
            )
            seen.name, seen.total, seen.queue_id = name, view.total_size, view.queue_id
            if status_changed:
                seen.status = view.status
                if view.status is JobStatus.COMPLETED:
                    events.extend(self._job_event("job.finished", job_id))
                elif view.status is JobStatus.FAILED:
                    events.extend(self._job_event("job.failed", job_id))
                else:
                    seen.update_pending = True
            elif meta_changed:
                seen.update_pending = True
            if seen.update_pending and now - seen.update_sent_at >= UPDATE_INTERVAL:
                seen.update_pending = False
                seen.update_sent_at = now
                events.extend(self._job_event("job.updated", job_id))
            if (
                view.status is JobStatus.DOWNLOADING
                and view.downloaded != seen.progress_sent_bytes
                and now - seen.progress_sent_at >= PROGRESS_INTERVAL
            ):
                seen.progress_sent_at = now
                seen.progress_sent_bytes = view.downloaded
                live = self.live(job_id)
                events.append(
                    {
                        "event": "job.progress",
                        "jobId": job_id,
                        "status": view.status.value,
                        "downloaded": max(0, view.downloaded),
                        "total": view.total_size if view.total_size else None,
                        "speed": live.speed,
                        "eta": live.eta,
                    }
                )
        events.extend(self._global_events(now))
        return events

    def _sample(self, seen: _Seen, downloaded: int, now: float) -> None:
        elapsed = now - seen.sample_at
        if elapsed <= 0:
            return
        delta = max(0, downloaded - seen.downloaded)
        instant = delta / elapsed
        seen.speed = (
            instant if seen.speed == 0 else (_SMOOTHING * instant + (1 - _SMOOTHING) * seen.speed)
        )
        seen.downloaded = downloaded
        seen.sample_at = now

    def _job_event(self, name: str, job_id: int) -> list[dict[str, Any]]:
        job = self._job_object(job_id)
        if job is None:
            return []
        return [{"event": name, "job": job}]

    def _global_events(self, now: float) -> list[dict[str, Any]]:
        state = self._global_state()
        comparable = {k: v for k, v in state.items() if k != "speed"}
        last = (
            {k: v for k, v in self._last_global.items() if k != "speed"}
            if self._last_global is not None
            else None
        )
        if last is not None and comparable != last:
            self._global_pending = True
        speed_moved = self._last_global is not None and state.get("speed") != self._last_global.get(
            "speed"
        )
        if self._last_global is None:
            self._last_global = state
            return []
        if (self._global_pending or speed_moved) and now - self._global_sent_at >= GLOBAL_INTERVAL:
            self._global_pending = False
            self._global_sent_at = now
            self._last_global = state
            return [{"event": "global.updated", "state": state}]
        return []
