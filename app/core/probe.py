"""Probe a URL for size, resumability, and metadata (F0.1).

The probe is a GET with ``Range: bytes=0-0`` rather than a HEAD: many servers
omit ``Accept-Ranges`` on HEAD but honor ``Range`` on GET, and some reject
HEAD outright. A ``206 Partial Content`` answer proves the server supports
resume; a plain ``200`` means single-connection fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from email.message import Message
from email.utils import collapse_rfc2231_value
from typing import cast

import httpx

from app.core.errors import DownloadError

_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)")
_CONTENT_RANGE_UNSATISFIED = re.compile(r"bytes \*/(\d+)")


@dataclass(frozen=True)
class ProbeResult:
    final_url: str
    resumable: bool
    total_size: int | None
    etag: str | None
    last_modified: str | None
    filename: str | None
    content_type: str | None


def _filename_from_disposition(header: str | None) -> str | None:
    if not header:
        return None
    message = Message()
    message["content-disposition"] = header
    # filename* is the authoritative Unicode name; get_filename() instead
    # returns the first filename, often an LMS's extensionless ASCII fallback.
    # The stdlib returns RFC 2231 triples here, although its type stub only
    # advertises strings.
    params = cast(
        list[tuple[str, str | tuple[str, str, str]]],
        message.get_params(header="content-disposition") or [],
    )
    for key, value in params:
        if key == "filename" and isinstance(value, tuple):
            decoded = collapse_rfc2231_value(value).strip()
            if decoded:
                return decoded
    return message.get_filename()


def probe(
    client: httpx.Client, url: str, extra_headers: dict[str, str] | None = None
) -> ProbeResult:
    # identity: a compressed response would report the *compressed* length here
    # while httpx hands back decompressed bytes during the download, so the
    # size check and every byte offset would be computed against the wrong
    # number. Downloads want the file as stored, never a re-encoded copy.
    headers = {
        "Range": "bytes=0-0",
        "Accept-Encoding": "identity",
        **(extra_headers or {}),
    }
    try:
        for ranged in (True, False):
            if not ranged:
                headers = {key: value for key, value in headers.items() if key.lower() != "range"}
            with client.stream("GET", url, headers=headers) as response:
                status = response.status_code
                if ranged and status in (400, 403, 405, 412, 428, 501):
                    # Some file hosts reject Range requests even though an ordinary
                    # GET works. Retry once without Range; do not consume its body.
                    continue
                if status == 206:
                    resumable = True
                    total = _parse_content_range(response.headers.get("content-range"))
                    response.read()  # tiny 1-byte body; keeps the connection reusable
                elif status == 200:
                    resumable = False
                    length = response.headers.get("content-length")
                    total = int(length) if length and length.isdigit() else None
                    # Deliberately do NOT read: a 200 body is the whole file.
                elif status == 416:
                    # Range not satisfiable happens for zero-byte files; the
                    # Content-Range still tells us the (zero) size.
                    resumable = True
                    match = _CONTENT_RANGE_UNSATISFIED.match(
                        response.headers.get("content-range", "")
                    )
                    total = int(match.group(1)) if match else None
                elif status in (401, 403):
                    # Reached here despite the browser-like User-Agent and Referer
                    # every client now sends, so this is a real access wall, not the
                    # bot filter those defaults clear. Point the user at the one
                    # thing that fixes it: their own browser session.
                    raise DownloadError(
                        f"the server refused this download (HTTP {status}). It may require "
                        "sign-in, browser security verification, or a fresh download link. "
                        "If it works only in your browser, turn off GrabLine interception "
                        "in the extension and retry the website download button."
                    )
                else:
                    raise DownloadError(f"server responded with HTTP {status}")
                return ProbeResult(
                    final_url=str(response.url),
                    resumable=resumable,
                    total_size=total,
                    etag=response.headers.get("etag"),
                    last_modified=response.headers.get("last-modified"),
                    filename=_filename_from_disposition(
                        response.headers.get("content-disposition")
                    ),
                    content_type=response.headers.get("content-type"),
                )
    except httpx.HTTPError as exc:
        raise DownloadError(f"could not reach server: {exc}") from exc

    raise DownloadError("could not determine download metadata")


def _parse_content_range(header: str | None) -> int | None:
    if not header:
        return None
    match = _CONTENT_RANGE.match(header)
    if not match or match.group(3) == "*":
        return None
    return int(match.group(3))
