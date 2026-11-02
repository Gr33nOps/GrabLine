"""Browser credentials must stay with the URL whose session they came from."""

from pathlib import Path

import httpx
import pytest

from app.core import net
from app.core.downloader import SegmentedDownload
from app.core.models import JobStatus
from app.db.database import Database
from app.tests.media_server import MediaServer, payload


@pytest.mark.parametrize("ranges", [True, False])
@pytest.mark.parametrize("cached", [True, False])
def test_direct_redirect_does_not_reapply_source_credentials(
    server: MediaServer, db: Database, dest: Path, ranges: bool, cached: bool
):
    data = payload(600_000, 31)
    target = server.add("/target.bin", data, supports_ranges=ranges).replace(
        "127.0.0.1", "localhost"
    )
    source = server.add("/source", redirect_to=target)
    job = db.create_job(source, str(dest), "target.bin")
    if cached:
        job.final_url = target
        job.total_size = len(data)
        job.resumable = ranges
        db.update_job_probe(job)
    headers = {
        "cOoKiE": "CVP_TEST=synthetic",
        "aUtHoRiZaTiOn": "Bearer synthetic",
        "pRoXy-AuThOrIzAtIoN": "Basic synthetic",
        "Referer": "https://page.example/watch",
        "User-Agent": "GrabLineScopeTest/1.0",
    }
    assert SegmentedDownload(db, job, connections=2, headers=headers).run() is JobStatus.COMPLETED
    assert (dest / "target.bin").read_bytes() == data
    sent = server.received_headers("/target.bin")
    assert not {"cookie", "authorization", "proxy-authorization"}.intersection(sent)
    assert sent["referer"] == headers["Referer"]
    assert sent["user-agent"] == headers["User-Agent"]
    if not cached:
        assert server.received_headers("/source")["cookie"] == headers["cOoKiE"]


def test_same_host_redirect_preserves_required_cookie(
    server: MediaServer, db: Database, dest: Path
):
    data = payload(1024, 32)
    target = server.add("/gated.bin", data, required_headers={"Cookie": "CVP_TEST=synthetic"})
    source = server.add("/source", redirect_to=target)
    job = db.create_job(source, str(dest), "gated.bin")
    status = SegmentedDownload(db, job, headers={"Cookie": "CVP_TEST=synthetic"}).run()
    assert status is JobStatus.COMPLETED
    assert (dest / "gated.bin").read_bytes() == data


@pytest.mark.parametrize("resume", [True, False])
def test_resume_and_expired_destination_retry_keep_cookie_on_source(
    server: MediaServer, db: Database, dest: Path, resume: bool
):
    data = payload(600_000, 33)
    target = server.add("/fresh.bin", data).replace("127.0.0.1", "localhost")
    source = server.add(
        "/source", redirect_to=target, required_headers={"Cookie": "CVP_TEST=synthetic"}
    )
    expired = server.add("/expired.bin", fail_status=403, fail_until=99).replace(
        "127.0.0.1", "localhost"
    )
    job = db.create_job(source, str(dest), "fresh.bin")
    job.final_url = target if resume else expired
    job.resumable = True
    job.total_size = len(data)
    job.etag = server.resources["/fresh.bin"].etag
    db.update_job_probe(job)
    if resume:
        segments = db.replace_segments(job.id, [(0, len(data) - 1)])
        job.part_path.write_bytes(data[:4096])
        db.update_segment_progress({segments[0].id: 4096})
    status = SegmentedDownload(
        db, job, connections=1, retry_backoff=0.01, headers={"Cookie": "CVP_TEST=synthetic"}
    ).run()
    assert status is JobStatus.COMPLETED
    assert (dest / "fresh.bin").read_bytes() == data
    assert server.received_headers("/source")["cookie"] == "CVP_TEST=synthetic"
    assert "cookie" not in server.received_headers("/fresh.bin")
    assert "cookie" not in server.received_headers("/expired.bin")


@pytest.mark.parametrize(
    "target",
    [
        "https://foreign.example/file",
        "https://child.source.example/file",
        "https://source.example:8443/file",
        "http://source.example/file",
    ],
)
def test_final_request_hook_filters_defaults_auth_cookies_and_redirects(target: str):
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": target})
        return httpx.Response(200, content=b"ok")

    origin = "https://source.example/start"
    with net.build_client(
        credential_origin=origin,
        follow_redirects=True,
        trust_env=False,
        transport=httpx.MockTransport(respond),
        headers={"Cookie": "CVP_TEST=synthetic", "Proxy-Authorization": "Basic synthetic"},
        auth=("synthetic", "synthetic"),
    ) as client:
        assert client.get(origin).content == b"ok"
        assert client.get(target).content == b"ok"
    assert seen[0].headers["cookie"] == "CVP_TEST=synthetic"
    for request in seen[1:]:
        assert not net.CREDENTIAL_HEADERS.intersection(request.headers)


