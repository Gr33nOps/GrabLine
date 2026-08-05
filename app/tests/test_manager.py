from __future__ import annotations

from pathlib import Path

import pytest

from app.core.manager import DownloadManager
from app.core.models import JobKind, JobStatus
from app.core.settings import Settings
from app.db.database import Database
from app.engines.smart import MediaInfo, QualityOption
from app.tests.conftest import sha256_file, wait_for
from app.tests.media_server import MediaServer, payload, sha256

MB = 1024 * 1024


def _status(db: Database, job_id: int) -> JobStatus:
    job = db.get_job(job_id)
    assert job is not None
    return job.status


def test_add_hls_stores_browser_headers_on_the_job(db: Database, dest: Path):
    """add_hls must carry the browser handoff's headers into job.options, the
    same convention add_url already uses - HlsDownload reads them from there
    to forward Referer/Cookie to the CDN."""
    manager = DownloadManager(db, max_concurrent=0)
    try:
        job = manager.add_hls(
            "https://cdn.example/master.m3u8",
            dest_dir=str(dest),
            headers={"Referer": "https://site.example/watch", "Cookie": "sess=abc"},
        )
        fresh = db.get_job(job.id)
        assert fresh is not None
        assert fresh.options.get("http_headers") == {
            "Referer": "https://site.example/watch",
            "Cookie": "sess=abc",
        }

        # No headers (a plain paste) -> no key at all, not an empty dict.
        plain = manager.add_hls("https://cdn.example/other.m3u8", dest_dir=str(dest))
        fresh_plain = db.get_job(plain.id)
        assert fresh_plain is not None
        assert "http_headers" not in fresh_plain.options
    finally:
        manager.shutdown()


def test_queue_respects_concurrency_and_completes_all(
    server: MediaServer, db: Database, dest: Path
):
    datas = [payload(400_000, seed) for seed in range(3)]
    urls = [server.add(f"/f{i}.bin", datas[i]) for i in range(3)]
    manager = DownloadManager(db, max_concurrent=2)
    try:
        jobs = [manager.add_url(url, dest) for url in urls]
        wait_for(
            lambda: all(_status(db, job.id) is JobStatus.COMPLETED for job in jobs),
            timeout=60,
        )
    finally:
        manager.shutdown()
    for i in range(3):
        assert sha256_file(dest / f"f{i}.bin") == sha256(datas[i])


def test_add_url_threads_headers_into_a_gated_download(
    server: MediaServer, db: Database, dest: Path
):
    data = payload(400_000, 41)
    url = server.add("/gated.bin", data, required_headers={"Cookie": "session=abc"})
    manager = DownloadManager(db, max_concurrent=1)
    try:
        job = manager.add_url(url, dest, headers={"Cookie": "session=abc"})
        assert job.options["http_headers"] == {"Cookie": "session=abc"}
        wait_for(lambda: _status(db, job.id) is JobStatus.COMPLETED, timeout=60)
    finally:
        manager.shutdown()
    assert sha256_file(dest / "gated.bin") == sha256(data)
    assert server.received_headers("/gated.bin")["cookie"] == "session=abc"


def test_force_remove_drops_a_running_download(server: MediaServer, db: Database, dest: Path):
    # Remove must mean remove, whatever the state. Cancelling is asynchronous,
    # so a mid-flight force remove has to survive the round trip: the worker
    # stops, then the row and its partial file go.
    data = payload(4 * MB, 17)
    url = server.add("/live.bin", data, chunk_size=32 * 1024, delay_per_chunk=0.03)
    manager = DownloadManager(db, max_concurrent=1)
    try:
        job = manager.add_url(url, dest)
        wait_for(lambda: db.job_downloaded(job.id) > 256 * 1024, timeout=60)
        assert _status(db, job.id) is JobStatus.DOWNLOADING
        part_path = job.part_path

        manager.remove(job.id, force=True)

        wait_for(lambda: db.get_job(job.id) is None, timeout=30)
        assert not part_path.exists()  # the partial file goes with the job
    finally:
        manager.shutdown()


