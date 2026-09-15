"""Regression tests for the code/security review.

Each test here fails against the behaviour that was found and passes against
the fix. Where a local server can reproduce the real failure it does, rather
than asserting on a mock: a cookie leak and a corrupted resume are both things
you can only really prove by watching what crosses a socket.
"""

from __future__ import annotations

import ssl
from pathlib import Path

import pytest

from app.core import net
from app.core.models import JobStatus
from app.db.database import Database
from app.tests.conftest import sha256_file
from app.tests.media_server import MediaServer, payload, sha256

# ------------------------------------------------ #3 HLS credential scoping


def test_credentials_are_dropped_for_another_origin():
    headers = {
        "Cookie": "session=secret",
        "Authorization": "Bearer secret",
        "Proxy-Authorization": "Basic secret",
        "Referer": "https://site.example/watch",
        "User-Agent": "Mozilla/5.0",
    }
    origin = "https://site.example/stream.m3u8"

    # A playlist naming a host of its choosing gets none of the user's session.
    off = net.scoped_headers(headers, "https://attacker.example/seg.ts", origin)
    assert "Cookie" not in off
    assert "Authorization" not in off
    assert "Proxy-Authorization" not in off
    # ...but still identifies the request, so legitimate CDNs keep working.
    assert off["Referer"] == "https://site.example/watch"
    assert off["User-Agent"] == "Mozilla/5.0"

    # The approved origin and its subdomains keep everything.
    # A different port on the same host is a different service.
    assert "Cookie" not in net.scoped_headers(headers, "https://site.example:8443/s.ts", origin)

    for same in ("https://site.example/seg.ts", "https://cdn.site.example/seg.ts"):
        assert net.scoped_headers(headers, same, origin)["Cookie"] == "session=secret"


def test_credentials_are_not_downgraded_to_plain_http():
    headers = {"Cookie": "session=secret"}
    origin = "https://site.example/stream.m3u8"
    assert "Cookie" not in net.scoped_headers(headers, "http://site.example/seg.ts", origin)


def test_a_redirect_cannot_carry_the_session_off_origin(server: MediaServer):
    """httpx's own follow_redirects keeps an explicitly-set Cookie across a
    cross-host redirect. stream_scoped re-decides at every hop instead."""
    secret = "session=secret"
    server.add("/landing.ts", payload(64, 1))
    server.add("/bounce.ts", b"", redirect_to="/landing.ts")
    origin = server.url("/bounce.ts")

    with (
        net.build_client(timeout=10) as client,
        net.stream_scoped(client, origin, headers={"Cookie": secret}, origin=origin) as response,
    ):
        response.read()
    # Same host across the hop, so the cookie is legitimately kept.
    assert server.received_headers("/landing.ts").get("cookie") == secret

    # Now the same fetch, but the origin we approved is somewhere else: the
    # redirect target is off-origin and must not see the cookie.
    with (
        net.build_client(timeout=10) as client,
        net.stream_scoped(
            client,
            server.url("/bounce.ts"),
            headers={"Cookie": secret},
            origin="https://approved.example/stream.m3u8",
        ) as response,
    ):
        response.read()
    assert "cookie" not in server.received_headers("/landing.ts")


def test_hls_segment_fetch_does_not_leak_cookies_to_a_foreign_host(
    server: MediaServer, db: Database, dest: Path
):
    """End to end through the engine: a playlist that points a segment at
    another host must not hand that host the page's session."""
    from app.engines.hls import HlsDownload

    foreign = MediaServer()
    foreign.start()
    try:
        foreign.add("/evil.ts", payload(2048, 7))
        server.add("/home.ts", payload(2048, 8))
        playlist = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:4\n"
            f"#EXTINF:4,\n{server.url('/home.ts')}\n"
            f"#EXTINF:4,\n{foreign.url('/evil.ts')}\n"
            "#EXT-X-ENDLIST\n"
        )
        url = server.add("/stream.m3u8", playlist.encode(), content_type="application/x-mpegURL")
        job = db.create_job(
            url,
            str(dest),
            "stream.mp4",
            kind=__import__("app.core.models", fromlist=["JobKind"]).JobKind.HLS,
            options={"http_headers": {"Cookie": "session=secret", "Referer": "https://site/"}},
        )
        task = HlsDownload(db, job, ffmpeg_path=None)
        work = dest / "work"
        work.mkdir()
        _rewritten, downloads = task._localize(playlist, url, "video")
        task._fetch_segments(downloads, work)

        assert server.received_headers("/home.ts").get("cookie") == "session=secret"
        assert "cookie" not in foreign.received_headers("/evil.ts")
        # The Referer still travels, so hotlink-protected CDNs keep working.
        assert foreign.received_headers("/evil.ts").get("referer") == "https://site/"
    finally:
        foreign.stop()


