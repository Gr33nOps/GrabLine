"""Named queues: CRUD, sequential/parallel modes, queue priorities, pause,
per-queue schedules, category auto-assign, queue dependencies, and job-level
'download B only after A finishes' - all against the real scheduler with the
failure-simulating media server.
"""

from __future__ import annotations

import time
from pathlib import Path

from app.core.manager import DownloadManager
from app.core.models import JobStatus, Queue
from app.db.database import Database
from app.tests.conftest import wait_for
from app.tests.media_server import MediaServer, payload


def _status(db: Database, job_id: int) -> JobStatus:
    job = db.get_job(job_id)
    assert job is not None
    return job.status


def _edit(db: Database, queue: Queue, **changes: object) -> Queue:
    from dataclasses import replace

    updated = replace(queue, **changes)  # type: ignore[arg-type]
    db.update_queue(updated)
    return updated


# ------------------------------------------------------------------- CRUD


def test_queue_crud_and_unlimited_count(db: Database):
    queues = [db.create_queue(f"Queue {i}") for i in range(25)]  # no cap
    assert len(db.list_queues()) == 25
    assert [q.position for q in db.list_queues()] == list(range(1, 26))  # ordered

    first = queues[0]
    _edit(db, first, name="Movies", max_concurrent=1, category="Video")
    fresh = db.get_queue(first.id)
    assert fresh is not None
    assert fresh.name == "Movies" and fresh.max_concurrent == 1 and fresh.category == "Video"

    db.delete_queue(first.id)
    assert db.get_queue(first.id) is None
    assert len(db.list_queues()) == 24


def test_deleting_a_queue_releases_its_jobs_and_dependents(db: Database, dest: Path):
    blocker = db.create_queue("A")
    dependent = db.create_queue("B")
    _edit(db, dependent, depends_on=blocker.id)
    job = db.create_job("http://x.test/f.bin", str(dest), "f.bin", queue_id=blocker.id)

    db.delete_queue(blocker.id)

    fresh_job = db.get_job(job.id)
    assert fresh_job is not None and fresh_job.queue_id is None  # back to default
    fresh_dep = db.get_queue(dependent.id)
    assert fresh_dep is not None and fresh_dep.depends_on is None  # unblocked


# -------------------------------------------------------------- scheduling


def test_sequential_queue_runs_one_at_a_time(server: MediaServer, db: Database, dest: Path):
    """max_concurrent=1 (sequential mode) - the queue's second job must not
    start while its first is still downloading, even with free global slots."""
    urls = [
        server.add(f"/s{i}.bin", payload(1_000_000, i), chunk_size=32 * 1024, delay_per_chunk=0.05)
        for i in range(2)
    ]
    queue = db.create_queue("Sequential")
    _edit(db, queue, max_concurrent=1)
    # One connection each so the first job verifiably takes >1.5s.
    jobs = [
        db.create_job(url, str(dest), f"s{i}.bin", queue_id=queue.id, options={"connections": 1})
        for i, url in enumerate(urls)
    ]
    manager = DownloadManager(db, max_concurrent=4)
    try:
        wait_for(lambda: _status(db, jobs[0].id) is JobStatus.DOWNLOADING, timeout=30)
        time.sleep(0.8)  # give the scheduler every chance to (wrongly) start #2
        assert _status(db, jobs[1].id) is JobStatus.QUEUED
        wait_for(lambda: all(_status(db, j.id) is JobStatus.COMPLETED for j in jobs), timeout=60)
    finally:
        manager.shutdown()


def test_queue_dependency_download_b_only_after_a(server: MediaServer, db: Database, dest: Path):
    """The headline example: queue B's job stays QUEUED until queue A has
    nothing left, then runs."""
    url_a = server.add("/a.bin", payload(1_000_000, 1), chunk_size=32 * 1024, delay_per_chunk=0.05)
    url_b = server.add("/b.bin", payload(100_000, 2))
    queue_a = db.create_queue("A")
    queue_b = db.create_queue("B")
    _edit(db, queue_b, depends_on=queue_a.id)
    job_a = db.create_job(
        url_a, str(dest), "a.bin", queue_id=queue_a.id, options={"connections": 1}
    )
    job_b = db.create_job(url_b, str(dest), "b.bin", queue_id=queue_b.id)
    manager = DownloadManager(db, max_concurrent=4)
    try:
        wait_for(lambda: _status(db, job_a.id) is JobStatus.DOWNLOADING, timeout=30)
        time.sleep(0.8)
        assert _status(db, job_b.id) is JobStatus.QUEUED  # blocked on queue A
        wait_for(lambda: _status(db, job_a.id) is JobStatus.COMPLETED, timeout=60)
        wait_for(lambda: _status(db, job_b.id) is JobStatus.COMPLETED, timeout=60)
    finally:
        manager.shutdown()