def test_plain_remove_leaves_a_running_download_in_the_list(
    server: MediaServer, db: Database, dest: Path
):
    # Without force the old contract holds: a running job is only cancelled.
    data = payload(4 * MB, 18)
    url = server.add("/keep.bin", data, chunk_size=32 * 1024, delay_per_chunk=0.03)
    manager = DownloadManager(db, max_concurrent=1)
    try:
        job = manager.add_url(url, dest)
        wait_for(lambda: db.job_downloaded(job.id) > 256 * 1024, timeout=60)
        manager.remove(job.id)
        wait_for(lambda: _status(db, job.id) is JobStatus.CANCELLED, timeout=30)
        assert db.get_job(job.id) is not None  # row survives
    finally:
        manager.shutdown()


def test_manager_pause_and_resume(server: MediaServer, db: Database, dest: Path):
    data = payload(4 * MB, 9)
    url = server.add("/s.bin", data, chunk_size=32 * 1024, delay_per_chunk=0.03)
    manager = DownloadManager(db, max_concurrent=1)
    try:
        job = manager.add_url(url, dest)
        wait_for(lambda: db.job_downloaded(job.id) > 512 * 1024, timeout=60)
        manager.pause(job.id)
        wait_for(lambda: _status(db, job.id) is JobStatus.PAUSED, timeout=30)
        manager.resume(job.id)
        wait_for(lambda: _status(db, job.id) is JobStatus.COMPLETED, timeout=60)
    finally:
        manager.shutdown()
    assert sha256_file(dest / "s.bin") == sha256(data)


def test_manager_dispatches_smart_jobs(
    server: MediaServer, db: Database, dest: Path, monkeypatch: pytest.MonkeyPatch
):
    # Force ffmpeg_path=None: no postprocessing, so served bytes come through
    # untouched and the checksum must match.
    monkeypatch.setattr("app.core.manager.find_ffmpeg", lambda settings: None)
    data = payload(600_000, 21)
    url = server.add("/tube.mp4", data, content_type="video/mp4")
    media = MediaInfo(
        url=url,
        id="x",
        title="Manager Clip",
        uploader=None,
        duration=None,
        thumbnail_url=None,
        options=(QualityOption(label="Best", kind="video", format_spec="b"),),
    )
    manager = DownloadManager(db, max_concurrent=1)
    try:
        job = manager.add_smart(url, media, media.options[0], dest_dir=dest)
        assert job.kind is JobKind.SMART
        wait_for(lambda: _status(db, job.id) is JobStatus.COMPLETED, timeout=60)
        views = {view.id: view for view in manager.snapshot()}
        assert views[job.id].display_name == "Manager Clip"
    finally:
        manager.shutdown()
    assert sha256_file(dest / "Manager Clip.mp4") == sha256(data)


def test_add_smart_entry_for_playlist_items(db: Database, dest: Path):
    from app.engines.smart import generic_quality_options

    manager = DownloadManager(db, max_concurrent=0)
    try:
        option = generic_quality_options()[0]
        job = manager.add_smart_entry(
            "https://tube.example/watch?v=xyz", "Episode 12", option, dest_dir=dest
        )
        assert job.kind is JobKind.SMART
        assert job.title == "Episode 12"
        assert job.filename == "Episode 12.mp4"
        assert job.options["format_spec"] == option.format_spec
    finally:
        manager.shutdown()


def test_add_smart_threads_post_processing_extras(db: Database, dest: Path):
    from app.engines.smart import generic_quality_options

    manager = DownloadManager(db, max_concurrent=0)
    try:
        option = generic_quality_options()[0]
        job = manager.add_smart_entry(
            "https://tube.example/watch?v=xyz",
            "Talk",
            option,
            dest_dir=dest,
            extras={"sponsorblock": "remove", "chapters": True, "save_thumbnail": True},
        )
        assert job.options["sponsorblock"] == "remove"
        assert job.options["chapters"] is True
        assert job.options["save_thumbnail"] is True
    finally:
        manager.shutdown()


