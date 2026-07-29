"""The live dashboard: stats persistence and rollups, the speed tracker's
current/average/peak/ETA, the system sampler, and an end-to-end check that a
completed download is recorded with its category and host.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

from app.core.manager import DownloadManager
from app.core.models import JobStatus
from app.core.settings import Settings
from app.core.stats import SpeedTracker, SystemSampler
from app.db.database import Database
from app.tests.conftest import sha256_file, wait_for
from app.tests.media_server import MediaServer, payload, sha256

MB = 1024 * 1024


# ------------------------------------------------------------ persistence


def test_record_and_rollups(db: Database):
    today = datetime.now().strftime("%Y-%m-%d")
    db.record_download("Video", "cdn.example.com", 100)
    db.record_download("Video", "cdn.example.com", 50)  # same bucket accrues
    db.record_download("Music", "media.example.com", 30)

    assert db.bytes_since(today) == 180
    lifetime, files = db.lifetime_bytes()
    assert lifetime == 180 and files == 3

    hosts = dict((h, b) for h, b, _ in db.bytes_by_host())
    assert hosts["cdn.example.com"] == 150
    assert hosts["media.example.com"] == 30

    cats = dict((c, b) for c, b, _ in db.bytes_by_category())
    assert cats["Video"] == 150 and cats["Music"] == 30


def test_clear_stats_wipes_rollups_but_not_jobs(db: Database, tmp_path: Path):
    db.record_download("Video", "cdn.example.com", 100)
    job = db.create_job("https://x.test/a.bin", str(tmp_path), "a.bin")
    db.clear_stats()
    assert db.lifetime_bytes() == (0, 0)
    assert db.get_job(job.id) is not None  # the download list is untouched


def test_bytes_since_respects_the_cutoff(db: Database):
    with db._lock, db._conn:  # seed an old row directly (a past day)
        old = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
        db._conn.execute(
            "INSERT INTO download_stats (day, category, host, bytes, files) VALUES (?,?,?,?,1)",
            (old, "Video", "old.example", 500),
        )
    db.record_download("Video", "new.example", 200)

    month_start = datetime.now().date().replace(day=1).strftime("%Y-%m-%d")
    assert db.bytes_since(month_start) == 200  # the 40-day-old row is excluded
    assert db.lifetime_bytes()[0] == 700  # but lifetime counts everything


def test_zero_and_negative_bytes_are_ignored(db: Database):
    db.record_download("Video", "h", 0)
    db.record_download("Video", "h", -5)
    assert db.lifetime_bytes() == (0, 0)


# ------------------------------------------------------------ speed tracker


def test_speed_tracker_current_peak_eta():
    tracker = SpeedTracker(smoothing=1.0)  # no smoothing -> exact instants
    # Prime the clock.
    tracker.update(0, None)
    time.sleep(0.05)
    reading = tracker.update(100_000, remaining=100_000)
    assert reading.current > 0
    assert reading.peak >= reading.current
    assert reading.eta_seconds is not None and reading.eta_seconds > 0

    # A stall drops current but never lowers the recorded peak.
    peak = reading.peak
    time.sleep(0.05)
    stalled = tracker.update(100_000, remaining=100_000)
    assert stalled.current < peak
    assert stalled.peak == peak


def test_speed_tracker_no_eta_without_remaining():
    tracker = SpeedTracker()
    tracker.update(0, None)
    time.sleep(0.02)
    assert tracker.update(1000, remaining=None).eta_seconds is None


def test_system_sampler_returns_numbers():
    sampler = SystemSampler()
    reading = sampler.sample()
    assert reading.cpu_percent >= 0
    assert reading.disk_bytes_per_sec >= 0
    assert reading.net_recv_per_sec >= 0
    assert reading.net_sent_per_sec >= 0


# --------------------------------------------------------------- end to end


def _status(db: Database, job_id: int) -> JobStatus:
    job = db.get_job(job_id)
    assert job is not None
    return job.status


def test_completed_download_is_recorded_with_category_and_host(
    server: MediaServer, db: Database, tmp_path: Path
):
    data = payload(200_000, 7)
    url = server.add("/clip.mp4", data, content_type="video/mp4")
    settings = Settings(db)
    settings.download_dir = tmp_path
    manager = DownloadManager(db, settings=settings, max_concurrent=1)
    try:
        job = manager.add_url(url)
        wait_for(lambda: _status(db, job.id) is JobStatus.COMPLETED, timeout=60)
    finally:
        manager.shutdown()

    assert sha256_file(tmp_path / "Video" / "clip.mp4") == sha256(data)
    totals = manager.stat_totals()
    assert totals["today"] == len(data)
    assert totals["files"] == 1
    hosts = dict((h, b) for h, b, _ in manager.stats_by_host())
    assert hosts["127.0.0.1"] == len(data)
    cats = dict((c, b) for c, b, _ in manager.stats_by_category())
    assert cats["Video"] == len(data)
