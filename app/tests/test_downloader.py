from __future__ import annotations

import itertools
import threading
import time
from pathlib import Path

import pytest

from app.core.downloader import SegmentedDownload, plan_segments
from app.core.models import JobStatus
from app.db.database import Database
from app.tests.conftest import sha256_file, wait_for
from app.tests.media_server import MediaServer, payload, sha256

MB = 1024 * 1024


def test_plan_segments_covers_range_exactly():
    spans = plan_segments(10 * MB, 8)
    assert len(spans) == 8
    assert spans[0][0] == 0
    assert spans[-1][1] == 10 * MB - 1
    for (_, prev_end), (next_start, _) in itertools.pairwise(spans):
        assert prev_end is not None and next_start == prev_end + 1


def test_plan_segments_small_file_fewer_segments():
    assert len(plan_segments(100_000, 8)) == 1
    assert plan_segments(100_000, 8) == [(0, 99_999)]


def test_segmented_download_completes_with_checksum(server: MediaServer, db: Database, dest: Path):
    data = payload(4 * MB, 7)
    url = server.add("/big.bin", data)
    job = db.create_job(url, str(dest), "big.bin")

    download = SegmentedDownload(db, job, connections=8)
    status = download.run()

    assert status is JobStatus.COMPLETED
    assert sha256_file(dest / "big.bin") == sha256(data)
    assert not (dest / "big.bin.gl-part").exists()
    # >= 8: dynamic segmentation may split segments as fast workers steal work.
    assert len(download._segments) >= 8
    assert all(segment.is_complete for segment in download._segments)
    assert server.request_count("/big.bin") >= 9  # probe + 8+ range requests
    # Completion prunes the segment rows, so the job row must carry the total.
    assert db.segments_for(job.id) == []
    finished = db.get_job(job.id)
    assert finished is not None and db.stored_progress(finished) == len(data)


