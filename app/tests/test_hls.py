from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import pytest

from app.core.models import JobKind, JobStatus
from app.db.database import Database
from app.engines.hls import HlsDownload
from app.tests.media_fixtures import (
    FFMPEG,
    FFPROBE,
    make_hls,
    make_hls_audio,
    probe_duration,
    probe_streams,
)
from app.tests.media_server import MediaServer


def _hls_job(db: Database, url: str, dest: Path, options: dict[str, Any] | None = None):
    return db.create_job(
        url, str(dest), "lecture.mp4", kind=JobKind.HLS, title="Lecture", options=options
    )


def test_hls_requires_ffmpeg(db: Database, dest: Path):
    job = _hls_job(db, "http://127.0.0.1:1/x.m3u8", dest)
    status = HlsDownload(db, job, ffmpeg_path=None).run()
    assert status is JobStatus.FAILED
    fresh = db.get_job(job.id)
    assert fresh is not None
    assert fresh.error is not None and "FFmpeg" in fresh.error


@pytest.mark.skipif(FFMPEG is None, reason="needs a real ffmpeg")
def test_hls_reassembles_to_mp4(server: MediaServer, db: Database, dest: Path, tmp_path: Path):
    files = make_hls(tmp_path / "hls", seconds=2)
    assert "index.m3u8" in files
    for name, content in files.items():
        content_type = "application/vnd.apple.mpegurl" if name.endswith(".m3u8") else "video/mp2t"
        server.add(f"/hls/{name}", content, content_type=content_type)

    job = _hls_job(db, server.url("/hls/index.m3u8"), dest)
    status = HlsDownload(db, job, ffmpeg_path=FFMPEG).run()

    assert status is JobStatus.COMPLETED
    output = dest / "lecture.mp4"
    assert output.exists() and output.stat().st_size > 0
    assert not (dest / "lecture.mp4.gl-part").exists()
    fresh = db.get_job(job.id)
    assert fresh is not None
    assert fresh.total_size == output.stat().st_size
    if FFPROBE is not None:
        duration = probe_duration(output)
        assert duration is not None and 1.0 < duration < 3.5


@pytest.mark.skipif(FFMPEG is None, reason="needs a real ffmpeg")
def test_hls_broken_stream_fails_cleanly(server: MediaServer, db: Database, dest: Path):
    server.add("/broken.m3u8", b"this is not a playlist at all", content_type="text/plain")
    job = _hls_job(db, server.url("/broken.m3u8"), dest)
    status = HlsDownload(db, job, ffmpeg_path=FFMPEG, stall_timeout=15).run()
    assert status is JobStatus.FAILED
    fresh = db.get_job(job.id)
    assert fresh is not None
    assert fresh.error is not None
    assert not (dest / "lecture.mp4").exists()
    assert not (dest / "lecture.mp4.gl-part").exists()


@pytest.mark.skipif(FFMPEG is None, reason="needs a real ffmpeg")
def test_hls_variant_with_separate_audio(
    server: MediaServer, db: Database, dest: Path, tmp_path: Path
):
    """F2.1: a chosen variant plus its audio rendition mux into one mp4."""
    video_files = make_hls(tmp_path / "video", seconds=2)
    audio_files = make_hls_audio(tmp_path / "audio", seconds=2)
    for name, content in video_files.items():
        content_type = "application/vnd.apple.mpegurl" if name.endswith(".m3u8") else "video/mp2t"
        server.add(f"/video/{name}", content, content_type=content_type)
    for name, content in audio_files.items():
        content_type = "application/vnd.apple.mpegurl" if name.endswith(".m3u8") else "video/mp2t"
        server.add(f"/audio/{name}", content, content_type=content_type)
    master = (
        "#EXTM3U\n"
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="Main",DEFAULT=YES,URI="audio/audio.m3u8"\n'
        '#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=128x72,AUDIO="aud"\n'
        "video/index.m3u8\n"
    )
    server.add("/master.m3u8", master.encode(), content_type="application/vnd.apple.mpegurl")

    job = _hls_job(
        db,
        server.url("/master.m3u8"),
        dest,
        options={
            "variant_url": server.url("/video/index.m3u8"),
            "audio_url": server.url("/audio/audio.m3u8"),
            "quality_label": "72p",
        },
    )
    status = HlsDownload(db, job, ffmpeg_path=FFMPEG).run()

    assert status is JobStatus.COMPLETED
    output = dest / "lecture.mp4"
    assert output.exists() and output.stat().st_size > 0
    if FFPROBE is not None:
        kinds = probe_streams(output)
        assert "video" in kinds and "audio" in kinds


