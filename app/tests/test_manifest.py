"""Manifest parsing (F2.1): master-playlist variants, audio groups, durations."""

from __future__ import annotations

from app.core.models import JobKind
from app.core.resolver import Resolver
from app.engines.manifest import (
    parse_attributes,
    parse_master_playlist,
    playlist_duration,
)
from app.tests.media_server import MediaServer

MASTER = """\
#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",DEFAULT=YES,URI="audio/en.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="French",URI="audio/fr.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,CODECS="avc1.64002a,mp4a.40.2",AUDIO="aud"
video/1080p.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720,AUDIO="aud"
video/720p.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720,AUDIO="aud"
video/720p-low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=600000
https://cdn.example.com/mobile.m3u8
"""

MEDIA = """\
#EXTM3U
#EXT-X-TARGETDURATION:2
#EXTINF:2.000,
seg000.ts
#EXTINF:2.000,
seg001.ts
#EXTINF:1.500,
seg002.ts
#EXT-X-ENDLIST
"""


def test_parse_attributes_handles_quoted_commas():
    attrs = parse_attributes('#EXT-X-STREAM-INF:BANDWIDTH=5000000,CODECS="avc1,mp4a",AUDIO="aud"')
    assert attrs["BANDWIDTH"] == "5000000"
    assert attrs["CODECS"] == "avc1,mp4a"
    assert attrs["AUDIO"] == "aud"


def test_parse_master_playlist_variants_best_first():
    variants = parse_master_playlist(MASTER, "https://example.com/streams/master.m3u8")
    labels = [v.label for v in variants]
    # 720p deduped to the higher-bandwidth rendition; heightless one labelled by rate.
    assert labels == ["1080p", "720p", "0.6 Mbps"]
    assert variants[0].url == "https://example.com/streams/video/1080p.m3u8"
    assert variants[0].bandwidth == 5000000
    assert variants[1].bandwidth == 2500000
    assert variants[2].url == "https://cdn.example.com/mobile.m3u8"


def test_audio_group_default_rendition_wins():
    variants = parse_master_playlist(MASTER, "https://example.com/streams/master.m3u8")
    assert variants[0].audio_url == "https://example.com/streams/audio/en.m3u8"
    assert variants[2].audio_url is None  # no AUDIO attribute on that variant


def test_media_playlist_is_not_a_master():
    assert parse_master_playlist(MEDIA, "https://example.com/index.m3u8") == ()


def test_playlist_duration_sums_extinf():
    assert playlist_duration(MEDIA) == 5.5
    assert playlist_duration("#EXTM3U\n") is None


def test_resolver_attaches_variants(server: MediaServer):
    server.add(
        "/master.m3u8",
        MASTER.encode(),
        content_type="application/vnd.apple.mpegurl",
    )
    resolution = Resolver().resolve(server.url("/master.m3u8"))
    assert resolution.kind is JobKind.HLS
    assert [v.label for v in resolution.variants] == ["1080p", "720p", "0.6 Mbps"]
    # Relative URIs resolve against the manifest's own URL.
    assert resolution.variants[0].url == server.url("/video/1080p.m3u8")


def test_resolver_media_playlist_has_no_variants(server: MediaServer):
    server.add("/index.m3u8", MEDIA.encode(), content_type="application/vnd.apple.mpegurl")
    resolution = Resolver().resolve(server.url("/index.m3u8"))
    assert resolution.kind is JobKind.HLS
    assert resolution.variants == ()