# --------------------------------------- #8 HLS resume across a changed playlist


def test_a_changed_playlist_discards_the_stale_segment_cache(
    db: Database, dest: Path, tmp_path: Path
):
    """Local names are positions (video-00007.ts), not identities. Reusing them
    after the playlist changed splices two different streams together."""
    from app.core.models import JobKind
    from app.engines.hls import HlsDownload

    job = db.create_job("https://x.test/s.m3u8", str(dest), "s.mp4", kind=JobKind.HLS)
    task = HlsDownload(db, job, ffmpeg_path=None)
    work = tmp_path / "work"
    work.mkdir()

    first = "#EXTM3U\n#EXTINF:4,\nhttps://x.test/a.ts\n#EXT-X-ENDLIST\n"
    task._check_manifest_identity(first, "https://x.test/s.m3u8", work)
    stale = work / "video-00000.ts"
    stale.write_bytes(b"old media")

    # Same playlist again: the cache is still valid and is kept.
    task._check_manifest_identity(first, "https://x.test/s.m3u8", work)
    assert stale.exists()

    # A different playlist: the cached segment is now a different stream.
    second = "#EXTM3U\n#EXTINF:4,\nhttps://x.test/b.ts\n#EXT-X-ENDLIST\n"
    task._check_manifest_identity(second, "https://x.test/s.m3u8", work)
    assert not stale.exists()


# ------------------------------------------------ #4 / #5 WebDAV resume safety


def test_a_server_that_ignores_range_restarts_instead_of_appending(
    server: MediaServer, db: Database, dest: Path
):
    """The corruption case: we ask to resume, the server sends the whole file
    with HTTP 200, and the old code appended it to the partial - producing
    `old partial + complete file`, which passes a size check and is garbage."""
    from app.core.models import JobKind
    from app.engines.cloud import CloudDownload

    data = payload(40_000, 11)
    server.add("/f.bin", data, supports_ranges=False)  # answers 200, ignores Range
    url = server.url("/f.bin").replace("http://", "webdav://")
    job = db.create_job(url, str(dest), "f.bin", kind=JobKind.CLOUD)

    part = Path(job.dest_dir) / (job.filename + ".gl-part")
    part.write_bytes(b"X" * 5_000)  # a partial from a previous session

    assert CloudDownload(db, job).run() is JobStatus.COMPLETED
    written = Path(job.dest_dir) / job.filename
    assert written.stat().st_size == len(data)
    assert sha256_file(written) == sha256(data)


def test_a_416_only_finalises_when_the_totals_actually_agree():
    from app.engines.cloud import _complete_per_content_range

    assert _complete_per_content_range("bytes */1000", 1000) is True
    assert _complete_per_content_range("bytes */1000", 900) is False  # local is short
    assert _complete_per_content_range("bytes */900", 1000) is False  # local is longer
    assert _complete_per_content_range(None, 1000) is False  # proves nothing
    assert _complete_per_content_range("garbage", 1000) is False
    assert _complete_per_content_range("bytes 0-10/1000", 1000) is False


def test_a_206_must_confirm_the_range_it_answers():
    from app.engines.cloud import _range_starts_at

    assert _range_starts_at("bytes 500-999/1000", 500) is True
    assert _range_starts_at("bytes 0-999/1000", 500) is False  # wrong start
    assert _range_starts_at(None, 500) is False
    assert _range_starts_at("bytes junk", 500) is False
    assert _range_starts_at("bytes 500-499/1000", 500) is False  # end before start


# ------------------------------------------------------ #10 WebDAV URL mapping


def test_webdav_urls_keep_their_query_and_port():
    from app.engines.cloud import _webdav_http_url

    assert (
        _webdav_http_url("webdavs://example.com/file.zip?token=ABC")
        == "https://example.com/file.zip?token=ABC"
    )
    assert _webdav_http_url("webdav://example.com:8080/a%20b.zip") == (
        "http://example.com:8080/a%20b.zip"
    )
    # Userinfo is dropped on purpose - it is sent as Basic auth instead.
    assert _webdav_http_url("webdavs://u:p@example.com/f") == "https://example.com/f"
    assert _webdav_http_url("webdavs://example.com/f?a=1&b=2") == "https://example.com/f?a=1&b=2"


