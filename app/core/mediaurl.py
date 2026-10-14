"""Recover complete video pages from browser-captured playback requests."""

from urllib.parse import urlsplit


def captured_video_page(url: str, page_url: str | None) -> str:
    """YouTube CDN URLs may address just one second or one audio/video track.

    Keep the signed CDN URL untouched unless a matching YouTube page is
    available; the site extractor must choose and assemble the full formats.
    """
    host = (urlsplit(url).hostname or "").lower()
    if not (host == "googlevideo.com" or host.endswith(".googlevideo.com")):
        return url
    if not page_url:
        return url
    page = urlsplit(page_url)
    page_host = (page.hostname or "").lower()
    if page.scheme in ("http", "https") and (
        page_host == "youtu.be" or page_host == "youtube.com" or page_host.endswith(".youtube.com")
    ):
        if page.path == "/watch" or page.path.startswith(("/shorts/", "/live/", "/embed/")):
            return page_url
        if page_host == "youtu.be" and page.path.strip("/"):
            return page_url
    return url
