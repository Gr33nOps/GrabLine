"""Queue priorities, per-download speed cap, speed schedule, and auto-retry."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import pytest

from app.core.manager import DownloadManager, _is_transient_error
from app.core.models import Job, JobStatus
from app.core.settings import Settings
from app.db.database import Database


def _job(db: Database, job_id: int) -> Job:
    job = db.get_job(job_id)
    assert job is not None
    return job


@pytest.fixture()
def manager(db: Database) -> Iterator[DownloadManager]:
    # max_concurrent=0 keeps the scheduler from starting anything, so these
    # tests exercise the queue bookkeeping without network activity.
    mgr = DownloadManager(db, max_concurrent=0)
    yield mgr
    mgr.shutdown()


# ------------------------------------------------------------- priorities


def _pending_ids(manager: DownloadManager) -> list[int]:
    return [j.id for j in manager._pending_order()]


def test_new_jobs_keep_insertion_order(manager: DownloadManager, db: Database):
    ids = [manager.add_url(f"http://x.test/f{i}.bin", "/tmp").id for i in range(4)]
    assert _pending_ids(manager) == ids


def test_move_up_and_down(manager: DownloadManager):
    ids = [manager.add_url(f"http://x.test/f{i}.bin", "/tmp").id for i in range(4)]
    manager.move_up(ids[2])
    assert _pending_ids(manager) == [ids[0], ids[2], ids[1], ids[3]]
    manager.move_down(ids[0])
    assert _pending_ids(manager) == [ids[2], ids[0], ids[1], ids[3]]


def test_move_to_top_and_bottom(manager: DownloadManager):
    ids = [manager.add_url(f"http://x.test/f{i}.bin", "/tmp").id for i in range(4)]
    manager.move_to_top(ids[3])
    assert _pending_ids(manager)[0] == ids[3]
    manager.move_to_bottom(ids[3])
    assert _pending_ids(manager)[-1] == ids[3]


def test_move_edges_are_noops(manager: DownloadManager):
    ids = [manager.add_url(f"http://x.test/f{i}.bin", "/tmp").id for i in range(3)]
    before = _pending_ids(manager)
    manager.move_up(ids[0])  # already first
    manager.move_down(ids[-1])  # already last
    assert _pending_ids(manager) == before


# ---------------------------------------------------- per-download speed cap


def test_set_job_speed_persists_and_makes_limiter(manager: DownloadManager, db: Database):
    job = manager.add_url("http://x.test/f.bin", "/tmp")
    manager.set_job_speed(job.id, 512)
    fresh = db.get_job(job.id)
    assert fresh is not None and fresh.options["speed_limit_kbps"] == 512
    limiter = manager._job_limiter_for(fresh)
    assert limiter is not None and limiter.rate == 512 * 1024
    # Clearing removes both the persisted value and the live limiter.
    manager.set_job_speed(job.id, 0)
    cleared = db.get_job(job.id)
    assert cleared is not None and "speed_limit_kbps" not in cleared.options
    assert job.id not in manager._job_limiters


# --------------------------------------------------------- speed schedule


def test_full_speed_window_same_day(manager: DownloadManager):
    manager.settings.speed_full_from = "01:00"
    manager.settings.speed_full_to = "06:00"
    assert manager._in_full_speed_window(datetime(2026, 1, 1, 3, 0))
    assert not manager._in_full_speed_window(datetime(2026, 1, 1, 7, 0))


def test_full_speed_window_wraps_midnight(manager: DownloadManager):
    manager.settings.speed_full_from = "23:00"
    manager.settings.speed_full_to = "07:00"
    assert manager._in_full_speed_window(datetime(2026, 1, 1, 2, 0))
    assert manager._in_full_speed_window(datetime(2026, 1, 1, 23, 30))
    assert not manager._in_full_speed_window(datetime(2026, 1, 1, 12, 0))


def test_effective_rate_lifts_limit_in_window(db: Database):
    settings = Settings(db)
    settings.speed_limit_kbps = 500
    settings.speed_schedule_enabled = True
    settings.speed_full_from = "00:00"
    settings.speed_full_to = "23:59"
    mgr = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        assert mgr._effective_global_rate() == 0  # inside window -> unlimited
        settings.speed_schedule_enabled = False
        assert mgr._effective_global_rate() == 500 * 1024
    finally:
        mgr.shutdown()


# ------------------------------------------------------------- auto-retry


def test_transient_error_classification():
    assert _is_transient_error("connection reset by peer")
    assert _is_transient_error("server responded with HTTP 503")
    assert _is_transient_error(None) is False
    assert not _is_transient_error("This content is DRM-protected.")
    assert not _is_transient_error("no downloadable media was found")
    assert not _is_transient_error("not enough free disk space")


def test_auto_retry_schedules_then_promotes(manager: DownloadManager, db: Database):
    manager.settings.auto_retry = True
    manager.settings.auto_retry_max = 3
    job = manager.add_url("http://x.test/f.bin", "/tmp")
    db.set_job_status(job.id, JobStatus.FAILED, error="connection reset")
    assert manager._schedule_retry(job.id) is True
    assert _job(db, job.id).retry_count == 1
    assert job.id in manager._retry_at
    # Force the deadline into the past; the scheduler should re-queue it.
    manager._retry_at[job.id] = 0.0
    manager._promote_due_retries()
    assert _job(db, job.id).status is JobStatus.QUEUED
    assert job.id not in manager._retry_at


def test_auto_retry_respects_max_and_permanent_errors(manager: DownloadManager, db: Database):
    manager.settings.auto_retry = True
    manager.settings.auto_retry_max = 2
    job = manager.add_url("http://x.test/f.bin", "/tmp")

    db.set_job_status(job.id, JobStatus.FAILED, error="This content is DRM-protected.")
    assert manager._schedule_retry(job.id) is False  # permanent, never retried

    db.set_job_status(job.id, JobStatus.FAILED, error="timeout")
    db.set_retry_count(job.id, 2)  # already at the cap
    assert manager._schedule_retry(job.id) is False


def test_auto_retry_off_does_nothing(manager: DownloadManager, db: Database):
    manager.settings.auto_retry = False
    job = manager.add_url("http://x.test/f.bin", "/tmp")
    db.set_job_status(job.id, JobStatus.FAILED, error="timeout")
    assert manager._schedule_retry(job.id) is False


def test_manual_resume_clears_retry_state(manager: DownloadManager, db: Database):
    job = manager.add_url("http://x.test/f.bin", "/tmp")
    db.set_job_status(job.id, JobStatus.FAILED, error="timeout")
    manager._schedule_retry(job.id)
    manager.resume(job.id)
    assert job.id not in manager._retry_at
    fresh = db.get_job(job.id)
    assert fresh is not None and fresh.retry_count == 0 and fresh.status is JobStatus.QUEUED