def test_job_level_after_dependency(server: MediaServer, db: Database, dest: Path):
    url_a = server.add("/ja.bin", payload(1_000_000, 3), chunk_size=32 * 1024, delay_per_chunk=0.05)
    url_b = server.add("/jb.bin", payload(100_000, 4))
    job_a = db.create_job(url_a, str(dest), "ja.bin", options={"connections": 1})
    job_b = db.create_job(url_b, str(dest), "jb.bin", options={"after_job": job_a.id})
    manager = DownloadManager(db, max_concurrent=4)
    try:
        wait_for(lambda: _status(db, job_a.id) is JobStatus.DOWNLOADING, timeout=30)
        time.sleep(0.8)
        assert _status(db, job_b.id) is JobStatus.QUEUED
        wait_for(lambda: _status(db, job_b.id) is JobStatus.COMPLETED, timeout=60)
        assert _status(db, job_a.id) is JobStatus.COMPLETED
    finally:
        manager.shutdown()


def test_paused_queue_holds_jobs_until_unpaused(server: MediaServer, db: Database, dest: Path):
    url = server.add("/p.bin", payload(100_000, 5))
    queue = db.create_queue("Held")
    held = _edit(db, queue, paused=True)
    job = db.create_job(url, str(dest), "p.bin", queue_id=queue.id)
    manager = DownloadManager(db, max_concurrent=2)
    try:
        time.sleep(0.8)
        assert _status(db, job.id) is JobStatus.QUEUED

        manager.update_queue(
            Queue(id=held.id, name=held.name, position=held.position, paused=False)
        )
        wait_for(lambda: _status(db, job.id) is JobStatus.COMPLETED, timeout=60)
    finally:
        manager.shutdown()


def test_scheduled_queue_outside_window_does_not_run(server: MediaServer, db: Database, dest: Path):
    url = server.add("/w.bin", payload(50_000, 6))
    queue = db.create_queue("Nightly")
    # A window guaranteed not to include "now".
    from datetime import datetime, timedelta

    start = (datetime.now() + timedelta(hours=2)).strftime("%H:%M")
    stop = (datetime.now() + timedelta(hours=3)).strftime("%H:%M")
    _edit(db, queue, schedule_enabled=True, start_time=start, stop_time=stop)
    job = db.create_job(url, str(dest), "w.bin", queue_id=queue.id)
    manager = DownloadManager(db, max_concurrent=2)
    try:
        time.sleep(0.8)
        assert _status(db, job.id) is JobStatus.QUEUED  # outside its window
    finally:
        manager.shutdown()


def test_queue_priority_orders_starts(server: MediaServer, db: Database, dest: Path):
    """With one global slot, the job in the earlier-positioned queue starts
    first even though it was added second."""
    url_late = server.add("/late.bin", payload(50_000, 7))
    url_first = server.add("/first.bin", payload(50_000, 8))
    manager = DownloadManager(db, max_concurrent=0)  # hold the scheduler
    try:
        back = manager.create_queue("Back")
        front = manager.create_queue("Front")
        _edit(db, back, position=5)
        _edit(db, front, position=1)
        job_late = manager.add_url(url_late, dest)
        manager.set_job_queue(job_late.id, back.id)
        job_first = manager.add_url(url_first, dest)
        manager.set_job_queue(job_first.id, front.id)

        picked = manager._next_queued()
        assert picked is not None and picked.id == job_first.id
    finally:
        manager.shutdown()


def test_category_queue_auto_assigns_new_downloads(db: Database, dest: Path):
    manager = DownloadManager(db, max_concurrent=0)
    try:
        movies = manager.create_queue("Movies")
        _edit(db, movies, category="Video")
        video = manager.add_url("http://x.test/film.mkv", dest_dir=dest)
        document = manager.add_url("http://x.test/paper.pdf", dest_dir=dest)
        assert video.queue_id == movies.id
        assert document.queue_id is None  # no queue claims Documents
    finally:
        manager.shutdown()
