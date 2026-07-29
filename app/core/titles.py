"""Instant video titles via oEmbed - the lightweight endpoint YouTube and
Vimeo serve in ~200ms, so a just-queued download can show its real name
right away instead of waiting for the full yt-dlp extraction.
"""

from __future__ import annotations

import logging
from urllib.parse import quote, urlsplit

import httpx

from app.core import net

log = logging.getLogger(__name__)

_OEMBED = {
    "youtube": "https://www.youtube.com/oembed?format=json&url={url}",
    "vimeo": "https://vimeo.com/api/oembed.json?url={url}",
}


def _endpoint(url: str) -> str | None:
    host = (urlsplit(url).hostname or "").lower()
    if host in ("youtu.be", "youtube.com", "youtube-nocookie.com") or host.endswith(
        (".youtube.com", ".youtube-nocookie.com")
    ):
        return _OEMBED["youtube"].format(url=quote(url, safe=""))
    if host == "vimeo.com" or host.endswith(".vimeo.com"):
        return _OEMBED["vimeo"].format(url=quote(url, safe=""))
    return None


def quick_title(url: str, proxy: str | None = None) -> str | None:
    """The video's real title in one tiny request, or None (unsupported site,
    offline, private video, …). Never raises."""
    endpoint = _endpoint(url)
    if endpoint is None:
        return None
    try:
        with net.build_client(proxy=proxy, follow_redirects=True, timeout=6) as client:
            response = client.get(endpoint)
        if response.status_code != 200:
            return None
        title = str(response.json().get("title") or "").strip()
        return title or None
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("quick title lookup failed for %s: %s", url, exc)
        return None