def test_scoped_stream_filters_merged_defaults_and_extra_headers():
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"ok")

    with (
        net.build_client(
            transport=httpx.MockTransport(respond),
            trust_env=False,
            headers={"Cookie": "CVP_TEST=synthetic"},
            auth=("synthetic", "synthetic"),
        ) as client,
        net.stream_scoped(
            client,
            "https://foreign.example/file",
            origin="https://source.example/file",
            headers=None,
            extra_headers={"pRoXy-AuThOrIzAtIoN": "Basic synthetic"},
        ) as response,
    ):
        assert response.read() == b"ok"
    assert not net.CREDENTIAL_HEADERS.intersection(seen[0].headers)


def test_redirect_destination_can_use_its_own_session_cookie():
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "source.example":
            return httpx.Response(302, headers={"Location": "https://cdn.example/set"})
        if request.url.path == "/set":
            return httpx.Response(
                302,
                headers={"Location": "/file", "Set-Cookie": "CDN_TEST=synthetic; Path=/; Secure"},
            )
        assert request.headers.get("cookie") == "CDN_TEST=synthetic"
        return httpx.Response(200, content=b"file")

    with net.build_client(
        credential_origin="https://source.example/start",
        follow_redirects=True,
        headers={"Cookie": "CVP_TEST=synthetic"},
        transport=httpx.MockTransport(respond),
    ) as client:
        assert client.get("https://source.example/start").content == b"file"
    assert "cookie" not in seen[1].headers
    assert seen[2].headers["cookie"] == "CDN_TEST=synthetic"


@pytest.mark.parametrize("same_host", [True, False])
def test_mirror_rotation_keeps_only_authorized_credentials(
    db: Database, dest: Path, same_host: bool
):
    from app.core.manager import DownloadManager

    origin = "https://source.example/file"
    mirror = "https://source.example/mirror" if same_host else "https://foreign.example/mirror"
    manager = DownloadManager(db, max_concurrent=0)
    try:
        job = manager.add_url(
            origin,
            dest,
            headers={"Cookie": "CVP_TEST=synthetic", "Referer": origin},
            mirrors=[mirror],
        )
        assert manager._try_mirror(job.id)
        fresh = db.get_job(job.id)
        assert fresh is not None
        assert ("Cookie" in fresh.options["http_headers"]) is same_host
        assert fresh.options["http_headers"]["Referer"] == origin
    finally:
        manager.shutdown()


def test_drive_share_transform_drops_source_cookie(db: Database, dest: Path):
    from app.core.manager import DownloadManager

    manager = DownloadManager(db, max_concurrent=0)
    try:
        job = manager.add_url(
            "https://drive.google.com/file/d/synthetic/view",
            dest,
            headers={"Cookie": "CVP_TEST=synthetic", "User-Agent": "ScopeTest"},
        )
        assert job.url.startswith("https://drive.usercontent.google.com/")
        assert job.options["http_headers"] == {"User-Agent": "ScopeTest"}
    finally:
        manager.shutdown()


def test_smart_cookie_jar_is_host_only_and_never_a_global_header():
    import yt_dlp

    from app.engines.smart import _handoff_headers, _seed_handoff_cookies

    raw = {
        "cOoKiE": "CVP_TEST=synthetic",
        "aUtHoRiZaTiOn": "Bearer synthetic",
        "Referer": "https://source.example/",
    }
    forwarded = _handoff_headers(raw)
    assert forwarded == {"Referer": "https://source.example/"}
    with yt_dlp.YoutubeDL({"quiet": True, "http_headers": forwarded}) as ydl:
        _seed_handoff_cookies(ydl, raw, "https://source.example/video")
        assert (
            ydl.cookiejar.get_cookie_header("https://source.example/next") == "CVP_TEST=synthetic"
        )
        for url in (
            "https://foreign.example/next",
            "https://child.source.example/next",
            "http://source.example/next",
            "https://source.example:8443/next",
        ):
            assert not ydl.cookiejar.get_cookie_header(url)
        # External FFmpeg and metadata serialization get no cookies; neither
        # API preserves the browser's host-only/port/scheme restrictions.
        assert ydl.cookiejar.get_cookies_for_url("https://source.example/video") == []
        info = {"url": "https://source.example/video", "http_headers": {}}
        ydl._calc_headers(info)
        assert "cookies" not in info
        # Old cached metadata must not broaden the original cookie's scope.
        info["cookies"] = "CVP_TEST=synthetic; Domain=source.example; Path=/; Secure"
        ydl._calc_headers(info, load_cookies=True)
        assert not ydl.cookiejar.get_cookie_header("https://child.source.example/video")
        assert not ydl.cookiejar.get_cookie_header("https://source.example:8443/video")