# --------------------------------------------------------- #28 redaction safety


@pytest.mark.parametrize(
    "url",
    [
        "socks5://user:pass@host:99999",  # port out of range
        "http://user:pass@host:abc",  # non-numeric port
        "http://user:pass@host:-1",
        "socks5://user:pass@[::1",  # malformed host
    ],
)
def test_redacting_a_malformed_url_never_raises_and_never_leaks(url: str):
    """This runs inside logging and diagnostics paths: if it raises there, the
    error report it was protecting is what breaks."""
    redacted = net.redact_credentials(url)
    assert "pass" not in redacted
    assert "user" not in redacted


def test_redacting_a_normal_proxy_url_is_unchanged_in_behaviour():
    assert net.redact_credentials("socks5://u:p@h:1080") == "socks5://h:1080"
    assert net.redact_credentials("socks5://h:1080") == "socks5://h:1080"
    assert net.redact_credentials("") == ""


# ------------------------------------------- #30 insecure trust does not spread


def test_insecure_trust_is_scoped_to_the_approved_host():
    """ "Ignore the certificate for this download" must not also ignore it for
    wherever a redirect points."""
    client = net.build_client(insecure=True, trusted_host="nas.local")
    try:
        modes = {
            getattr(pattern, "pattern", str(pattern)): _verify_mode(transport)
            for pattern, transport in client._mounts.items()
        }
        assert any("nas.local" in p and m == ssl.CERT_NONE for p, m in modes.items()), (
            f"the approved host is not exempt: {modes}"
        )
        # Everything else still verifies.
        assert modes["https://"] == ssl.CERT_REQUIRED, f"a redirect target is not verified: {modes}"
    finally:
        client.close()


def _verify_mode(transport: object) -> int:
    pool = getattr(transport, "_pool", None)
    context = getattr(pool, "_ssl_context", None)
    return int(getattr(context, "verify_mode", -1))


# ------------------------------------------------------- #15 updater architecture


@pytest.mark.parametrize(
    ("machine", "expected"),
    [
        ("arm64", "Grabline-1.0.0-applesilicon.dmg"),
        ("x86_64", "Grabline-1.0.0-intel.dmg"),
    ],
)
def test_the_updater_picks_the_dmg_for_this_cpu(machine: str, expected: str):
    from app.core.update import _asset_matches

    names = ["Grabline-1.0.0-applesilicon.dmg", "Grabline-1.0.0-intel.dmg"]
    picked = [n for n in names if _asset_matches(n, "darwin", machine)]
    assert picked == [expected]


def test_the_updater_never_offers_an_x86_build_to_an_arm_machine():
    from app.core.update import _asset_matches

    assert _asset_matches("Grabline-1.0.0-x86_64.AppImage", "linux", "arm64") is False
    assert _asset_matches("grabline_1.0.0_amd64.deb", "linux", "arm64") is False
    assert _asset_matches("Grabline-1.0.0-aarch64.AppImage", "linux", "arm64") is True


def test_an_asset_without_an_architecture_tag_is_still_offered():
    """Single-architecture releases are normal; refusing them would mean never
    updating at all."""
    from app.core.update import _asset_matches

    assert _asset_matches("Grabline-Setup-1.0.0.exe", "win32", "x86_64") is True
    assert _asset_matches("Grabline-Setup-1.0.0.exe", "win32", "arm64") is True


# --------------------------------------------- #13 handoff credentials cleanup


def test_claiming_a_handoff_removes_its_credentials_from_disk(db: Database):
    """A handoff's headers are live session credentials. Once read they have no
    further purpose, and how long they sit in SQLite is a choice."""
    db.add_handoff(
        url="https://site.example/file.bin",
        page_url="https://site.example/",
        page_title="t",
        source="extension",
        headers={"Cookie": "session=secret", "Authorization": "Bearer secret"},
    )
    claimed = db.claim_handoffs()
    assert claimed and claimed[0].headers["Cookie"] == "session=secret"

    with db._lock:
        rows = db._conn.execute("SELECT COUNT(*) AS n FROM handoffs").fetchone()
    assert rows["n"] == 0, "the claimed handoff (and its cookies) stayed on disk"
    assert db.claim_handoffs() == []


# ---------------------------------------------- #19 sparse files vs disk space