def test_add_smart_entry_stores_browser_headers_on_the_job(db: Database, dest: Path):
    """A gated video needs the browser handoff's Referer/Cookie forwarded to
    yt-dlp, the same convention add_hls/add_url use - stored under http_headers,
    and absent entirely for a plain paste (not an empty dict)."""
    from app.engines.smart import generic_quality_options

    manager = DownloadManager(db, max_concurrent=0)
    try:
        option = generic_quality_options()[0]
        job = manager.add_smart_entry(
            "https://tube.example/watch?v=xyz",
            "Gated Talk",
            option,
            dest_dir=dest,
            headers={"Referer": "https://site.example/watch", "Cookie": "sess=abc"},
        )
        assert job.options.get("http_headers") == {
            "Referer": "https://site.example/watch",
            "Cookie": "sess=abc",
        }

        plain = manager.add_smart_entry(
            "https://tube.example/watch?v=plain", "Plain", option, dest_dir=dest
        )
        assert "http_headers" not in plain.options
    finally:
        manager.shutdown()


def test_remove_deletes_row_but_keeps_completed_file(db: Database, dest: Path):
    manager = DownloadManager(db, max_concurrent=0)
    try:
        job = db.create_job("http://x.test/keep.bin", str(dest), "keep.bin")
        db.set_job_status(job.id, JobStatus.COMPLETED)
        artifact = dest / "keep.bin"
        artifact.write_bytes(b"data")
        manager.remove(job.id)
        assert db.get_job(job.id) is None
        assert artifact.exists()  # history removal never deletes finished files
    finally:
        manager.shutdown()


def test_reload_settings_applies_speed_cap(db: Database):
    from app.core.settings import Settings

    settings = Settings(db)
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        assert manager.limiter.rate == 0
        settings.speed_limit_kbps = 512
        manager.reload_settings()
        assert manager.limiter.rate == 512 * 1024
        # dynamic concurrency: no override given at construction
        dynamic = DownloadManager(db, settings=settings)
        try:
            settings.max_concurrent = 7
            assert dynamic.max_concurrent == 7
        finally:
            dynamic.shutdown()
    finally:
        manager.shutdown()


def test_add_url_sorts_into_categories(server: MediaServer, db: Database, tmp_path: Path):
    url = server.add("/report.pdf", payload(10_000, 23))
    settings = Settings(db)
    settings.download_dir = tmp_path / "base"
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        job = manager.add_url(url)
        assert job.dest_dir == str(tmp_path / "base" / "Documents")
        settings.categories_enabled = False
        job2 = manager.add_url(url)
        assert job2.dest_dir == str(tmp_path / "base")
    finally:
        manager.shutdown()


def test_snapshot_reports_progress(server: MediaServer, db: Database, dest: Path):
    data = payload(300_000, 5)
    url = server.add("/snap.bin", data)
    manager = DownloadManager(db, max_concurrent=1)
    try:
        job = manager.add_url(url, dest)
        wait_for(lambda: _status(db, job.id) is JobStatus.COMPLETED, timeout=60)
        views = manager.snapshot()
    finally:
        manager.shutdown()
    assert len(views) == 1
    view = views[0]
    assert view.id == job.id
    assert view.status is JobStatus.COMPLETED
    assert view.downloaded == len(data)
    assert view.total_size == len(data)


