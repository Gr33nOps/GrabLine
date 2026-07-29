"""The Download Inspector: CDN detection, a live probe against the local
media server (headers, cookies, redirect chain, IP, MIME, response time), and
the TLS certificate parsing.
"""

from __future__ import annotations

import httpx

from app.core import inspector
from app.core.inspector import InspectionReport, detect_cdn, inspect_url, tls_info
from app.tests.media_server import MediaServer, payload

# ------------------------------------------------------------ CDN detection


def _headers(**pairs: str) -> httpx.Headers:
    return httpx.Headers(list(pairs.items()))


def test_detect_cdn_by_signature():
    assert detect_cdn(_headers(**{"cf-ray": "abc123"})) == "Cloudflare"
    assert detect_cdn(_headers(server="cloudflare")) == "Cloudflare"
    assert detect_cdn(_headers(**{"x-amz-cf-id": "z"})) == "Amazon CloudFront"
    assert detect_cdn(_headers(**{"x-served-by": "cache-lhr"})) == "Fastly"
    assert detect_cdn(_headers(**{"x-akamai-request-id": "1"})) == "Akamai"
    assert detect_cdn(_headers(server="nginx")) == ""
    assert detect_cdn(_headers(via="1.1 varnish")).startswith("via")


# ------------------------------------------------------------- TLS parsing


def test_name_from_cert_prefers_common_name():
    subject = ((("countryName", "US"),), (("commonName", "example.com"),))
    assert inspector._name_from_cert(subject) == "example.com"
    issuer = ((("organizationName", "Lets Encrypt"),),)
    assert inspector._name_from_cert(issuer) == "Lets Encrypt"
    assert inspector._name_from_cert(None) == ""


def test_tls_info_is_none_for_unreachable():
    assert tls_info("127.0.0.1", 1) is None  # nothing listening -> no crash


# ------------------------------------------------------------- live probe


def test_inspect_reports_headers_cookies_and_mime(server: MediaServer):
    url = server.add(
        "/file.bin",
        payload(20_000, 1),
        content_type="application/octet-stream",
        extra_headers=(
            ("Server", "nginx/1.25"),
            ("Set-Cookie", "session=abc; Path=/"),
            ("Set-Cookie", "theme=dark"),
        ),
    )
    report = inspect_url(url)
    assert report.reachable
    assert report.status in (200, 206)
    assert report.mime_type == "application/octet-stream"
    assert "nginx/1.25" in report.server
    assert report.response_ms is not None and report.response_ms >= 0
    header_names = {name.lower() for name, _ in report.headers}
    assert "content-type" in header_names
    assert "session=abc; Path=/" in report.cookies
    assert "theme=dark" in report.cookies
    assert report.ip_addresses  # 127.0.0.1 resolves


def test_inspect_captures_redirect_chain(server: MediaServer):
    target = server.add("/real.bin", payload(5_000, 2))
    start = server.add("/go", b"", redirect_to=target)
    report = inspect_url(start)
    assert report.final_url == target
    assert report.redirect_chain
    assert report.redirect_chain[0][0] == 302
    assert report.redirect_chain[0][1].endswith("/go")


def test_inspect_unreachable_is_a_report_not_an_exception():
    report = inspect_url("http://127.0.0.1:1/nothing")
    assert isinstance(report, InspectionReport)
    assert report.reachable is False
    assert report.error


def test_http_url_has_no_tls_section(server: MediaServer):
    url = server.add("/plain.bin", payload(1_000, 3))
    assert inspect_url(url).tls is None  # http, not https
