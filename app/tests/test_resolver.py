from __future__ import annotations

import pytest

from app.core.errors import DownloadError
from app.core.models import JobKind
from app.core.resolver import Resolver
from app.engines.smart import (
    MediaInfo,
    PlaylistEntry,
    PlaylistInfo,
    QualityOption,
    SmartEngine,
)
from app.tests.media_server import MediaServer, payload

FAKE_MEDIA = MediaInfo(
    url="https://tube.example/watch?v=1",
    id="1",
    title="A Video",
    uploader="Someone",
    duration=60.0,
    thumbnail_url=None,
    options=(QualityOption(label="Best", kind="video", format_spec="bv*+ba/b"),),
)

FAKE_PLAYLIST = PlaylistInfo(
    url="https://tube.example/playlist?list=1",
    title="A Playlist",
    uploader="Someone",
    entries=(
        PlaylistEntry(url="https://tube.example/watch?v=1", title="One", duration=60, index=1),
        PlaylistEntry(url="https://tube.example/watch?v=2", title="Two", duration=90, index=2),
    ),
)


class FakeSmart(SmartEngine):
    def __init__(
        self,
        *,
        match: bool,
        error: str | None = None,
        playlist: PlaylistInfo | None = None,
        generic: MediaInfo | PlaylistInfo | None = None,
    ) -> None:
        super().__init__()
        self._match = match
        self._error = error
        self._playlist = playlist
        self._generic = generic
        self.last_headers: dict[str, str] | None = None

    def matches(self, url: str) -> bool:
        return self._match

    def inspect(self, url: str, **kwargs) -> MediaInfo | PlaylistInfo:
        self.last_headers = kwargs.get("headers")
        if kwargs.get("force_generic"):
            # Model the generic scraper: nothing found unless a fake was given.
            if self._generic is None:
                raise DownloadError("Unsupported URL")
            return self._generic
        if self._error:
            raise DownloadError(self._error)
        return self._playlist if self._playlist is not None else FAKE_MEDIA


def test_direct_file_routes_to_segmenter(server: MediaServer):
    url = server.add("/file.bin", payload(100_000, 3))
    resolution = Resolver(FakeSmart(match=False)).resolve(url)
    assert resolution.kind is JobKind.DIRECT
    assert resolution.probe is not None
    assert resolution.probe.total_size == 100_000


def test_manifest_suffix_routes_to_hls():
    resolution = Resolver(FakeSmart(match=False)).resolve("https://cdn.example/live/stream.m3u8")
    assert resolution.kind is JobKind.HLS


def test_manifest_content_type_routes_to_hls(server: MediaServer):
    url = server.add(
        "/manifest",
        b"#EXTM3U\n",
        content_type="application/vnd.apple.mpegurl",
    )
    resolution = Resolver(FakeSmart(match=False)).resolve(url)
    assert resolution.kind is JobKind.HLS


def test_hls_master_playlist_forwards_headers(server: MediaServer):
    """A referer-gated master playlist (many CDNs check this) must still
    yield its variants when the browser handoff's headers are passed through -
    without them the request 403s and the quality picker comes back empty."""
    master = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720\n720p.m3u8\n"
    url = server.add(
        "/gated.m3u8",
        master,
        content_type="application/vnd.apple.mpegurl",
        required_headers={"Referer": "https://site.example/watch"},
    )
    resolver = Resolver(FakeSmart(match=False))

    # Without the header: refused, so no variants (the pre-fix behavior).
    resolution = resolver.resolve(url)
    assert resolution.kind is JobKind.HLS
    assert resolution.variants == ()

    # With it forwarded: the master playlist is read and parsed.
    resolution = resolver.resolve(url, headers={"Referer": "https://site.example/watch"})
    assert resolution.kind is JobKind.HLS
    assert len(resolution.variants) == 1
    assert resolution.variants[0].url.endswith("720p.m3u8")


def test_smart_inspection_forwards_browser_headers():
    """A gated video (yt-dlp path) must analyse with the browser's Referer/
    Cookie/User-Agent, the same headers the HLS and direct paths forward -
    without them the page 403s and the quality panel comes back empty."""
    smart = FakeSmart(match=True)
    headers = {"Referer": "https://site.example/watch", "Cookie": "sess=abc"}
    Resolver(smart).resolve("https://tube.example/watch?v=1", headers=headers)
    assert smart.last_headers == headers


def test_generic_scrape_forwards_browser_headers(server: MediaServer):
    """The last-resort page scrape (force_generic) also needs the handoff's
    headers, so a gated embed page can be reached at all."""
    url = server.add("/page", b"<html></html>", content_type="text/html")
    smart = FakeSmart(match=False, generic=FAKE_MEDIA)
    headers = {"Referer": "https://site.example/watch"}
    Resolver(smart).resolve(url, headers=headers)
    assert smart.last_headers == headers


