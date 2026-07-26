"""Site grabber: crawl a page (optionally a few levels deep on the same host)
and collect every downloadable file link. Regex-based so it needs no HTML
parser dependency; it only follows same-host HTML pages and never leaves the
starting host."""

from __future__ import annotations

import logging
import re
from collections import deque
from urllib.parse import urljoin, urlsplit

import httpx

from app.core import net

log = logging.getLogger(__name__)

_LINK = re.compile(r"""(?:href|src)\s*=\s*["']([^"'<>]+)["']""", re.IGNORECASE)
#: File extensions worth downloading (superset of the link-picker's list).
_FILE = re.compile(
    r"\.(mp4|mkv|webm|mov|avi|m4v|mp3|m4a|flac|wav|ogg|opus|aac|zip|rar|7z|tar|gz|xz|"
    r"iso|pdf|docx?|xlsx?|pptx?|epub|apk|exe|dmg|appimage|deb|rpm|jpe?g|png|gif|webp|"
    r"svg|mpd|m3u8)(\?|$)",
    re.IGNORECASE,
)
#: Paths that look like another HTML page (so worth following when depth > 0).
_PAGE = re.compile(r"(/|\.html?|\.php|\.aspx?)($|\?)", re.IGNORECASE)

MAX_PAGES = 60
MAX_LINKS = 500


def crawl(
    start_url: str,
    *,
    depth: int = 0,
    proxy: str | None = None,
    max_pages: int = MAX_PAGES,
    max_links: int = MAX_LINKS,
) -> list[str]:
    """Return the downloadable file links found from ``start_url``.

    ``depth`` 0 scans just the page; higher values follow same-host HTML links
    that many levels. Bounded by ``max_pages`` and ``max_links``.
    """
    start_host = urlsplit(start_url).hostname or ""
    seen_pages: set[str] = set()
    found: list[str] = []
    found_set: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(start_url, 0)])

    with net.build_client(proxy=proxy, follow_redirects=True, timeout=15) as client:
        while queue and len(seen_pages) < max_pages and len(found) < max_links:
            page_url, level = queue.popleft()
            if page_url in seen_pages:
                continue
            seen_pages.add(page_url)
            html = _fetch(client, page_url)
            if html is None:
                continue
            for raw in _LINK.findall(html):
                absolute = urljoin(page_url, raw.strip())
                if urlsplit(absolute).scheme not in ("http", "https"):
                    continue
                if _FILE.search(absolute):
                    if absolute not in found_set:
                        found_set.add(absolute)
                        found.append(absolute)
                        if len(found) >= max_links:
                            break
                elif (
                    level < depth
                    and urlsplit(absolute).hostname == start_host
                    and _PAGE.search(urlsplit(absolute).path or "/")
                    and absolute not in seen_pages
                ):
                    queue.append((absolute, level + 1))
    return found


def _fetch(client: httpx.Client, url: str) -> str | None:
    try:
        response = client.get(url)
    except httpx.HTTPError as exc:
        log.debug("crawl fetch failed for %s: %s", url, exc)
        return None
    if response.status_code != 200:
        return None
    if "html" not in response.headers.get("content-type", "").lower():
        return None
    return response.text