def test_disk_space_needed_is_measured_from_progress_not_apparent_size(db: Database, dest: Path):
    """A preallocated part file is sparse: its apparent size is the full length
    from the moment it exists. Subtracting that said "0 bytes needed" for a
    download that had written nothing."""
    from app.core.downloader import SegmentedDownload

    job = db.create_job("http://x.test/big.bin", str(dest), "big.bin")
    job.total_size = 10_000_000
    task = SegmentedDownload(db, job, connections=1)

    part = dest / "big.bin.gl-part"
    with open(part, "wb") as handle:  # sparse: apparent size 10 MB, nothing written
        handle.truncate(10_000_000)
    assert part.stat().st_size == 10_000_000

    # Nothing is recorded as written, so the whole file is still to come -
    # on every platform. (Windows has no st_blocks to ask about the real
    # allocation, so anything derived from the part file's own size is wrong
    # there too; only recorded progress is trustworthy.)
    assert task._bytes_still_needed(part, 10_000_000) == 10_000_000

    # Once segments carry progress, that is what counts.
    from app.core.models import Segment

    task._segments = [
        Segment(id=1, job_id=job.id, index=0, start=0, end=9_999_999, downloaded=4_000_000)
    ]
    assert task._bytes_still_needed(part, 10_000_000) == 6_000_000


# ----------------------------------------- #12 cloud resume and remote identity


def test_a_changed_remote_file_restarts_instead_of_joining_two_versions(db: Database, dest: Path):
    """A .part is only a valid head of the file it was started against.
    Resuming it against a replaced remote object splices two files together."""
    from app.core.models import JobKind
    from app.engines.cloud import _IDENTITY_OPTION, CloudDownload

    job = db.create_job("sftp://h/f.bin", str(dest), "f.bin", kind=JobKind.CLOUD)
    task = CloudDownload(db, job)
    part = job.part_path
    part.write_bytes(b"A" * 100)

    # First sighting records what the partial belongs to and keeps it.
    _p, offset = task._sink("sftp:1000:1700000000")
    assert offset == 100
    assert db.get_job(job.id).options[_IDENTITY_OPTION] == "sftp:1000:1700000000"  # type: ignore[union-attr]

    # The same object again: the resume is still valid.
    part.write_bytes(b"A" * 100)
    _p, offset = task._sink("sftp:1000:1700000000")
    assert offset == 100

    # A different mtime at the same size - the exact case a size check misses.
    part.write_bytes(b"A" * 100)
    _p, offset = task._sink("sftp:1000:1800000000")
    assert offset == 0, "resumed a partial against a different remote file"
    assert not part.exists()


def test_a_first_run_with_no_recorded_identity_still_resumes(db: Database, dest: Path):
    """Databases from before this existed must keep resuming, not restart."""
    from app.core.models import JobKind
    from app.engines.cloud import CloudDownload

    job = db.create_job("sftp://h/f.bin", str(dest), "f.bin", kind=JobKind.CLOUD)
    job.part_path.write_bytes(b"A" * 100)
    _p, offset = CloudDownload(db, job)._sink("sftp:1000:1700000000")
    assert offset == 100


# ------------------------------------------- #9 remotely-chosen URLs are bounded


def test_a_playlist_cannot_point_grabline_at_cloud_metadata(db: Database, dest: Path):
    """A remote document choosing 169.254.169.254 is never legitimate. A NAS or
    a LAN box chosen by the user is, and stays allowed."""
    from app.core.models import JobKind
    from app.engines.hls import HlsDownload

    job = db.create_job("https://cdn.example/s.m3u8", str(dest), "s.mp4", kind=JobKind.HLS)
    task = HlsDownload(db, job, ffmpeg_path=None)

    hostile = "#EXTM3U\n#EXTINF:4,\nhttp://169.254.169.254/latest/meta-data/\n#EXT-X-ENDLIST\n"
    with pytest.raises(ValueError, match="link-local"):
        task._localize(hostile, "https://cdn.example/s.m3u8", "video")

    # Private/LAN addresses are deliberately still allowed - self-hosted
    # streams are a normal thing to download.
    lan = "#EXTM3U\n#EXTINF:4,\nhttp://192.168.1.10/seg.ts\n#EXT-X-ENDLIST\n"
    _rewritten, downloads = task._localize(lan, "https://cdn.example/s.m3u8", "video")
    assert downloads and downloads[0][0] == "http://192.168.1.10/seg.ts"


def test_link_local_detection():
    assert net.is_link_local("169.254.169.254") is True
    assert net.is_link_local("fe80::1") is True
    assert net.is_link_local("192.168.1.1") is False  # a LAN box is fine
    assert net.is_link_local("127.0.0.1") is False  # so is localhost
    assert net.is_link_local("example.com") is False  # a name, not an address