def test_transient_failure_is_retried_once(server: MediaServer, db: Database, dest: Path):
    """A nonzero FFmpeg exit gets exactly one retry before failing the job."""
    vod = b"#EXTM3U\n#EXTINF:2.0,\nseg000.ts\n#EXT-X-ENDLIST\n"
    server.add("/vod.m3u8", vod, content_type="application/vnd.apple.mpegurl")
    marker = dest / "invocations.txt"
    stub = dest / "fake-ffmpeg.sh"
    stub.write_text(f'#!/bin/sh\necho run >> "{marker}"\nexit 1\n')
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)

    job = _hls_job(db, server.url("/vod.m3u8"), dest)
    status = HlsDownload(db, job, ffmpeg_path=str(stub)).run()

    assert status is JobStatus.FAILED
    assert marker.read_text().count("run") == 2
    assert not (dest / "lecture.mp4.gl-part").exists()


def test_remux_failure_keeps_segments_and_does_not_redownload(
    server: MediaServer, db: Database, dest: Path, tmp_path: Path
):
    """Every segment was fetched but FFmpeg can't combine them: the job must fail
    cleanly and KEEP the fetched parts for a retry - never wipe them and
    re-download the whole stream from scratch (the reported bug). Only the two
    remux attempts run; the FFmpeg-direct fallback does not."""
    files = make_hls(tmp_path / "hls", seconds=2)
    for name, content in files.items():
        ct = "application/vnd.apple.mpegurl" if name.endswith(".m3u8") else "video/mp2t"
        server.add(f"/hls/{name}", content, content_type=ct)
    marker = dest / "invocations.txt"
    stub = dest / "fake-ffmpeg.sh"
    stub.write_text(f'#!/bin/sh\necho run >> "{marker}"\nexit 1\n')  # remux always fails
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)

    job = _hls_job(db, server.url("/hls/index.m3u8"), dest)
    status = HlsDownload(db, job, ffmpeg_path=str(stub)).run()

    assert status is JobStatus.FAILED
    assert marker.read_text().count("run") == 2  # two remux tries, NOT a re-download
    work = job.part_path.parent / f".{job.part_path.name}.hls"
    assert work.exists() and any(work.glob("*.ts"))  # fetched segments kept for retry


def test_looks_complete_accepts_full_stream(db: Database, dest: Path):
    """A stream FFmpeg muxed to ~its full duration is kept even on a nonzero
    exit (a trailing segment 404 must not throw away a finished file)."""
    job = _hls_job(db, "http://x/live.m3u8", dest)
    task = HlsDownload(db, job, ffmpeg_path="ffmpeg")
    part = job.part_path
    part.write_bytes(b"x" * 1024)
    task._duration = 100.0

    task._out_time = 99.5  # 99.5% muxed -> complete
    assert task._looks_complete(part) is True
    task._out_time = 40.0  # less than 98% -> not complete
    assert task._looks_complete(part) is False
    task._out_time = 99.5
    task._duration = None  # unknown duration -> cannot claim complete
    assert task._looks_complete(part) is False


def test_native_fetch_skips_segments_already_on_disk(
    server: MediaServer, db: Database, dest: Path, tmp_path: Path
):
    """Resume: a segment a previous run already fetched (renamed into place) is
    counted and skipped, not re-downloaded. This is what keeps a paused multi-GB
    HLS stream from starting over."""
    seg0 = b"already-fetched-segment-zero"
    seg1 = b"the-only-segment-we-still-need"
    url0 = server.add("/s/seg000.ts", seg0, content_type="video/mp2t")
    url1 = server.add("/s/seg001.ts", seg1, content_type="video/mp2t")

    job = _hls_job(db, server.url("/s/index.m3u8"), dest)
    task = HlsDownload(db, job, ffmpeg_path="ffmpeg")  # ffmpeg is not invoked here

    work = tmp_path / "work"
    work.mkdir()
    (work / "video-00000.ts").write_bytes(seg0)  # a previous run finished this one

    ok = task._fetch_segments([(url0, "video-00000.ts"), (url1, "video-00001.ts")], work)

    assert ok is True
    assert server.request_count("/s/seg000.ts") == 0  # already on disk: not refetched
    assert server.request_count("/s/seg001.ts") == 1  # the only one fetched
    assert (work / "video-00001.ts").read_bytes() == seg1
    assert task._downloaded == len(seg0) + len(seg1)  # both count toward progress
    assert not (work / "video-00001.ts.part").exists()  # temp renamed away


