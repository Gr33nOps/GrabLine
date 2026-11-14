"""Regressions found by the November 2026 independent audit.

Each test here failed against the code as it stood before the audit and pins
the defect it reproduced.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from app.core.downloader import SegmentedDownload
from app.core.manager import DownloadManager, _is_transient_error
from app.core.models import JobKind, JobStatus
from app.core.settings import Settings
from app.db.database import Database
from app.engines.hls import HlsDownload
from app.tests.conftest import sha256_file, wait_for
from app.tests.media_server import MediaServer, payload, sha256

MB = 1024 * 1024


def _settled(db: Database, job_ids: list[int]) -> bool:
    jobs = [db.get_job(job_id) for job_id in job_ids]
    return all(job is not None and job.status is JobStatus.COMPLETED for job in jobs)


def test_two_downloads_with_one_name_never_share_a_part_file(
    server: MediaServer, db: Database, dest: Path
):
    """Different URLs, same basename, same folder, running at once.

    Both jobs used to resolve to ``thumb.bin.gl-part``: each preallocated,
    wrote and renamed the other's file, so one download vanished and the
    survivor could hold a mixture of the two.
    """
    first, second = payload(3 * MB, 101), payload(3 * MB, 202)
    url_a = server.add("/a/thumb.bin", first, delay_per_chunk=0.01)
    url_b = server.add("/b/thumb.bin", second, delay_per_chunk=0.01)
    manager = DownloadManager(db, settings=Settings(db), max_concurrent=2, connections=4)
    try:
        job_a = manager.add_url(url_a, dest)
        job_b = manager.add_url(url_b, dest)
        assert job_a.part_path != job_b.part_path
        wait_for(lambda: _settled(db, [job_a.id, job_b.id]), timeout=60)
    finally:
        manager.shutdown()
    files = sorted(path for path in dest.iterdir() if path.is_file())
    assert [path.name for path in files] == ["thumb (1).bin", "thumb.bin"]
    assert {sha256_file(path) for path in files} == {sha256(first), sha256(second)}


def test_a_server_supplied_name_cannot_collide_with_a_running_download(
    server: MediaServer, db: Database, dest: Path
):
    """The probe can rename a job (Content-Disposition) after it was added, so
    the uniqueness decided at add time has to be re-checked there."""
    first, second = payload(2 * MB, 303), payload(2 * MB, 404)
    disposition = 'attachment; filename="report.bin"'
    url_a = server.add("/one", first, content_disposition=disposition, delay_per_chunk=0.01)
    url_b = server.add("/two", second, content_disposition=disposition, delay_per_chunk=0.01)
    manager = DownloadManager(db, settings=Settings(db), max_concurrent=2, connections=4)
    try:
        job_a = manager.add_url(url_a, dest)
        job_b = manager.add_url(url_b, dest)
        wait_for(lambda: _settled(db, [job_a.id, job_b.id]), timeout=60)
    finally:
        manager.shutdown()
    files = [path for path in dest.iterdir() if path.is_file()]
    assert {sha256_file(path) for path in files} == {sha256(first), sha256(second)}


def test_a_file_that_shrank_since_the_probe_is_retried_not_abandoned(
    server: MediaServer, db: Database, dest: Path
):
    """A job can start long after it was probed (a schedule, a paused queue).
    If the file got smaller meanwhile, the out-of-range segments drew HTTP 416,
    which read as a permanent failure - though simply trying again re-probes
    and downloads the new file correctly."""
    data = payload(2 * MB, 5)
    url = server.add("/f.bin", data)
    job = db.create_job(url, str(dest), "f.bin")
    job.final_url, job.total_size, job.resumable = url, 3 * MB, True  # the stale probe
    db.update_job_probe(job)

    def attempt() -> JobStatus:
        fresh = db.get_job(job.id)
        assert fresh is not None
        return SegmentedDownload(db, fresh, connections=4, max_retries=1, retry_backoff=0.01).run()

    assert attempt() is JobStatus.FAILED
    failed = db.get_job(job.id)
    assert failed is not None and _is_transient_error(failed.error)
    assert attempt() is JobStatus.COMPLETED
    assert sha256_file(dest / "f.bin") == sha256(data)


# ----------------------------------------------------------------- HLS engine


def _hls_job(db: Database, url: str, dest: Path):
    return db.create_job(url, str(dest), "lecture.mp4", kind=JobKind.HLS, title="Lecture")


def test_a_chatty_ffmpeg_cannot_deadlock_on_its_error_pipe(
    db: Database, dest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """FFmpeg's stderr was read only after it exited. One that printed more
    than the pipe buffer blocked mid-write, produced no more output, and was
    killed by the stall guard as if the stream had died."""
    script = tmp_path / "noisy_ffmpeg.py"
    script.write_text(
        "import sys\n"
        "for _ in range(20000):\n"
        "    sys.stderr.write('x' * 80 + '\\n')\n"
        "sys.stderr.flush()\n"
        "open(sys.argv[1], 'wb').write(b'm' * 4096)\n"
    )
    # Nothing listens here, so the playlist fetch fails fast and the job goes
    # straight to the path where FFmpeg does the fetching.
    job = _hls_job(db, "http://127.0.0.1:9/stream.m3u8", dest)
    task = HlsDownload(db, job, ffmpeg_path=sys.executable, stall_timeout=4, max_attempts=1)
    monkeypatch.setattr(task, "_command", lambda part: [sys.executable, str(script), str(part)])

    assert task.run() is JobStatus.COMPLETED
    assert (dest / "lecture.mp4").stat().st_size == 4096


def test_cancel_during_the_local_remux_is_honoured(
    db: Database, dest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The remux ran to completion whatever the user did, so a download
    cancelled while combining still finished and left its file behind."""
    script = tmp_path / "slow_ffmpeg.py"
    script.write_text(
        "import sys, time\ntime.sleep(8)\nopen(sys.argv[1], 'wb').write(b'm' * 4096)\n"
    )
    real_popen = subprocess.Popen

    def slow_ffmpeg(command, **kwargs):
        return real_popen([sys.executable, str(script), command[-1]], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", slow_ffmpeg)
    job = _hls_job(db, "http://127.0.0.1:9/stream.m3u8", dest)
    task = HlsDownload(db, job, ffmpeg_path="ffmpeg")
    manifest = tmp_path / "video.m3u8"
    manifest.write_text("#EXTM3U\n")
    threading.Timer(0.5, task.cancel).start()

    started = time.monotonic()
    status = task._remux_local([manifest], job.part_path)

    assert status is None  # not COMPLETED: the caller settles the cancel
    assert time.monotonic() - started < 6
    assert not job.part_path.exists()
    assert not (dest / "lecture.mp4").exists()


def test_one_dead_segment_stops_the_fetch_instead_of_draining_the_playlist(
    server: MediaServer, db: Database, dest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A segment that fails for good used to be noticed only after every other
    segment had been downloaded - all of which were then thrown away."""
    from app.engines import hls

    monkeypatch.setattr(hls, "_SEGMENT_RETRIES", 0)
    slow = b"s" * 4096
    downloads = [(server.url("/s/missing.ts"), "video-00000.ts")]
    for index in range(1, 81):
        server.add(f"/s/seg{index:03d}.ts", slow, chunk_size=1024, delay_per_chunk=0.25)
        downloads.append((server.url(f"/s/seg{index:03d}.ts"), f"video-{index:05d}.ts"))
    task = HlsDownload(db, _hls_job(db, server.url("/s/index.m3u8"), dest), ffmpeg_path="ffmpeg")
    work = tmp_path / "work"
    work.mkdir()

    with pytest.raises(httpx.HTTPError):
        task._fetch_segments(downloads, work)

    requested = sum(1 for index in range(1, 81) if server.request_count(f"/s/seg{index:03d}.ts"))
    assert requested <= 40  # the in-flight batch at most, never all 80


def test_a_dropped_connection_keeps_the_segments_already_fetched(
    server: MediaServer, db: Database, dest: Path, monkeypatch: pytest.MonkeyPatch
):
    """A network drop mid-stream wiped every segment already on disk, so the
    retry started a multi-gigabyte stream from zero."""
    from app.engines import hls

    monkeypatch.setattr(hls, "_SEGMENT_RETRIES", 0)
    good = b"g" * 20_000
    server.add("/s/seg000.ts", good)
    # Every request for this one is cut mid-body: the line is down.
    # (slowly, so the healthy segment is safely on disk before it fails).
    server.add(
        "/s/seg001.ts",
        good,
        chunk_size=1024,
        delay_per_chunk=0.2,
        cut_after=4096,
        cut_from=1,
        cut_until=10_000,
    )
    playlist = "#EXTM3U\n#EXTINF:1.0,\nseg000.ts\n#EXTINF:1.0,\nseg001.ts\n#EXT-X-ENDLIST\n"
    job = _hls_job(db, server.url("/s/index.m3u8"), dest)
    task = HlsDownload(db, job, ffmpeg_path="ffmpeg")

    status = task._attempt_native(playlist, server.url("/s/index.m3u8"), job.part_path)

    assert status is None  # falls through to the FFmpeg fetch, as before
    work = dest / f".{job.part_path.name}.hls"
    assert (work / "video-00000.ts").read_bytes() == good


def test_cancelling_a_paused_stream_removes_its_segment_cache(db: Database, dest: Path):
    """A paused native HLS fetch keeps its segments in a hidden folder beside
    the part file. Cancelling or removing the job while it was not running
    deleted only the part file and left that folder - gigabytes, invisibly."""
    manager = DownloadManager(db, settings=Settings(db), max_concurrent=0)
    try:
        for action in (manager.cancel, manager.remove):
            job = _hls_job(db, "http://127.0.0.1:9/stream.m3u8", dest)
            db.set_job_status(job.id, JobStatus.PAUSED)
            work = dest / f".{job.part_path.name}.hls"
            work.mkdir()
            (work / "video-00000.ts").write_bytes(b"segment")
            action(job.id)
            assert not work.exists()
    finally:
        manager.shutdown()


def test_cancelling_a_paused_video_removes_its_partial_files(db: Database, dest: Path):
    """yt-dlp's scratch files outlived a paused download that was then
    cancelled: only a running job knew how to clean them up."""
    manager = DownloadManager(db, settings=Settings(db), max_concurrent=0)
    try:
        job = db.create_job(
            "https://example.test/watch", str(dest), "Clip.mp4", kind=JobKind.SMART, title="Clip"
        )
        db.set_job_status(job.id, JobStatus.PAUSED)
        scratch = ["Clip.f137.mp4.part", "Clip.f140.m4a.part", "Clip.f137.mp4.ytdl"]
        scratch += ["Clip.mp4.part-Frag7.part", "Clip.temp.mp4"]
        for name in scratch:
            (dest / name).write_bytes(b"partial")
        (dest / "Clip notes.txt").write_text("mine")
        (dest / "Clipboard.mp4.part").write_bytes(b"someone else's")

        manager.cancel(job.id)

        assert sorted(path.name for path in dest.iterdir()) == [
            "Clip notes.txt",
            "Clipboard.mp4.part",
        ]
    finally:
        manager.shutdown()


def test_a_full_disk_is_not_retried_automatically():
    # The pre-flight check already says so in words the retry logic knows; the
    # same condition met mid-write surfaces as the OS error text instead.
    assert not _is_transient_error("[Errno 28] No space left on device")
    assert _is_transient_error("segment 3: giving up after 5 retries (timed out)")


def test_cancel_preserves_partial_files_for_a_different_dotted_title(db: Database, dest: Path):
    manager = DownloadManager(db, settings=Settings(db), max_concurrent=0)
    try:
        job = db.create_job("https://example.test/clip", str(dest), "Clip.mp4", kind=JobKind.SMART)
        db.set_job_status(job.id, JobStatus.PAUSED)
        others = [
            "Clip.notes.f137.mp4.part",
            "Clip.notes.mp4.part-Frag7.part",
            "Clip.mp4.part-Frag7.part.backup",
        ]
        for name in others:
            (dest / name).write_bytes(b"another download")
        (dest / "Clip.f137.mp4.part").write_bytes(b"this download")

        manager.cancel(job.id)

        assert not (dest / "Clip.f137.mp4.part").exists()
        for name in others:
            assert (dest / name).read_bytes() == b"another download"
    finally:
        manager.shutdown()