def test_connection_budget_is_shared_across_active_jobs(db: Database, dest: Path):
    # Simultaneous downloads split the connection budget equally, and the split
    # is *live*: every active download's share shrinks as a sibling starts and
    # grows back as one finishes - so no single download hogs the line.
    from app.core.downloader import SegmentedDownload

    def segmented(url: str, name: str, connections: int | None = None) -> SegmentedDownload:
        options = {"connections": connections} if connections else None
        job = db.create_job(url, str(dest), name, options=options)
        # Park it defensively; the scheduler is already stopped below, so the
        # budget math just needs the task objects.
        db.set_job_status(job.id, JobStatus.PAUSED)
        task = manager._create_task(job)
        assert isinstance(task, SegmentedDownload)
        return task

    manager = DownloadManager(db, max_concurrent=3)
    # Stop the scheduler before creating any jobs, so it can't start a phantom
    # download thread and mutate _active out from under the assertions.
    manager._running = False
    manager._kick()
    manager._scheduler.join(timeout=5)
    try:
        manager._connections_override = 16
        alone = segmented("https://x.test/a.bin", "a.bin")
        # An unpinned download plans for the full budget; the live target is what
        # limits its running connections.
        assert alone.connections == 16 and alone.shares_budget is True
        manager._active[1] = alone
        assert alone._target_connections() == 16  # nothing else running: all 16

        second = segmented("https://x.test/b.bin", "b.bin")
        manager._active[2] = second
        # Both drop to half - the first is rebalanced live, not frozen at 16.
        assert second._target_connections() == 8
        assert alone._target_connections() == 8

        third = segmented("https://x.test/c.bin", "c.bin")
        manager._active[3] = third
        # 16 across 3 is 6+5+5: the two connections an even split would round
        # away are handed out rather than left idle.
        shares = [t._target_connections() for t in (alone, second, third)]
        assert sorted(shares) == [5, 5, 6]
        assert sum(shares) == 16

        # A pinned download keeps its exact count and stays out of the split.
        pinned = segmented("https://x.test/d.bin", "d.bin", connections=20)
        assert pinned.connections == 20 and pinned.shares_budget is False
        assert pinned._target_connections() == 20
        manager._active[4] = pinned
        # the pin didn't change the sharers' split
        assert [t._target_connections() for t in (alone, second, third)] == shares
    finally:
        manager._active.clear()
        manager.shutdown()


def test_429_and_408_are_transient_but_404_is_permanent():
    from app.core.manager import _is_transient_error

    assert _is_transient_error("server responded with HTTP 429")
    assert _is_transient_error("HTTP Error 408: Request Timeout")
    assert _is_transient_error("could not reach server: timeout")
    assert not _is_transient_error("server responded with HTTP 404")
    assert not _is_transient_error("server responded with HTTP 403")


def test_retry_forever_when_max_is_zero(db: Database, dest: Path):
    settings = Settings(db)
    settings.auto_retry = True
    settings.auto_retry_max = 0  # 0 = forever
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        job = db.create_job("https://x.test/f.bin", str(dest), "f.bin")
        db.set_job_status(job.id, JobStatus.FAILED, error="could not reach server: timeout")
        db.set_retry_count(job.id, 50)  # far past any finite cap
        assert manager._schedule_retry(job.id) is True
        fresh = db.get_job(job.id)
        assert fresh is not None and fresh.retry_count == 51
    finally:
        manager.shutdown()


def test_mirror_failover_downloads_from_the_next_url(server: MediaServer, db: Database, dest: Path):
    # First URL 404s (permanent - no point retrying it); the job must switch
    # to its mirror and complete from there.
    data = payload(300_000, 61)
    dead = server.url("/gone.bin")  # never added -> 404
    mirror = server.add("/mirror.bin", data)
    manager = DownloadManager(db, max_concurrent=1)
    try:
        job = manager.add_url(dead, dest, filename="m.bin", mirrors=[mirror])
        assert job.options["mirrors"] == [mirror]
        wait_for(lambda: _status(db, job.id) is JobStatus.COMPLETED, timeout=60)
        fresh = db.get_job(job.id)
        assert fresh is not None
        assert fresh.url == mirror  # switched
        assert fresh.options["mirrors"] == []  # consumed
    finally:
        manager.shutdown()
    assert sha256_file(dest / "m.bin") == sha256(data)


