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