def test_native_fetch_retries_a_dropped_segment(
    server: MediaServer, db: Database, dest: Path, tmp_path: Path
):
    """A segment whose connection drops mid-transfer is retried on a fresh one,
    so one flaky part cannot stall a worker or fail the whole stream - the cause
    of the '0-byte .part files and a crawling speed' report."""
    good = b"x" * 40_000
    server.add("/s/seg000.ts", good, content_type="video/mp2t")
    # seg001 drops after 100 body bytes on the FIRST request, then serves fully.
    server.add(
        "/s/seg001.ts", good, content_type="video/mp2t", cut_after=100, cut_from=1, cut_until=1
    )
    server.add("/s/seg002.ts", good, content_type="video/mp2t")

    job = _hls_job(db, server.url("/s/index.m3u8"), dest)
    task = HlsDownload(db, job, ffmpeg_path="ffmpeg")  # ffmpeg is not invoked here
    work = tmp_path / "work"
    work.mkdir()
    downloads = [
        (server.url("/s/seg000.ts"), "video-00000.ts"),
        (server.url("/s/seg001.ts"), "video-00001.ts"),
        (server.url("/s/seg002.ts"), "video-00002.ts"),
    ]

    ok = task._fetch_segments(downloads, work)

    assert ok is True
    for i in range(3):
        assert (work / f"video-0000{i}.ts").read_bytes() == good  # all complete despite the drop
    assert server.request_count("/s/seg001.ts") >= 2  # the dropped part was retried
    assert not any(p.name.endswith(".part") for p in work.iterdir())  # no leftovers


def test_paused_native_fetch_keeps_its_progress(db: Database, dest: Path):
    """A paused native fetch keeps its segments on disk, so its progress count
    must survive the pause; only the non-resumable FFmpeg path zeroes it."""
    job = _hls_job(db, "https://x/s.m3u8", dest)
    task = HlsDownload(db, job, ffmpeg_path="ffmpeg")
    part = job.part_path
    part.parent.mkdir(parents=True, exist_ok=True)
    db.update_job_downloaded(job.id, 500_000)

    assert task._settle_stopped(part, keep_progress=True) is JobStatus.PAUSED
    fresh = db.get_job(job.id)
    assert fresh is not None and fresh.downloaded == 500_000  # not reset

    assert task._settle_stopped(part) is JobStatus.PAUSED  # FFmpeg path: no keep
    fresh2 = db.get_job(job.id)
    assert fresh2 is not None and fresh2.downloaded == 0


def test_discard_removes_leftover_part(db: Database, dest: Path, tmp_path: Path):
    from app.engines.hls import _discard

    leftover = tmp_path / "x.mp4.gl-part"
    leftover.write_bytes(b"junk")
    _discard(leftover)
    assert not leftover.exists()
    _discard(leftover)  # already gone: no error


def test_live_playlist_is_refused_clearly(server: MediaServer, db: Database, dest: Path):
    # A media playlist without #EXT-X-ENDLIST is a live stream in progress.
    # FFmpeg would happily poll it forever; Grabline must refuse up front.
    live = b"#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2.0,\nseg000.ts\n"
    server.add("/live.m3u8", live, content_type="application/vnd.apple.mpegurl")
    job = _hls_job(db, server.url("/live.m3u8"), dest)
    # ffmpeg is never launched for a refused stream, so the path can be fake.
    status = HlsDownload(db, job, ffmpeg_path="ffmpeg-never-invoked").run()
    assert status is JobStatus.FAILED
    fresh = db.get_job(job.id)
    assert fresh is not None
    assert fresh.error is not None and "live" in fresh.error.lower()


def test_estimate_does_not_read_absurdly_high_early():
    """The early size estimate used to extrapolate the container header over a
    fraction of a second into hundreds of GB. Anchored from a steady window,
    it stays near the true size once it appears."""
    import tempfile
    from pathlib import Path

    from app.core.models import JobKind
    from app.db.database import Database
    from app.engines.hls import HlsDownload

    tmp = Path(tempfile.mkdtemp())
    db = Database(tmp / "t.db")
    job = db.create_job("https://x/s.m3u8", str(tmp), "s.mp4", kind=JobKind.HLS, title="s")
    task = HlsDownload(db, job, ffmpeg_path=None)
    task._duration = 7200.0  # 2-hour video
    header, bitrate = 2_000_000, (2_000_000_000 - 2_000_000) / 7200.0  # ~2 GB total

    last = 0
    seen: list[int] = []
    for out in (0.5, 2.0, 5.0, 8.0, 12.0, 60.0, 600.0, 3600.0):
        task._downloaded = int(header + bitrate * out)
        task._out_time = out
        last = task._persist_estimate(last)
        fresh = db.get_job(job.id)
        assert fresh is not None
        if fresh.total_size:
            seen.append(fresh.total_size)

    assert seen, "an estimate should eventually be published"
    # Every published estimate is within 20% of the true ~2 GB - never 500 GB.
    assert all(1.6e9 <= t <= 2.4e9 for t in seen), [t / 1e9 for t in seen]
    db.close()


