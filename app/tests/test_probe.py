from __future__ import annotations

import httpx
import pytest

from app.core.errors import DownloadError
from app.core.probe import probe
from app.tests.media_server import MediaServer, payload


@pytest.fixture()
def client():
    with httpx.Client(follow_redirects=True) as httpx_client:
        yield httpx_client


def test_probe_range_server(server: MediaServer, client: httpx.Client):
    url = server.add("/f.bin", payload(100_000, 1))
    result = probe(client, url)
    assert result.resumable
    assert result.total_size == 100_000
    assert result.etag


def test_probe_403_gives_actionable_message(server: MediaServer, client: httpx.Client):
    """A real access wall (403 even with browser-like defaults) must tell the
    user the one thing that fixes it - their browser session - not just echo a
    bare status code."""
    url = server.add("/gated.bin", payload(1000, 3), required_headers={"Cookie": "s=1"})
    with pytest.raises(DownloadError) as exc:
        probe(client, url)
    message = str(exc.value).lower()
    assert "403" in message and "signed in" in message


def test_probe_server_without_ranges(server: MediaServer, client: httpx.Client):
    url = server.add("/plain.bin", payload(50_000, 2), supports_ranges=False)
    result = probe(client, url)
    assert not result.resumable
    assert result.total_size == 50_000


def test_probe_follows_redirects(server: MediaServer, client: httpx.Client):
    server.add("/real.bin", payload(10_000, 3))
    url = server.add("/go", redirect_to="/real.bin")
    result = probe(client, url)
    assert result.resumable
    assert result.total_size == 10_000
    assert result.final_url.endswith("/real.bin")


def test_probe_reads_content_disposition(server: MediaServer, client: httpx.Client):
    url = server.add(
        "/dl",
        payload(1_000, 4),
        content_disposition='attachment; filename="My Video.mp4"',
    )
    result = probe(client, url)
    assert result.filename == "My Video.mp4"


def test_probe_forwards_extra_headers(server: MediaServer, client: httpx.Client):
    url = server.add(
        "/gated.bin",
        payload(4_000, 7),
        required_headers={"Cookie": "session=abc"},
    )
    result = probe(client, url, {"Cookie": "session=abc"})
    assert result.total_size == 4_000
    assert server.received_headers("/gated.bin")["cookie"] == "session=abc"


def test_probe_without_cookie_is_refused(server: MediaServer, client: httpx.Client):
    url = server.add("/gated.bin", payload(4_000, 8), required_headers={"Cookie": "session=abc"})
    with pytest.raises(DownloadError, match="HTTP 403"):
        probe(client, url)


def test_probe_http_error_is_friendly(server: MediaServer, client: httpx.Client):
    with pytest.raises(DownloadError, match="HTTP 404"):
        probe(client, server.url("/missing"))


def test_probe_unreachable_server_is_friendly(client: httpx.Client):
    with pytest.raises(DownloadError, match="could not reach server"):
        probe(client, "http://127.0.0.1:1/nothing")


@pytest.mark.parametrize(
    "header, expected",
    [
        ("attachment; filename=\"pptx\"; filename*=UTF-8''Lecture%201.pptx", "Lecture 1.pptx"),
        ("attachment; filename*=UTF-8''Lecture%201.pptx; filename=\"pptx\"", "Lecture 1.pptx"),
        (
            "attachment; filename=\"fallback.pptx\"; filename*=UTF-8''%E8%AF%BE%E7%A8%8B.pptx",
            "课程.pptx",
        ),
        ('attachment; filename="Lecture 1.pptx"', "Lecture 1.pptx"),
    ],
)
def test_probe_prefers_encoded_lms_filename(server, client, header, expected):
    url = server.add("/resource", b"presentation", content_disposition=header)
    assert probe(client, url).filename == expected


@pytest.mark.parametrize("status", [400, 403, 405, 412, 428, 501])
def test_range_rejection_falls_back_without_reading_file(status):
    calls = []

    class UnreadBody(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("probe must not download the full file")
            yield b""

    def respond(request):
        calls.append(request)
        if request.headers.get("range"):
            return httpx.Response(status)
        return httpx.Response(200, headers={"Content-Length": "12345"}, stream=UnreadBody())

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = probe(client, "https://example.com/file.zip", {"Referer": "https://example.com/"})
    assert not result.resumable and result.total_size == 12345
    assert len(calls) == 2
    assert calls[1].headers["referer"] == "https://example.com/"
    assert "range" not in calls[1].headers


def test_probe_does_not_retry_real_auth_failure_forever():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(403)

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as client,
        pytest.raises(DownloadError, match="HTTP 403"),
    ):
        probe(client, "https://example.com/private.zip")
    assert len(calls) == 2