def test_downloader_uses_http1_for_real_parallel_connections(
    server: MediaServer, db: Database, dest: Path, monkeypatch: pytest.MonkeyPatch
):
    # HTTP/2 multiplexes every range request onto one socket (one congestion
    # window), collapsing the N-connection accelerator to single-connection
    # speed. The downloader must build an HTTP/1.1 client whose pool can hold a
    # live connection per segment - otherwise "8 connections" silently crawls.
    from app.core import net

    captured: dict[str, object] = {}
    real_build = net.build_client

    def spy(**kwargs: object) -> object:
        captured.update(kwargs)
        return real_build(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("app.core.downloader.net.build_client", spy)

    url = server.add("/h1.bin", payload(2 * MB, 3))
    job = db.create_job(url, str(dest), "h1.bin")

    status = SegmentedDownload(db, job, connections=8).run()

    assert status is JobStatus.COMPLETED
    assert captured.get("http2") is False
    limits = captured.get("limits")
    assert limits is not None
    assert limits.max_connections >= 8  # type: ignore[attr-defined]
    assert limits.max_keepalive_connections >= 8  # type: ignore[attr-defined]


def test_downloader_refuses_a_body_the_server_compressed_anyway(
    server: MediaServer, db: Database, dest: Path
):
    # Segments are written straight off the wire, which is only safe while the
    # bytes on the wire are the bytes of the file. Every request asks for
    # identity; a server that answers with a content-encoding regardless is
    # describing a *different* stream than its byte ranges do, so the download
    # has to fail loudly rather than stitch compressed bytes into the file.
    url = server.add("/gz.bin", payload(2 * MB, 11), extra_headers=(("Content-Encoding", "gzip"),))
    job = db.create_job(url, str(dest), "gz.bin")

    status = SegmentedDownload(db, job, connections=2, max_retries=1, retry_backoff=0.01).run()

    assert status is JobStatus.FAILED
    failed = db.get_job(job.id)
    assert failed is not None and "gzip-compressed" in (failed.error or "")
    assert not (dest / "gz.bin").exists()


def test_finalize_survives_fsync_failure(
    server: MediaServer, db: Database, dest: Path, monkeypatch: pytest.MonkeyPatch
):
    # Windows' FlushFileBuffers rejects fsync on some handles with EBADF; a
    # download whose bytes are all on disk must still finalize, not fail.
    data = payload(500_000, 37)
    url = server.add("/f.bin", data)
    job = db.create_job(url, str(dest), "f.bin")

    def boom(_fd: int) -> None:
        raise OSError(9, "Bad file descriptor")

    monkeypatch.setattr("app.core.downloader.os.fsync", boom)
    status = SegmentedDownload(db, job, connections=4).run()

    assert status is JobStatus.COMPLETED
    assert sha256_file(dest / "f.bin") == sha256(data)
    assert not (dest / "f.bin.gl-part").exists()


def test_gated_download_passes_cookie_through(server: MediaServer, db: Database, dest: Path):
    data = payload(2 * MB, 21)
    url = server.add("/gated.bin", data, required_headers={"Cookie": "session=abc"})
    job = db.create_job(url, str(dest), "gated.bin")

    status = SegmentedDownload(db, job, connections=8, headers={"Cookie": "session=abc"}).run()

    assert status is JobStatus.COMPLETED
    assert sha256_file(dest / "gated.bin") == sha256(data)
    assert server.received_headers("/gated.bin")["cookie"] == "session=abc"


def test_gated_download_without_cookie_fails(server: MediaServer, db: Database, dest: Path):
    url = server.add("/gated.bin", payload(2 * MB, 22), required_headers={"Cookie": "session=abc"})
    job = db.create_job(url, str(dest), "gated.bin")

    status = SegmentedDownload(db, job, connections=8).run()

    assert status is JobStatus.FAILED
    assert not (dest / "gated.bin").exists()


def test_download_sends_default_browser_ua_and_referer(
    server: MediaServer, db: Database, dest: Path
):
    """A plain paste (no browser handoff) still goes out looking like a browser:
    a real User-Agent and a same-origin Referer. httpx's own UA and a missing
    Referer are what bot/hotlink filters answer with 403 - the reported bug."""
    from app.core import net

    data = payload(512 * 1024, 3)
    url = server.add("/plain.bin", data)
    job = db.create_job(url, str(dest), "plain.bin")

    assert SegmentedDownload(db, job, connections=4).run() is JobStatus.COMPLETED
    sent = server.received_headers("/plain.bin")
    assert sent["user-agent"] == net.DEFAULT_USER_AGENT
    assert sent["referer"] == net.default_referer(url)


def test_download_clears_hotlink_403_with_default_referer(
    server: MediaServer, db: Database, dest: Path
):
    """End to end: a server that 403s without a same-origin Referer now
    downloads on a plain paste, because the downloader sends one by default."""
    from app.core import net

    data = payload(512 * 1024, 4)
    ref = net.default_referer(server.url("/hotlink.bin"))
    url = server.add("/hotlink.bin", data, required_headers={"Referer": ref})
    job = db.create_job(url, str(dest), "hotlink.bin")

    assert SegmentedDownload(db, job, connections=4).run() is JobStatus.COMPLETED
    assert sha256_file(dest / "hotlink.bin") == sha256(data)


def test_handoff_referer_overrides_default(server: MediaServer, db: Database, dest: Path):
    """A real Referer from the browser handoff wins over the same-origin default,
    so a CDN that checks Referer against the *page* still gets the right one."""
    data = payload(256 * 1024, 6)
    url = server.add("/watch.bin", data, required_headers={"Referer": "https://site.example/watch"})
    job = db.create_job(url, str(dest), "watch.bin")

    status = SegmentedDownload(
        db, job, connections=4, headers={"Referer": "https://site.example/watch"}
    ).run()

    assert status is JobStatus.COMPLETED
    assert server.received_headers("/watch.bin")["referer"] == "https://site.example/watch"


def test_single_connection_fallback_when_no_ranges(server: MediaServer, db: Database, dest: Path):
    data = payload(1 * MB, 11)
    url = server.add("/legacy.bin", data, supports_ranges=False)
    job = db.create_job(url, str(dest), "legacy.bin")

    download = SegmentedDownload(db, job, connections=8)
    status = download.run()

    assert status is JobStatus.COMPLETED
    assert sha256_file(dest / "legacy.bin") == sha256(data)
    assert len(download._segments) == 1  # one stream, not eight range requests


def test_unknown_content_length(server: MediaServer, db: Database, dest: Path):
    data = payload(700_000, 13)
    url = server.add("/stream.bin", data, supports_ranges=False, send_content_length=False)
    job = db.create_job(url, str(dest), "stream.bin")

    status = SegmentedDownload(db, job, connections=8).run()

    assert status is JobStatus.COMPLETED
    assert sha256_file(dest / "stream.bin") == sha256(data)
    fresh = db.get_job(job.id)
    assert fresh is not None
    assert fresh.total_size == len(data)


def test_download_follows_redirects(server: MediaServer, db: Database, dest: Path):
    data = payload(2 * MB, 17)
    server.add("/actual.bin", data)
    url = server.add("/jump", redirect_to="/actual.bin")
    job = db.create_job(url, str(dest), "actual.bin")

    status = SegmentedDownload(db, job, connections=8).run()

    assert status is JobStatus.COMPLETED
    assert sha256_file(dest / "actual.bin") == sha256(data)


def test_flaky_server_connection_drops_are_retried(server: MediaServer, db: Database, dest: Path):
    data = payload(2 * MB, 19)
    # Requests 2..9 (the first attempt of every segment) get cut mid-body.
    url = server.add("/flaky.bin", data, cut_after=100_000, cut_from=2, cut_until=9)
    job = db.create_job(url, str(dest), "flaky.bin")

    status = SegmentedDownload(db, job, connections=8, retry_backoff=0.05).run()

    assert status is JobStatus.COMPLETED
    assert sha256_file(dest / "flaky.bin") == sha256(data)
    assert server.request_count("/flaky.bin") > 9  # retries actually happened


def test_permanently_broken_server_fails_cleanly(server: MediaServer, db: Database, dest: Path):
    data = payload(1 * MB, 23)
    # Every request after the probe drops before sending a single byte.
    url = server.add("/dead.bin", data, cut_after=0, cut_from=2, cut_until=10**9)
    job = db.create_job(url, str(dest), "dead.bin")

    status = SegmentedDownload(db, job, connections=4, max_retries=1, retry_backoff=0.05).run()

    assert status is JobStatus.FAILED
    fresh = db.get_job(job.id)
    assert fresh is not None
    assert fresh.error is not None and "giving up" in fresh.error
    assert not (dest / "dead.bin").exists()


def test_http_error_fails_with_friendly_message(db: Database, dest: Path, server: MediaServer):
    job = db.create_job(server.url("/nope.bin"), str(dest), "nope.bin")
    status = SegmentedDownload(db, job).run()
    assert status is JobStatus.FAILED
    fresh = db.get_job(job.id)
    assert fresh is not None
    assert fresh.error is not None and "404" in fresh.error


def test_zero_byte_file(server: MediaServer, db: Database, dest: Path):
    url = server.add("/empty.bin", b"")
    job = db.create_job(url, str(dest), "empty.bin")
    status = SegmentedDownload(db, job).run()
    assert status is JobStatus.COMPLETED
    assert (dest / "empty.bin").stat().st_size == 0


def test_pause_persists_and_resume_completes(server: MediaServer, db: Database, dest: Path):
    data = payload(6 * MB, 29)
    url = server.add("/slow.bin", data, chunk_size=32 * 1024, delay_per_chunk=0.03)
    job = db.create_job(url, str(dest), "slow.bin")

    download = SegmentedDownload(db, job, connections=8)
    results: list[JobStatus] = []
    thread = threading.Thread(target=lambda: results.append(download.run()))
    thread.start()
    wait_for(lambda: download.bytes_downloaded > 1 * MB)
    download.pause()
    thread.join(timeout=30)
    assert not thread.is_alive()

    assert results == [JobStatus.PAUSED]
    persisted = db.job_downloaded(job.id)
    assert persisted > 0
    assert (dest / "slow.bin.gl-part").exists()
    assert not (dest / "slow.bin").exists()

    served_before_resume = server.served_bytes("/slow.bin")
    resumed_job = db.get_job(job.id)
    assert resumed_job is not None
    status = SegmentedDownload(db, resumed_job, connections=8).run()

    assert status is JobStatus.COMPLETED
    assert sha256_file(dest / "slow.bin") == sha256(data)
    resumed_bytes = server.served_bytes("/slow.bin") - served_before_resume
    assert resumed_bytes < len(data)  # it resumed; it did not start over


def test_cancel_removes_partial_file(server: MediaServer, db: Database, dest: Path):
    data = payload(6 * MB, 31)
    url = server.add("/cancel.bin", data, chunk_size=32 * 1024, delay_per_chunk=0.03)
    job = db.create_job(url, str(dest), "cancel.bin")

    download = SegmentedDownload(db, job, connections=8)
    results: list[JobStatus] = []
    thread = threading.Thread(target=lambda: results.append(download.run()))
    thread.start()
    wait_for(lambda: download.bytes_downloaded > 256 * 1024)
    download.cancel()
    thread.join(timeout=30)
    assert not thread.is_alive()

    assert results == [JobStatus.CANCELLED]
    assert not (dest / "cancel.bin.gl-part").exists()
    assert not (dest / "cancel.bin").exists()
    assert db.job_downloaded(job.id) == 0


def test_speed_limiter_caps_throughput(server: MediaServer, db: Database, dest: Path):
    from app.core.ratelimit import RateLimiter

    data = payload(2 * MB, 41)
    url = server.add("/capped.bin", data)
    job = db.create_job(url, str(dest), "capped.bin")

    # 2 MB at 1 MB/s with a 1 MB starting burst: at least ~1 s of throttling.
    limiter = RateLimiter(1 * MB)
    started = time.monotonic()
    status = SegmentedDownload(db, job, connections=4, limiter=limiter).run()
    elapsed = time.monotonic() - started

    assert status is JobStatus.COMPLETED
    assert sha256_file(dest / "capped.bin") == sha256(data)
    assert elapsed >= 0.8


def test_steal_segment_splits_the_biggest_remainder(db: Database, dest: Path):
    """Dynamic segmentation: an idle worker steals the tail of the segment
    with the most bytes left, and both halves stay well-formed."""
    job = db.create_job("http://x.test/f.bin", str(dest), "f.bin")
    job.resumable = True
    task = SegmentedDownload(db, job, connections=4)
    # One big segment [0, 3_999_999], nothing downloaded yet.
    task._segments = db.replace_segments(job.id, [(0, 3_999_999)])
    victim = task._segments[0]

    stolen = task._steal_segment()
    assert stolen is not None
    # The victim shrank; the stolen tail continues exactly where it ends.
    assert victim.end == stolen.start - 1
    assert stolen.end == 3_999_999
    assert stolen.downloaded == 0
    # Together they still cover the whole range with no gap or overlap.
    assert victim.start == 0 and stolen.end == 3_999_999
    # Persisted so a crash mid-way can resume the new layout.
    persisted = {(s.start, s.end) for s in db.segments_for(job.id)}
    assert (victim.start, victim.end) in persisted
    assert (stolen.start, stolen.end) in persisted


def test_steal_declines_when_remainder_too_small(db: Database, dest: Path):
    job = db.create_job("http://x.test/f.bin", str(dest), "f.bin")
    job.resumable = True
    task = SegmentedDownload(db, job, connections=4)
    task._segments = db.replace_segments(job.id, [(0, 100_000)])  # below STEAL_THRESHOLD
    assert task._steal_segment() is None


def test_per_download_limiter_caps_throughput(server: MediaServer, db: Database, dest: Path):
    from app.core.ratelimit import RateLimiter

    data = payload(2 * MB, 47)
    url = server.add("/jobcap.bin", data)
    job = db.create_job(url, str(dest), "jobcap.bin")

    # The global limiter is unlimited; the per-download one does the capping.
    job_limiter = RateLimiter(1 * MB)
    started = time.monotonic()
    status = SegmentedDownload(
        db, job, connections=4, limiter=RateLimiter(0), job_limiter=job_limiter
    ).run()
    elapsed = time.monotonic() - started

    assert status is JobStatus.COMPLETED
    assert sha256_file(dest / "jobcap.bin") == sha256(data)
    assert elapsed >= 0.8


def test_insufficient_disk_space_fails_cleanly(
    server: MediaServer, db: Database, dest: Path, monkeypatch: pytest.MonkeyPatch
):
    import shutil as real_shutil

    fake_usage = real_shutil.disk_usage(dest)._replace(free=1024)
    monkeypatch.setattr("app.core.downloader.shutil.disk_usage", lambda _p: fake_usage)
    url = server.add("/huge.bin", payload(1 * MB, 43))
    job = db.create_job(url, str(dest), "huge.bin")

    status = SegmentedDownload(db, job).run()

    assert status is JobStatus.FAILED
    fresh = db.get_job(job.id)
    assert fresh is not None
    assert fresh.error is not None and "disk space" in fresh.error
    assert not (dest / "huge.bin.gl-part").exists()


def test_never_overwrites_existing_file(server: MediaServer, db: Database, dest: Path):
    existing = dest / "taken.bin"
    existing.write_bytes(b"precious user data")
    data = payload(400_000, 37)
    url = server.add("/taken.bin", data)
    job = db.create_job(url, str(dest), "taken.bin")

    status = SegmentedDownload(db, job).run()

    assert status is JobStatus.COMPLETED
    assert existing.read_bytes() == b"precious user data"
    assert sha256_file(dest / "taken (1).bin") == sha256(data)
    fresh = db.get_job(job.id)
    assert fresh is not None
    assert fresh.filename == "taken (1).bin"


# --------------------------------------------------------- fair connection sharing


def test_pool_with_low_target_still_downloads_every_segment(
    server: MediaServer, db: Database, dest: Path
):
    """The claim-or-steal pool must cover every planned segment even when far
    fewer workers run than there are segments (the shared-budget case)."""
    data = payload(4 * MB, 91)
    url = server.add("/pool.bin", data)
    job = db.create_job(url, str(dest), "pool.bin")
    job.resumable = True
    # 16 segments planned, but only 2 connections allowed at any moment.
    status = SegmentedDownload(db, job, connections=16, connections_target=lambda: 2).run()
    assert status is JobStatus.COMPLETED
    assert sha256_file(dest / "pool.bin") == sha256(data)


def test_pool_never_exceeds_the_connection_target(server: MediaServer, db: Database, dest: Path):
    """The live worker count must stay within the target, so a download can't
    hog more than its fair share of the connection budget."""
    from app.core.ratelimit import RateLimiter

    data = payload(4 * MB, 77)
    url = server.add("/cap.bin", data)
    job = db.create_job(url, str(dest), "cap.bin")
    task = SegmentedDownload(
        db,
        job,
        connections=16,
        connections_target=lambda: 3,
        job_limiter=RateLimiter(2 * MB),  # slow it enough to sample the pool
    )
    peak = 0
    stop = threading.Event()

    def sample() -> None:
        nonlocal peak
        while not stop.is_set():
            peak = max(peak, task._active_workers)
            time.sleep(0.005)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    status = task.run()
    stop.set()
    sampler.join(timeout=1)

    assert status is JobStatus.COMPLETED
    assert sha256_file(dest / "cap.bin") == sha256(data)
    assert 1 <= peak <= 3  # ran in parallel, but never above the target


def test_fair_share_connections_splits_the_budget(db: Database):
    """Each unpinned download gets budget // (number running); a lone one gets
    the whole budget; a pinned download doesn't count against the split."""
    from app.core.manager import DownloadManager
    from app.core.settings import Settings

    class _Task:
        def __init__(self, shares: bool) -> None:
            self.shares_budget = shares

        def run(self) -> JobStatus:
            return JobStatus.COMPLETED

        def pause(self) -> None: ...
        def cancel(self) -> None: ...

        @property
        def bytes_downloaded(self) -> int:
            return 0

    manager = DownloadManager(db, settings=Settings(db), max_concurrent=0, connections=16)
    try:
        assert manager._fair_share_connections(1) == 16  # a lone job: whole budget
        with manager._cond:
            manager._active[1] = _Task(shares=True)
        assert manager._fair_share_connections(1) == 16
        with manager._cond:
            manager._active[2] = _Task(shares=True)
        assert manager._fair_share_connections(1) == 8  # two share -> half each
        assert manager._fair_share_connections(2) == 8
        with manager._cond:
            manager._active[3] = _Task(shares=True)
            manager._active[4] = _Task(shares=True)
        assert manager._fair_share_connections(3) == 4  # four -> a quarter each
        with manager._cond:
            for job_id in range(5, 9):
                manager._active[job_id] = _Task(shares=True)
        # Eight sharers: 16 // 8 = 2 each. Never invent sockets above the budget.
        assert manager._fair_share_connections(8) == 2
        with manager._cond:
            manager._active[99] = _Task(shares=False)  # a pinned download
        assert manager._fair_share_connections(8) == 2  # ...doesn't change the split
    finally:
        with manager._cond:
            manager._active.clear()
        manager.shutdown()


def test_fair_share_connections_spends_the_whole_budget(db: Database):
    """An uneven split must not idle the leftover connections: 8 across 3
    downloads is 3+3+2, not 2+2+2 with a quarter of the pool unused."""
    from app.core.manager import DownloadManager
    from app.core.settings import Settings

    class _Task:
        shares_budget = True

        def run(self) -> JobStatus:
            return JobStatus.COMPLETED

        def pause(self) -> None: ...
        def cancel(self) -> None: ...

        @property
        def bytes_downloaded(self) -> int:
            return 0

    manager = DownloadManager(db, settings=Settings(db), max_concurrent=0, connections=8)
    try:
        with manager._cond:
            for job_id in (4, 7, 9):
                manager._active[job_id] = _Task()
        shares = [manager._fair_share_connections(job_id) for job_id in (4, 7, 9)]
        assert sorted(shares) == [2, 3, 3]
        assert sum(shares) == 8  # every connection in the budget is spoken for
    finally:
        with manager._cond:
            manager._active.clear()
        manager.shutdown()


def test_fresh_job_reuses_resolve_probe_without_second_round_trip(
    server: MediaServer, db: Database, dest: Path, monkeypatch: pytest.MonkeyPatch
):
    """add/resolve already probed the URL - starting must not Range-probe again."""
    from app.core import downloader as downloader_mod

    data = payload(2 * MB, 19)
    url = server.add("/reuse.bin", data)
    job = db.create_job(url, str(dest), "reuse.bin")
    job.final_url = url
    job.total_size = len(data)
    job.resumable = True
    job.etag = '"reuse-etag"'
    db.update_job_probe(job)
    reloaded = db.get_job(job.id)
    assert reloaded is not None

    calls: list[str] = []

    def boom(client, url, extra_headers=None):
        calls.append(url)
        raise AssertionError("probe must not run when the job already has probe fields")

    monkeypatch.setattr(downloader_mod, "probe", boom)

    status = SegmentedDownload(db, reloaded, connections=4).run()

    assert status is JobStatus.COMPLETED
    assert calls == []
    assert sha256_file(dest / "reuse.bin") == sha256(data)


def test_a_stolen_tail_is_never_handed_to_two_workers(
    server: MediaServer, db: Database, dest: Path
):
    """The classic corruption: a split segment claimed by two workers at once.

    Both would write into the same Segment and race its `downloaded` counter,
    so each wrote its bytes at the other's offset and the finished file failed
    its checksum - intermittently, on fast links with many connections.
    """
    url = server.add("/steal.bin", payload(6 * MB, 71))
    job = db.create_job(url, str(dest), "steal.bin")
    download = SegmentedDownload(db, job, connections=8)
    download._segments = db.replace_segments(job.id, [(0, 6 * MB - 1)])

    seen: list[int] = []
    barrier = threading.Barrier(4, timeout=10)

    def claim() -> None:
        barrier.wait()
        segment = download._next_work()
        if segment is not None:
            seen.append(segment.id)

    workers = [threading.Thread(target=claim) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert len(seen) == len(set(seen))  # no segment claimed twice


def test_a_retryable_status_retries_instead_of_killing_the_job(
    server: MediaServer, db: Database, dest: Path
):
    """One flaky 503 must not throw away the whole download.

    It used to raise DownloadError, which stops every other connection - and
    any "http 4xx/5xx" text also matches the manager's permanent-failure
    markers, so the job would never even be retried.
    """
    data = payload(2 * MB, 73)
    url = server.add("/flaky.bin", data, fail_status=503, fail_from=2, fail_until=3)
    job = db.create_job(url, str(dest), "flaky.bin")

    status = SegmentedDownload(db, job, connections=4, retry_backoff=0.01).run()

    assert status is JobStatus.COMPLETED
    assert sha256_file(dest / "flaky.bin") == sha256(data)


def test_mid_download_403_narrows_connections_instead_of_failing(
    server: MediaServer, db: Database, dest: Path
):
    """A host that serves the first segments and then starts answering 403 is
    rate limiting the parallelism, not refusing the file.

    The reported bug: it burned five retries at full width, raised
    DownloadError, and threw away a download that was already 72% done. The
    fix narrows the pool and finishes the job.
    """
    data = payload(2 * MB, 91)
    # Requests 4..9 are refused: enough to outlast the plain retry budget at
    # the original width, so only backing off can get through this.
    url = server.add("/limited.bin", data, fail_status=403, fail_from=4, fail_until=9)
    job = db.create_job(url, str(dest), "limited.bin")

    download = SegmentedDownload(db, job, connections=8, retry_backoff=0.01)
    status = download.run()

    assert status is JobStatus.COMPLETED
    assert sha256_file(dest / "limited.bin") == sha256(data)
    # It learned to use fewer connections rather than giving up.
    assert download._connection_ceiling is not None
    assert download._connection_ceiling < 8


def test_rate_limit_ceiling_is_remembered_for_the_next_run(
    server: MediaServer, db: Database, dest: Path
):
    """The tolerated connection count is stored on the job, so a retry (or the
    next run) starts there instead of tripping the same block again."""
    data = payload(2 * MB, 92)
    url = server.add("/limited.bin", data, fail_status=403, fail_from=4, fail_until=9)
    job = db.create_job(url, str(dest), "limited.bin")

    download = SegmentedDownload(db, job, connections=8, retry_backoff=0.01)
    assert download.run() is JobStatus.COMPLETED

    stored = db.get_job(job.id)
    assert stored is not None
    learned = int(stored.options.get("learned_connections") or 0)
    assert 0 < learned < 8


def test_a_hard_403_on_every_request_still_fails_as_rate_limiting(
    server: MediaServer, db: Database, dest: Path
):
    """When even one connection is refused, the job does fail - but with the
    marker that makes the manager treat it as transient and retry, rather than
    the bare "HTTP 403" that reads as permanent and strands it forever."""
    from app.core.downloader import RATE_LIMIT_MARKER
    from app.core.manager import _is_transient_error

    data = payload(512 * 1024, 93)
    url = server.add("/blocked.bin", data, fail_status=403, fail_from=2, fail_until=10_000)
    job = db.create_job(url, str(dest), "blocked.bin")

    status = SegmentedDownload(
        db, job, connections=4, retry_backoff=0.01, max_pushback_retries=2
    ).run()

    assert status is JobStatus.FAILED
    failed = db.get_job(job.id)
    assert failed is not None and failed.error is not None
    assert RATE_LIMIT_MARKER in failed.error
    assert _is_transient_error(failed.error)  # so the manager schedules a retry


def test_retry_after_header_is_honored(server: MediaServer, db: Database, dest: Path):
    """A server that says when to come back is obeyed, instead of the download
    guessing its own backoff."""
    import httpx

    from app.core.downloader import _retry_after_seconds

    assert _retry_after_seconds(httpx.Headers({"retry-after": "5"})) == 5.0
    # Clamped: a huge value must not park the download for hours.
    assert _retry_after_seconds(httpx.Headers({"retry-after": "99999"})) == 60.0
    # Absent or unparsable -> fall back to our own backoff.
    assert _retry_after_seconds(httpx.Headers({})) is None
    assert _retry_after_seconds(httpx.Headers({"retry-after": "soon"})) is None
    # HTTP-date form resolves to a delay, never a negative one.
    past = _retry_after_seconds(httpx.Headers({"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}))
    assert past == 0.0


def test_segments_are_requested_without_compression(server: MediaServer, db: Database, dest: Path):
    """identity only: httpx would transparently decompress a gzipped response
    while Content-Length and every byte offset still described the compressed
    stream, so segments landed at the wrong offsets."""
    data = payload(1 * MB, 74)
    url = server.add("/plain.bin", data)
    job = db.create_job(url, str(dest), "plain.bin")

    assert SegmentedDownload(db, job, connections=4).run() is JobStatus.COMPLETED
    assert server.received_headers("/plain.bin")["accept-encoding"] == "identity"


def test_an_unsized_resumable_job_streams_instead_of_failing(
    server: MediaServer, db: Database, dest: Path
):
    """A server can advertise ranges yet not say how big the file is. The
    single unsized segment must be streamed whole - it used to fail instantly
    with "segment 0 has no end offset"."""
    data = payload(300_000, 75)
    url = server.add("/nosize.bin", data, send_content_length=False)
    job = db.create_job(url, str(dest), "nosize.bin")
    job.resumable = True  # as a 206-without-total probe would have recorded it
    job.final_url = url
    db.update_job_probe(job)
    reloaded = db.get_job(job.id)
    assert reloaded is not None

    status = SegmentedDownload(db, reloaded, connections=4).run()

    assert status is JobStatus.COMPLETED
    assert sha256_file(dest / "nosize.bin") == sha256(data)