def test_smart_match_wins(server: MediaServer):
    resolution = Resolver(FakeSmart(match=True)).resolve("https://tube.example/watch?v=1")
    assert resolution.kind is JobKind.SMART
    assert resolution.media is FAKE_MEDIA


def test_playlist_resolution(server: MediaServer):
    resolver = Resolver(FakeSmart(match=True, playlist=FAKE_PLAYLIST))
    resolution = resolver.resolve("https://tube.example/playlist?list=1")
    assert resolution.kind is JobKind.SMART
    assert resolution.playlist is FAKE_PLAYLIST
    assert resolution.media is None


def test_smart_error_is_final_and_friendly():
    resolver = Resolver(FakeSmart(match=True, error="This video is private."))
    resolution = resolver.resolve("https://tube.example/watch?v=1")
    assert resolution.kind is None
    assert resolution.message == "This video is private."


def test_html_page_is_refused_with_guidance(server: MediaServer):
    """A streaming site's page must never be saved as lecture.html (the
    '123movies downloads HTML' bug); the message points at the sniffer."""
    url = server.add("/movie/watch", b"<!doctype html><video src=blob:x>", content_type="text/html")
    resolution = Resolver(FakeSmart(match=False)).resolve(url)
    assert resolution.kind is None
    assert resolution.message is not None and "web page" in resolution.message


def test_generic_scrape_rescues_an_unsupported_page(server: MediaServer):
    """A page no site extractor claims still downloads when the generic
    extractor finds real media in it (the 'other media' path)."""
    url = server.add(
        "/embed/clip", b"<!doctype html><video src=https://x/v.mp4>", content_type="text/html"
    )
    resolution = Resolver(FakeSmart(match=False, generic=FAKE_MEDIA)).resolve(url)
    assert resolution.kind is JobKind.SMART
    assert resolution.media is FAKE_MEDIA


def test_generic_scrape_with_nothing_usable_falls_back_to_refusal(server: MediaServer):
    """When the generic extractor comes up empty, the user sees the plain
    'this is a web page' guidance, not a generic-extractor error."""
    url = server.add("/blog/post", b"<!doctype html><p>no media</p>", content_type="text/html")
    empty = MediaInfo(
        url=url, id="", title="post", uploader=None, duration=None, thumbnail_url=None, options=()
    )
    resolution = Resolver(FakeSmart(match=False, generic=empty)).resolve(url)
    assert resolution.kind is None
    assert resolution.message is not None and "web page" in resolution.message


def test_drm_services_are_refused_by_name():
    resolver = Resolver(FakeSmart(match=False))
    for url, service in (
        ("https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC", "Spotify"),
        ("https://www.netflix.com/watch/81234567", "Netflix"),
        ("https://music.apple.com/us/album/x/12345", "Apple Music"),
        ("https://tidal.com/browse/track/12345", "TIDAL"),
    ):
        resolution = resolver.resolve(url)
        assert resolution.kind is None
        assert resolution.message is not None
        assert service in resolution.message and "DRM" in resolution.message


def test_spotify_podcasts_are_not_drm_blocked():
    """Spotify episodes/shows are plain audio; yt-dlp downloads them."""
    resolver = Resolver(FakeSmart(match=True))
    resolution = resolver.resolve("https://open.spotify.com/episode/abc123")
    assert resolution.kind is JobKind.SMART


def test_soundcloud_is_not_drm_blocked():
    resolver = Resolver(FakeSmart(match=True))
    resolution = resolver.resolve("https://soundcloud.com/artist/track")
    assert resolution.kind is JobKind.SMART


def test_non_http_scheme_refused():
    # ftp/sftp/s3 are cloud schemes now; gopher is a genuinely unsupported one.
    resolution = Resolver(FakeSmart(match=False)).resolve("gopher://host/file")
    assert resolution.kind is None
    assert resolution.message is not None and "http" in resolution.message


def test_unreachable_host_is_friendly():
    resolution = Resolver(FakeSmart(match=False)).resolve("http://127.0.0.1:1/nothing")
    assert resolution.kind is None
    assert resolution.message is not None
    assert "No downloadable media" in resolution.message


@pytest.mark.parametrize("url", ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"])
def test_real_extractor_matching_is_offline(url: str):
    """matches() must recognize big sites without any network traffic."""
    assert SmartEngine().matches(url)
    assert not SmartEngine().matches("https://example.com/some/page.html")