def test_mirror_failover_from_a_url_that_probed_fine(server: MediaServer, db: Database, dest: Path):
    """Switching mirrors must forget everything learned about the old URL.

    A URL that probed successfully and only failed later left its final_url,
    etag, size and resumable flag behind. The downloader treats a stored
    final_url as a usable cached probe, so the mirror inherited the dead URL's
    address and downloaded from it again - failover that could never work.
    """
    data = payload(300_000, 62)
    stale = server.add("/stale.bin", payload(300_000, 63))
    mirror = server.add("/fresh.bin", data)
    manager = DownloadManager(db, max_concurrent=0)
    try:
        job = manager.add_url(stale, dest, filename="f.bin", mirrors=[mirror])
        # What a first run against the (then working) URL leaves behind.
        job.final_url = stale
        job.total_size = 300_000
        job.resumable = True
        job.etag = '"stale-etag"'
        db.update_job_probe(job)

        assert manager._try_mirror(job.id) is True
        switched = db.get_job(job.id)
        assert switched is not None
        assert switched.url == mirror
        assert switched.final_url is None
        assert switched.etag is None
        assert switched.total_size is None
        assert switched.resumable is False
    finally:
        manager.shutdown()


def test_pinned_connections_bypass_the_share_split(db: Database, dest: Path):
    from app.core.downloader import SegmentedDownload

    manager = DownloadManager(db, max_concurrent=3)
    try:
        manager._connections_override = 16
        # Something already running, so the share-split would normally apply.
        other = db.create_job("https://x.test/other.bin", str(dest), "other.bin")
        manager._active[1] = manager._create_task(other)
        job = db.create_job("https://x.test/p.bin", str(dest), "p.bin", options={"connections": 24})
        task = manager._create_task(job)
        assert isinstance(task, SegmentedDownload)
        assert task.connections == 24  # the explicit pin wins over the split

        manager.set_job_connections(job.id, 500)  # clamped
        fresh = db.get_job(job.id)
        assert fresh is not None and fresh.options["connections"] == 32
        manager.set_job_connections(job.id, 0)  # back to automatic
        fresh = db.get_job(job.id)
        assert fresh is not None and "connections" not in fresh.options
    finally:
        manager._active.clear()
        manager.shutdown()


def test_rename_rules_apply_to_new_downloads(db: Database, dest: Path):
    manager = DownloadManager(db, max_concurrent=0)
    try:
        manager.settings.rename_rules = [("badword", "nice")]
        job = manager.add_url("http://x.test/badword-file.bin", dest_dir=dest)
        assert job.filename == "nice-file.bin"
    finally:
        manager.shutdown()


def test_tags_and_notes_roundtrip_into_views(db: Database, dest: Path):
    manager = DownloadManager(db, max_concurrent=0)
    try:
        job = manager.add_url("http://x.test/file.bin", dest_dir=dest)
        manager.set_job_tags(job.id, "work, iso")
        manager.set_job_notes(job.id, "for the demo box")
        view = next(v for v in manager.snapshot() if v.id == job.id)
        assert view.tags == "work, iso"
        assert view.notes == "for the demo box"
        manager.set_job_tags(job.id, "  ")  # blank clears the tag
        view = next(v for v in manager.snapshot() if v.id == job.id)
        assert view.tags == ""
    finally:
        manager.shutdown()


def test_find_existing_matches_exact_url(db: Database, dest: Path):
    manager = DownloadManager(db, max_concurrent=0)
    try:
        job = manager.add_url("http://x.test/file.bin", dest_dir=dest)
        found = manager.find_existing("http://x.test/file.bin")
        assert found is not None and found.id == job.id
        assert manager.find_existing("http://x.test/other.bin") is None
    finally:
        manager.shutdown()


def test_move_job_file_to_favorite_folder(db: Database, dest: Path, tmp_path: Path):
    manager = DownloadManager(db, max_concurrent=0)
    try:
        job = manager.add_url("http://x.test/keep.bin", dest_dir=dest)
        (dest / "keep.bin").write_bytes(b"data")
        db.set_job_status(job.id, JobStatus.COMPLETED)
        favorite = tmp_path / "Movies"
        (favorite / "keep.bin").parent.mkdir(parents=True, exist_ok=True)
        (favorite / "keep.bin").write_bytes(b"already here")  # collision

        target = manager.move_job_file(job.id, favorite)

        assert target == favorite / "keep (1).bin"  # never overwrites
        assert target.read_bytes() == b"data"
        assert not (dest / "keep.bin").exists()
        fresh = db.get_job(job.id)
        assert fresh is not None
        assert fresh.dest_dir == str(favorite)
        assert fresh.filename == "keep (1).bin"
    finally:
        manager.shutdown()


