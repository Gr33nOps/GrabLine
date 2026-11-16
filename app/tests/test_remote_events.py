"""Live phone events: diffing, throttles and speed (PROTOCOL.md section 15)."""

from __future__ import annotations

from typing import Any

from app.core.manager import JobView
from app.core.models import JobStatus
from app.remote.events import EventTracker


def _view(job_id: int, status: JobStatus, downloaded: int = 0, total: int | None = 1000) -> JobView:
    return JobView(
        id=job_id,
        url=f"https://example.com/{job_id}",
        filename=f"f{job_id}.bin",
        dest_dir="",
        status=status,
        total_size=total,
        downloaded=downloaded,
        error=None,
    )


def _tracker(state: dict[str, Any] | None = None) -> EventTracker:
    box = {"state": state or {"paused": False, "speedLimit": None, "speed": 0, "active": 0}}
    return EventTracker(lambda job_id: {"id": job_id}, lambda: dict(box["state"]))


def _names(events: list[dict[str, Any]]) -> list[str]:
    return [e["event"] for e in events]


def test_first_snapshot_primes_without_events() -> None:
    tracker = _tracker()
    assert tracker.update([_view(1, JobStatus.QUEUED)], 0.0) == []


def test_added_removed_finished_failed() -> None:
    tracker = _tracker()
    tracker.update([_view(1, JobStatus.DOWNLOADING)], 0.0)
    events = tracker.update([_view(1, JobStatus.DOWNLOADING), _view(2, JobStatus.QUEUED)], 1.0)
    assert "job.added" in _names(events)
    events = tracker.update([_view(1, JobStatus.COMPLETED), _view(2, JobStatus.FAILED)], 2.0)
    assert {"job.finished", "job.failed"} <= set(_names(events))
    events = tracker.update([_view(1, JobStatus.COMPLETED)], 3.0)
    assert {"event": "job.removed", "jobId": 2} in events


def test_status_change_sends_update_within_a_second() -> None:
    tracker = _tracker()
    tracker.update([_view(1, JobStatus.DOWNLOADING)], 0.0)
    events = tracker.update([_view(1, JobStatus.PAUSED)], 0.5)
    assert _names(events).count("job.updated") == 1  # QA-16: well within 1 s


def test_progress_at_most_once_per_second() -> None:
    tracker = _tracker()
    tracker.update([_view(1, JobStatus.DOWNLOADING, 0)], 0.0)
    sent = 0
    for step in range(1, 11):  # ten snapshots over 5 s
        events = tracker.update([_view(1, JobStatus.DOWNLOADING, step * 100)], step * 0.5)
        sent += _names(events).count("job.progress")
    assert 4 <= sent <= 5


def test_speed_and_eta() -> None:
    tracker = _tracker()
    tracker.update([_view(1, JobStatus.DOWNLOADING, 0, total=10_000)], 0.0)
    tracker.update([_view(1, JobStatus.DOWNLOADING, 1000, total=10_000)], 1.0)
    live = tracker.live(1)
    assert live.speed == 1000
    assert live.eta == 9
    tracker.update([_view(1, JobStatus.PAUSED, 1000, total=10_000)], 2.0)
    assert tracker.live(1).speed == 0


def test_global_update_throttled_and_on_change() -> None:
    state = {"paused": False, "speedLimit": None, "speed": 0, "active": 0}
    box = {"s": state}
    tracker = EventTracker(lambda job_id: {"id": job_id}, lambda: dict(box["s"]))
    tracker.update([], 0.0)
    box["s"] = {**state, "paused": True}
    assert "global.updated" in _names(tracker.update([], 1.5))
    box["s"] = {**state, "paused": False}
    assert "global.updated" not in _names(tracker.update([], 1.8))  # throttled
    assert "global.updated" in _names(tracker.update([], 2.6))
