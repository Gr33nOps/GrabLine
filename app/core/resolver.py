"""The resolver: every URL, one route (proposal §5).

1. Smart Engine - does a yt-dlp site extractor recognize it? Full experience.
2. HLS/DASH manifest - FFmpeg reassembly.
3. Direct file - the segmented downloader.
4. Nothing - a friendly message, never a traceback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.core import cloudlinks, net
from app.core.errors import DownloadError
from app.core.models import JobKind
from app.core.probe import ProbeResult, probe
from app.engines import cloud
from app.engines.manifest import HlsVariant, parse_master_playlist
from app.engines.smart import MediaInfo, PlaylistInfo, SmartEngine

_MANIFEST_SUFFIXES = (".m3u8", ".mpd")
_MANIFEST_CONTENT_TYPES = (
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "application/dash+xml",
)
_HLS_CONTENT_TYPES = _MANIFEST_CONTENT_TYPES[:3]  # dash+xml goes to FFmpeg unparsed
_HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")
_HTML_MESSAGE = (
    "This address is a web page, not a downloadable file. If a video plays "
    "on it, let it play for a moment, then use the GrabLine button on the "
    "player or the toolbar popup. GrabLine grabs the stream the page loads."
)

#: Services whose media is DRM-protected end to end. Refused up front with an
#: honest, named message (proposal: no DRM circumvention, clear refusal) -
#: better than the confusing failure yt-dlp or the probe would produce.
_DRM_SERVICES: tuple[tuple[str, str], ...] = (
    (r"(^|\.)netflix\.com$", "Netflix"),
    (r"(^|\.)primevideo\.com$", "Prime Video"),
    (r"(^|\.)disneyplus\.com$", "Disney+"),
    (r"(^|\.)max\.com$", "Max"),
    (r"(^|\.)hulu\.com$", "Hulu"),
    (r"(^|\.)peacocktv\.com$", "Peacock"),
    (r"(^|\.)paramountplus\.com$", "Paramount+"),
    (r"(^|\.)crunchyroll\.com$", "Crunchyroll"),
    (r"(^|\.)spotify\.com$", "Spotify"),
    (r"(^|\.)music\.apple\.com$", "Apple Music"),
    (r"(^|\.)tidal\.com$", "TIDAL"),
    (r"(^|\.)deezer\.com$", "Deezer"),
    (r"^music\.amazon\.", "Amazon Music"),
)
#: Spotify podcasts are not DRM-protected; yt-dlp downloads them fine.
_SPOTIFY_PODCAST_PATHS = re.compile(r"^/(episode|show)/")


def _drm_refusal(url: str) -> str | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    for pattern, service in _DRM_SERVICES:
        if re.search(pattern, host):
            if service == "Spotify" and _SPOTIFY_PODCAST_PATHS.match(parts.path):
                return None
            return (
                f"{service} protects its media with DRM. GrabLine cannot and will "
                "not bypass DRM, so there is nothing it can download here."
            )
    return None


@dataclass(frozen=True)
class Resolution:
    """Where a URL routes. ``kind`` None means: nothing to download, see message."""

    url: str
    kind: JobKind | None
    media: MediaInfo | None = None  # set for SMART single videos
    playlist: PlaylistInfo | None = None  # set for SMART playlists (F1.7)
    probe: ProbeResult | None = None  # set for DIRECT
    message: str | None = None  # set when kind is None
    variants: tuple[HlsVariant, ...] = ()  # set for HLS master playlists (F2.1)


def _hls_variants(
    url: str, proxy: str | None = None, headers: dict[str, str] | None = None
) -> tuple[HlsVariant, ...]:
    """Quality choices from a master playlist; empty for media playlists
    or when the manifest cannot be fetched (FFmpeg reports the real error).

    ``headers`` matters here: many stream CDNs check Referer/Cookie/User-Agent
    against the page that requested them and 403 (or serve an HTML error page)
    without it - the browser handoff already captured them, so they need to
    ride along into the manifest fetch, not just the direct-file probe.
    """
    try:
        with net.build_client(proxy=proxy, follow_redirects=True, http2=True, timeout=10) as client:
            response = client.get(url, headers=headers)
            if response.status_code != 200:
                return ()
            return parse_master_playlist(response.text, str(response.url))
    except httpx.HTTPError:
        return ()


class Resolver:
    def __init__(self, smart: SmartEngine | None = None) -> None:
        self.smart = smart or SmartEngine()

    def resolve(
        self,
        url: str,
        *,
        use_session: bool = False,
        session_browser: str = "chrome",
        proxy: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Resolution:
        url = url.strip()
        scheme = urlsplit(url).scheme.lower()
        if scheme == "magnet":
            return Resolution(url=url, kind=JobKind.TORRENT)
        if scheme in cloud.CLOUD_SCHEMES:
            # ftp/ftps/sftp/scp/s3/webdav go to the cloud protocol engine.
            return Resolution(url=url, kind=JobKind.CLOUD)
        if scheme not in ("http", "https"):
            return Resolution(
                url=url,
                kind=None,
                message=(
                    "GrabLine can download http://, https://, magnet: and cloud "
                    "(ftp/ftps/sftp/scp/s3/webdav) addresses."
                ),
            )
        unsupported = cloudlinks.unsupported_cloud_reason(url)
        if unsupported is not None:
            return Resolution(url=url, kind=None, message=unsupported)
        # A cloud *share* link (Drive/Dropbox/OneDrive/Nextcloud) becomes a
        # plain direct URL and downloads through the normal segmented engine.
        direct = cloudlinks.direct_download_url(url)
        if direct is not None:
            url = direct
            scheme = urlsplit(url).scheme.lower()
        if urlsplit(url).path.lower().endswith(".torrent"):
            # A .torrent link opens as a torrent, not as a small saved file.
            return Resolution(url=url, kind=JobKind.TORRENT)

        refusal = _drm_refusal(url)
        if refusal is not None:
            return Resolution(url=url, kind=None, message=refusal)

        if self.smart.matches(url):
            try:
                inspected = self.smart.inspect(
                    url,
                    use_session=use_session,
                    session_browser=session_browser,
                    proxy=proxy,
                    headers=headers,
                )
            except DownloadError as exc:
                # A site extractor claimed the URL; its verdict is final -
                # falling through to sniffing would just fail less clearly.
                return Resolution(url=url, kind=None, message=str(exc))
            if isinstance(inspected, PlaylistInfo):
                return Resolution(url=url, kind=JobKind.SMART, playlist=inspected)
            return Resolution(url=url, kind=JobKind.SMART, media=inspected)

        path = urlsplit(url).path.lower()
        if path.endswith(_MANIFEST_SUFFIXES):
            variants = _hls_variants(url, proxy, headers) if path.endswith(".m3u8") else ()
            return Resolution(url=url, kind=JobKind.HLS, variants=variants)

        try:
            with net.build_client(
                proxy=proxy,
                follow_redirects=True,
                http2=True,
                timeout=httpx.Timeout(20.0, connect=10.0),
            ) as client:
                result = probe(client, url, headers)
        except DownloadError as exc:
            return Resolution(
                url=url,
                kind=None,
                message=f"No downloadable media was found at this address ({exc}).",
            )
        content_type = (result.content_type or "").split(";")[0].strip().lower()
        if content_type == "application/x-bittorrent":
            return Resolution(url=url, kind=JobKind.TORRENT)
        if content_type in _MANIFEST_CONTENT_TYPES:
            variants = (
                _hls_variants(url, proxy, headers) if content_type in _HLS_CONTENT_TYPES else ()
            )
            return Resolution(url=url, kind=JobKind.HLS, probe=result, variants=variants)
        if content_type in _HTML_CONTENT_TYPES:
            # No site extractor claimed it and it's a web page, not a file. Before
            # giving up, let yt-dlp's generic extractor scrape the page for media
            # (og:video, <video>/<source>, JSON-LD, embedded m3u8) - this is how a
            # video on a site yt-dlp doesn't specifically support still downloads.
            scraped = self._try_generic(
                url,
                use_session=use_session,
                session_browser=session_browser,
                proxy=proxy,
                headers=headers,
            )
            if scraped is not None:
                return scraped
            # Saving a streaming site's page as lecture.html helps nobody.
            return Resolution(url=url, kind=None, message=_HTML_MESSAGE)
        return Resolution(url=url, kind=JobKind.DIRECT, probe=result)

    def _try_generic(
        self,
        url: str,
        *,
        use_session: bool,
        session_browser: str,
        proxy: str | None,
        headers: dict[str, str] | None = None,
    ) -> Resolution | None:
        """Best-effort scrape of an unsupported page. None when nothing usable
        turns up, so the caller falls back to its plain 'this is a web page'
        message rather than surfacing a scary generic-extractor traceback."""
        try:
            inspected = self.smart.inspect(
                url,
                use_session=use_session,
                session_browser=session_browser,
                proxy=proxy,
                force_generic=True,
                headers=headers,
            )
        except DownloadError:
            return None
        if isinstance(inspected, PlaylistInfo):
            return Resolution(url=url, kind=JobKind.SMART, playlist=inspected)
        if inspected.options:
            return Resolution(url=url, kind=JobKind.SMART, media=inspected)
        return None