def test_finalize_survives_a_failing_fsync(
    db: Database, dest: Path, monkeypatch: pytest.MonkeyPatch
):
    """The Windows regression: FlushFileBuffers rejects a read-only handle, the
    raise skipped the COMPLETED write, and a fully-downloaded stream sat at
    "Downloading" forever. The flush is best-effort; completion is not."""
    import os as os_mod

    job = _hls_job(db, "https://x/s.m3u8", dest)
    task = HlsDownload(db, job, ffmpeg_path="ffmpeg")
    part = job.part_path
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"x" * 1024)

    def refuse(fd):
        raise PermissionError(9, "The handle is invalid")  # what Windows raises

    monkeypatch.setattr(os_mod, "fsync", refuse)
    status = task._finalize(part)
    assert status is JobStatus.COMPLETED
    fresh = db.get_job(job.id)
    assert fresh is not None and fresh.status is JobStatus.COMPLETED
    assert (dest / fresh.filename).exists()


def test_tail_stall_keeps_a_fully_muxed_stream(db: Database, dest: Path):
    """A CDN holding a trailing connection open stalls the guard at 99% - but
    when ~all of the playlist's duration is muxed the download is done, and
    discarding 900 MB to retry from scratch would be self-harm."""
    job = _hls_job(db, "https://x/s.m3u8", dest)
    task = HlsDownload(db, job, ffmpeg_path="ffmpeg")
    task._duration = 100.0
    task._out_time = 99.5  # >= 98% muxed
    part = job.part_path
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"x" * 4096)

    assert task._looks_complete(part)
    status = task._finalize(part)
    assert status is JobStatus.COMPLETED

    # And the true stall (nothing near the end muxed) still discards.
    fresh = _hls_job(db, "https://x/s2.m3u8", dest)
    task2 = HlsDownload(db, fresh, ffmpeg_path="ffmpeg")
    task2._duration = 100.0
    task2._out_time = 30.0
    assert not task2._looks_complete(part)


def test_ffmpeg_protocol_whitelist(db: Database, dest: Path):
    """The FFmpeg command confines input protocols so a remote manifest can't
    reach file://, concat: or gopher: (CWE-668 / CWE-918). The whitelist must
    precede every -i, and must not contain 'file'."""
    from app.engines.hls import _INPUT_PROTOCOLS

    job = _hls_job(
        db, "https://cdn.example/master.m3u8", dest, options={"audio_url": "https://cdn/a.m3u8"}
    )
    task = HlsDownload(db, job, ffmpeg_path="ffmpeg")
    command = task._command(job.part_path)

    # Every -i is immediately preceded by the whitelist.
    for i, arg in enumerate(command):
        if arg == "-i":
            assert command[i - 2] == "-protocol_whitelist"
            assert command[i - 1] == _INPUT_PROTOCOLS
    # file: is deliberately absent; the dangerous exotics never appear.
    protocols = set(_INPUT_PROTOCOLS.split(","))
    assert "file" not in protocols
    assert protocols.isdisjoint({"concat", "subfile", "gopher", "pipe", "rtp"})
    # The web essentials are present so real streams still work.
    assert {"http", "https", "tls", "crypto"} <= protocols


def test_command_forwards_browser_headers_to_ffmpeg(db: Database, dest: Path):
    """Cookie/Referer/User-Agent from a browser handoff must reach FFmpeg -
    many CDNs check Referer against the requesting page and refuse the stream
    (or serve an HTML error FFmpeg can't parse) without it. -headers is a
    per-input option like -protocol_whitelist, so it must precede each -i."""
    job = _hls_job(
        db,
        "https://cdn.example/master.m3u8",
        dest,
        options={
            "audio_url": "https://cdn.example/audio.m3u8",
            "http_headers": {"Referer": "https://site.example/watch", "Cookie": "sess=abc123"},
        },
    )
    task = HlsDownload(db, job, ffmpeg_path="ffmpeg")
    command = task._command(job.part_path)

    for i, arg in enumerate(command):
        if arg == "-i":
            assert command[i - 2] == "-headers"
            block = command[i - 1]
            assert "Referer: https://site.example/watch\r\n" in block
            assert "Cookie: sess=abc123\r\n" in block