def test_move_job_file_refuses_unfinished_downloads(db: Database, dest: Path, tmp_path: Path):
    from app.core.errors import DownloadError

    manager = DownloadManager(db, max_concurrent=0)
    try:
        job = manager.add_url("http://x.test/partial.bin", dest_dir=dest)
        with pytest.raises(DownloadError, match="finished"):
            manager.move_job_file(job.id, tmp_path / "Movies")
    finally:
        manager.shutdown()


def test_a_crashing_engine_marks_the_job_failed(db: Database, dest: Path):
    """A bug in an engine must never strand a job at 'Downloading' - the exact
    aftermath of the HLS finalizer raising on Windows: thread dead, row stuck,
    the user watching a 100% download that never completes."""
    # max_concurrent=0 keeps the scheduler idle: this test drives _run_job
    # itself, and a live scheduler would re-dispatch the job underneath it.
    manager = DownloadManager(db, max_concurrent=0)
    try:
        job = db.create_job("https://example.invalid/x.bin", str(dest), "x.bin")
        db.set_job_status(job.id, JobStatus.DOWNLOADING)

        class ExplodingTask:
            bytes_downloaded = 0  # DownloadTask protocol member

            def run(self):
                raise RuntimeError("engine bug")

            def pause(self):
                pass

            def cancel(self):
                pass

        manager._run_job(job, ExplodingTask())  # must not raise out
        assert _status(db, job.id) is JobStatus.FAILED
        fresh = db.get_job(job.id)
        assert fresh is not None and "internal error" in (fresh.error or "")
    finally:
        manager.shutdown()


def test_fair_speed_splits_budget_evenly_and_stays_stable(db: Database):
    """N downloads each get budget÷N while every one of them wants it all."""
    from app.core.settings import Settings

    settings = Settings(db)
    settings.fair_speed = True
    settings.speed_limit_kbps = 2000  # 2 MB/s budget
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        manager._running = False
        manager._kick()
        manager._scheduler.join(timeout=5)

        class _Task:
            bytes_downloaded = 0

            def run(self):
                return JobStatus.COMPLETED

            def pause(self):
                pass

            def cancel(self):
                pass

            def set_download_limit(self, rate: int) -> None:
                self.limit = rate

        a = _Task()
        b = _Task()
        manager._active[1] = a
        manager._active[2] = b
        caps = manager._compute_fair_caps([1, 2])
        assert caps == {1: 1000 * 1024, 2: 1000 * 1024}

        # Without a settled rate sample nothing is reclaimed: repeated passes
        # keep handing out the same equal caps rather than drifting.
        caps = manager._compute_fair_caps([1, 2])
        assert caps[1] == caps[2] == 1000 * 1024

        manager._active[3] = _Task()
        manager._active[4] = _Task()
        caps = manager._compute_fair_caps([1, 2, 3, 4])
        assert caps == {i: 500 * 1024 for i in (1, 2, 3, 4)}

        manager._apply_fair_speed()
        assert a.limit == 500 * 1024
        # Second apply with the same caps must not re-push (torrent/handle thrash).
        a.limit = -1
        manager._apply_fair_speed()
        assert a.limit == -1

        settings.fair_speed = False
        manager._apply_fair_speed()
        assert a.limit == 0
    finally:
        manager._active.clear()
        manager.shutdown()


