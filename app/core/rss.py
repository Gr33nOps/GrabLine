"""RSS/Atom feeds for the torrent client: poll feeds, match titles against a
per-feed filter, and hand new torrent/magnet links to the queue.

A feed line in Settings is either just a URL or ``url | filter`` where the
filter is a case-insensitive substring the item title must contain.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx

from app.core.errors import DownloadError

_ATOM = "{http://www.w3.org/2005/Atom}"


@dataclass(frozen=True)
class FeedItem:
    title: str
    link: str
    guid: str


def parse_feed_line(line: str) -> tuple[str, str]:
    """A Settings feed line -> (url, filter). The filter may be empty."""
    url, _, needle = line.partition("|")
    return url.strip(), needle.strip()


def parse_feed(xml_text: str) -> list[FeedItem]:
    """Items of an RSS 2.0 or Atom feed. The link prefers a torrent/magnet
    enclosure over the page link, since that's what the queue can use."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise DownloadError(f"not a valid feed ({exc})") from exc
    items: list[FeedItem] = []
    for node in root.iter("item"):  # RSS 2.0
        title = (node.findtext("title") or "").strip()
        enclosure = node.find("enclosure")
        link = ""
        if enclosure is not None:
            link = (enclosure.get("url") or "").strip()
        if not link:
            link = (node.findtext("link") or "").strip()
        guid = (node.findtext("guid") or link or title).strip()
        if link:
            items.append(FeedItem(title=title, link=link, guid=guid))
    for node in root.iter(f"{_ATOM}entry"):  # Atom
        title = (node.findtext(f"{_ATOM}title") or "").strip()
        link = ""
        for anchor in node.findall(f"{_ATOM}link"):
            href = (anchor.get("href") or "").strip()
            if anchor.get("rel") == "enclosure" and href:
                link = href
                break
            if href and not link:
                link = href
        guid = (node.findtext(f"{_ATOM}id") or link or title).strip()
        if link:
            items.append(FeedItem(title=title, link=link, guid=guid))
    return items


def fetch_feed(url: str, proxy: str | None = None) -> list[FeedItem]:
    try:
        response = httpx.get(url, follow_redirects=True, timeout=30, proxy=proxy or None)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise DownloadError(f"could not fetch the feed ({exc})") from exc
    return parse_feed(response.text)


def matching_items(items: list[FeedItem], needle: str) -> list[FeedItem]:
    """Items whose title contains ``needle`` (case-insensitive); everything
    when the filter is empty."""
    if not needle:
        return list(items)
    lowered = needle.lower()
    return [item for item in items if lowered in item.title.lower()]