def test_command_omits_headers_flag_when_none_stored(db: Database, dest: Path):
    """A plain paste (no browser handoff) has no headers to forward - the
    flag must be absent entirely, not sent empty."""
    job = _hls_job(db, "https://cdn.example/master.m3u8", dest)
    task = HlsDownload(db, job, ffmpeg_path="ffmpeg")
    command = task._command(job.part_path)
    assert "-headers" not in command


@pytest.mark.skipif(FFMPEG is None, reason="needs a real ffmpeg")
def test_fetch_playlist_and_ffmpeg_both_send_the_headers(
    server: MediaServer, db: Database, dest: Path, tmp_path: Path
):
    """End to end: a job carrying a browser Referer sends it both when Grabline
    fetches the manifest itself (_fetch_playlist) and when FFmpeg fetches the
    segments - proving the whole chain, not just the command string."""
    files = make_hls(tmp_path / "hls", seconds=1)
    for name, content in files.items():
        content_type = "application/vnd.apple.mpegurl" if name.endswith(".m3u8") else "video/mp2t"
        server.add(f"/gated/{name}", content, content_type=content_type)

    job = _hls_job(
        db,
        server.url("/gated/index.m3u8"),
        dest,
        options={"http_headers": {"Referer": "https://site.example/watch"}},
    )
    status = HlsDownload(db, job, ffmpeg_path=FFMPEG).run()

    assert status is JobStatus.COMPLETED
    manifest_headers = server.received_headers("/gated/index.m3u8")
    assert manifest_headers.get("referer") == "https://site.example/watch"
    # At least one media segment must have carried it too (ffmpeg's own fetch).
    segment_headers = {
        name: server.received_headers(f"/gated/{name}")
        for name in files
        if not name.endswith(".m3u8")
    }
    assert any(h.get("referer") == "https://site.example/watch" for h in segment_headers.values())


@pytest.mark.skipif(FFMPEG is None, reason="needs a real ffmpeg")
def test_native_fetch_saves_a_referer_gated_stream(
    server: MediaServer, db: Database, dest: Path, tmp_path: Path
):
    """The fix for the 'Invalid data found when processing input' failures: the
    manifest AND every segment sit behind a Referer gate (403 without it, as a
    hotlink-protected CDN does). The app fetches them all with its own HTTP
    client, which carries the browser headers, and hands FFmpeg only the local
    files - so the stream saves. Without the header every fetch 403s and the job
    fails cleanly rather than handing FFmpeg an error page to choke on."""
    files = make_hls(tmp_path / "hls", seconds=1)
    gate = {"Referer": "https://site.example/watch"}
    for name, content in files.items():
        content_type = "application/vnd.apple.mpegurl" if name.endswith(".m3u8") else "video/mp2t"
        server.add(f"/gated/{name}", content, content_type=content_type, required_headers=gate)

    with_header = _hls_job(
        db, server.url("/gated/index.m3u8"), dest, options={"http_headers": gate}
    )
    assert HlsDownload(db, with_header, ffmpeg_path=FFMPEG).run() is JobStatus.COMPLETED
    assert (dest / "lecture.mp4").stat().st_size > 0
    # No .gl-part or scratch .hls dir left behind.
    assert not any(p.name.endswith(".hls") for p in dest.iterdir())

    without_header = _hls_job(db, server.url("/gated/index.m3u8"), dest)
    assert HlsDownload(db, without_header, ffmpeg_path=FFMPEG).run() is JobStatus.FAILED


@pytest.mark.skipif(FFMPEG is None, reason="needs a real ffmpeg")
def test_whitelist_blocks_local_file_read(dest: Path, tmp_path: Path):
    """End to end against real FFmpeg: a manifest pointing a segment at a local
    file yields no output (the protocol is refused), while the same command
    shape muxes a normal http stream fine (covered by the reassembly test)."""
    import subprocess

    from app.engines.hls import _INPUT_PROTOCOLS

    assert FFMPEG is not None  # skipif guards this; narrows str | None -> str
    secret = tmp_path / "secret.txt"
    secret.write_text("LOCAL-SECRET\n" * 4)
    manifest = tmp_path / "evil.m3u8"
    manifest.write_text(
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n"
        f"#EXTINF:1.0,\nfile://{secret}\n#EXT-X-ENDLIST\n"
    )
    out = tmp_path / "out.ts"
    # A local manifest needs 'file' just to open the input; add it, and confirm
    # the whitelist still refuses the file:// *segment* inside it.
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            _INPUT_PROTOCOLS + ",file",
            "-i",
            str(manifest),
            "-c",
            "copy",
            "-f",
            "mpegts",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    got = out.read_bytes() if out.exists() else b""
    assert b"LOCAL-SECRET" not in got  # the nested file:// segment was refused