def test_fair_speed_reclaims_share_a_slow_download_cannot_use(db: Database):
    """The line the slow one leaves on the table goes to the one that can use
    it - an equal split alone would cap the fast download at half a line that
    is mostly idle."""
    from app.core.settings import Settings

    settings = Settings(db)
    settings.fair_speed = True
    settings.speed_limit_kbps = 2000  # 2 MB/s budget
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        # Stop the scheduler first: its own sampling pass would clear the rate
        # and cap state these assertions set up.
        manager._running = False
        manager._kick()
        manager._scheduler.join(timeout=5)

        equal = 1000 * 1024
        # Both were capped at an equal share on the previous pass; job 1's own
        # server only ever managed 100 KB/s of it, job 2 is pinned at its cap.
        manager._fair_applied = {1: equal, 2: equal}
        manager._job_rates = {1: 100.0 * 1024, 2: float(equal)}

        caps = manager._compute_fair_caps([1, 2])

        assert caps[1] == int(100 * 1024 * 1.30)  # its own rate plus headroom
        assert caps[2] > equal  # the sibling gets the rest of the budget
        assert caps[1] + caps[2] <= 2000 * 1024  # and the budget still holds

        # A download held under its share must still be able to speed back up:
        # a rising sample is believed at once, not averaged away.
        manager._job_marks = {1: 0}
        manager._update_job_rates([(1, 800 * 1024)], elapsed=1.0)
        assert manager._job_rates[1] == 800 * 1024
        # A dip decays in gradually instead of surrendering the share at once.
        manager._job_marks = {1: 800 * 1024}
        manager._update_job_rates([(1, 800 * 1024)], elapsed=1.0)
        assert manager._job_rates[1] == pytest.approx(600 * 1024)
    finally:
        manager.shutdown()


def test_fair_speed_lone_job_keeps_global_limit(db: Database):
    from app.core.settings import Settings

    settings = Settings(db)
    settings.fair_speed = True
    settings.speed_limit_kbps = 512
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        assert manager._compute_fair_caps([7]) == {7: 512 * 1024}
    finally:
        manager.shutdown()


def test_fair_line_capacity_does_not_shrink_while_sharing(db: Database):
    """Under fair-speed, throttled samples must not collapse the budget."""
    from app.core.settings import Settings

    settings = Settings(db)
    settings.fair_speed = True
    settings.speed_limit_kbps = 0  # unlimited → capacity estimate
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        manager._line_capacity = 2_000_000
        manager._update_line_capacity(900_000, n_active=2)  # throttled aggregate
        assert manager._line_capacity == 2_000_000  # sticky
        manager._update_line_capacity(2_200_000, n_active=2)
        assert manager._line_capacity == 2_200_000  # ratchet up only
        assert manager._fair_budget_bytes() == 2_200_000
    finally:
        manager.shutdown()


def test_fair_probe_defers_caps_when_multi_share_starts(db: Database):
    """Two downloads starting together stay unlimited briefly so capacity is
    learned at full speed - not from a handshake-low equal split."""
    from app.core.settings import Settings

    settings = Settings(db)
    settings.fair_speed = True
    settings.speed_limit_kbps = 0
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        manager._running = False
        manager._kick()
        manager._scheduler.join(timeout=5)
        manager._line_capacity = 8_000_000  # would otherwise split immediately
        manager._fair_sharing = False
        manager._note_fair_share_state(2)
        assert manager._in_fair_probe()
        assert manager._compute_fair_caps([1, 2]) == {1: 0, 2: 0}

        # After the probe window, equal caps of the measured budget apply.
        manager._fair_probe_until = 0.0
        assert manager._compute_fair_caps([1, 2]) == {1: 4_000_000, 2: 4_000_000}
    finally:
        manager.shutdown()


def test_fair_saturation_boosts_underestimated_capacity(db: Database):
    """When aggregate rate sits on our own fair budget, raise the estimate so
    equal shares can climb toward the real line instead of locking low."""
    from app.core.settings import Settings

    settings = Settings(db)
    settings.fair_speed = True
    settings.speed_limit_kbps = 0
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        manager._line_capacity = 1_000_000
        manager._fair_probe_until = 0.0
        manager._update_line_capacity(950_000, n_active=2)  # saturating our cap
        assert manager._line_capacity == int(1_000_000 * 1.20)
        # Well below the budget: no boost (unused headroom, not saturation).
        before = manager._line_capacity
        manager._update_line_capacity(500_000, n_active=2)
        assert manager._line_capacity == before
    finally:
        manager.shutdown()