def test_smart_browser_cookies_survive_same_host_but_not_foreign_redirect(server: MediaServer):
    import yt_dlp

    from app.engines.smart import _seed_handoff_cookies

    source = server.add("/source.bin", b"source", required_headers={"Cookie": "CVP_TEST=synthetic"})
    foreign = server.add("/foreign.bin", b"foreign").replace("127.0.0.1", "localhost")
    bounce = server.add("/bounce", redirect_to=foreign)
    with yt_dlp.YoutubeDL({"quiet": True, "proxy": ""}) as ydl:
        _seed_handoff_cookies(ydl, {"Cookie": "CVP_TEST=synthetic"}, source)
        assert "Requests" not in ydl._request_director.handlers
        assert "CurlCFFI" not in ydl._request_director.handlers
        assert "Urllib" in ydl._request_director.handlers
        with ydl.urlopen(source) as response:
            assert response.read() == b"source"
        with ydl.urlopen(bounce) as response:
            assert response.read() == b"foreign"
    assert "cookie" not in server.received_headers("/foreign.bin")


@pytest.mark.parametrize("manifest", [True, False])
def test_resolver_redirect_scopes_credentials(server: MediaServer, manifest: bool):
    from app.core.resolver import Resolver
    from app.tests.test_resolver import FakeSmart

    body = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nvideo.m3u8\n" if manifest else b"file"
    target = server.add("/landing", body).replace("127.0.0.1", "localhost")
    source = server.add(
        "/start.m3u8" if manifest else "/start.bin",
        redirect_to=target,
        required_headers={"Cookie": "CVP_TEST=synthetic"},
    )
    result = Resolver(FakeSmart(match=False)).resolve(
        source,
        headers={
            "Cookie": "CVP_TEST=synthetic",
            "Proxy-Authorization": "Basic synthetic",
        },
    )
    assert result.kind is not None
    assert server.request_count("/landing") > 0
    assert not net.CREDENTIAL_HEADERS.intersection(server.received_headers("/landing"))


def test_resolve_thread_drops_credentials_after_url_rewrite(db: Database):
    from app.core.models import JobKind
    from app.core.resolver import Resolution, Resolver
    from app.core.settings import Settings
    from app.ui.work_threads import ResolveThread

    class Rewriter(Resolver):
        def resolve(self, url, **kwargs):
            return Resolution(url="https://foreign.example/file", kind=JobKind.DIRECT)

    thread = ResolveThread(
        Rewriter(),
        "https://source.example/share",
        Settings(db),
        None,
        headers={"Cookie": "CVP_TEST=synthetic", "User-Agent": "ScopeTest"},
    )
    emitted = []
    thread.resolved.connect(lambda *args: emitted.append(args))
    thread.run()
    assert emitted[0][-1] == {"User-Agent": "ScopeTest"}


def test_cookie_file_keeps_its_session_and_save_destination(tmp_path: Path):
    import yt_dlp

    from app.engines.smart import _seed_handoff_cookies

    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\nsource.example\tFALSE\t/\tTRUE\t0\tFILE_TEST\tsynthetic\n"
    )
    with yt_dlp.YoutubeDL({"quiet": True, "cookiefile": str(cookie_file)}) as ydl:
        _seed_handoff_cookies(
            ydl,
            {"Cookie": "CVP_TEST=ignored"},
            "https://source.example/video",
            has_cookie_source=True,
        )
        assert (
            ydl.cookiejar.get_cookie_header("https://source.example/video") == "FILE_TEST=synthetic"
        )
        assert ydl.cookiejar.get_cookies_for_url("https://source.example/video") == []
    assert "FILE_TEST" in cookie_file.read_text()
