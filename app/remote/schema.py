"""Strict validation of phone requests (PROTOCOL.md sections 2 and 12-13).

Pure and thread-safe: the listener thread calls ``validate`` before anything
touches the database or the UI. A valid request comes back as a ``Request``
holding only the known, type-checked fields; anything else raises
``RequestError`` with a protocol error code. Nothing is truncated or coerced.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

MAX_SAFE_INT = 2**53 - 1
MAX_URL = 8192
MAX_NESTING = 8

QUALITIES = frozenset(
    {"best", "2160p", "1440p", "1080p", "720p", "480p", "360p", "mp3", "m4a", "flac"}
)
#: Field names that try to name a location on the PC: refused, never ignored.
PATH_FIELDS = frozenset(
    {"path", "folder", "dir", "destDir", "dest_dir", "filename", "file", "saveAs"}
)

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_FOLDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]*$")


class RequestError(Exception):
    """A request the PC refuses. ``code`` is a PROTOCOL.md section 16 code."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


@dataclass(frozen=True)
class Request:
    id: str
    type: str
    fields: dict[str, Any] = field(default_factory=dict)

    @property
    def cost(self) -> int:
        return COSTS.get(self.type, 1)


# --------------------------------------------------------------- primitives


def _bad(name: str, why: str = "invalid") -> RequestError:
    return RequestError("badRequest", f"{name}: {why}")


def _is_int(value: object) -> bool:
    return type(value) is int and -MAX_SAFE_INT <= value <= MAX_SAFE_INT


def _int(msg: dict[str, Any], name: str, lo: int, hi: int, *, required: bool) -> int | None:
    value = msg.get(name)
    if value is None:
        if required:
            raise _bad(name, "required")
        return None
    if not _is_int(value) or not lo <= value <= hi:
        raise _bad(name)
    return int(value)


def _clean_str(value: object, name: str, lo: int, hi: int) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise _bad(name)
    value = value.strip()
    if not lo <= len(value.encode("utf-16-le")) // 2 <= hi:
        raise _bad(name, "length")
    return value


def _str(msg: dict[str, Any], name: str, lo: int, hi: int, *, required: bool) -> str | None:
    value = msg.get(name)
    if value is None:
        if required:
            raise _bad(name, "required")
        return None
    return _clean_str(value, name, lo, hi)


def _pattern(
    msg: dict[str, Any], name: str, pattern: re.Pattern[str], *, required: bool
) -> str | None:
    value = msg.get(name)
    if value is None:
        if required:
            raise _bad(name, "required")
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise _bad(name)
    return value


def valid_url(value: object, *, allow_magnet: bool = True) -> str | None:
    """The url rule from host.py ``_valid_url``; None if not acceptable."""
    if not isinstance(value, str) or "\x00" in value:
        return None
    value = value.strip()
    if not value or len(value) > MAX_URL:
        return None
    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    if scheme == "magnet":
        return value if allow_magnet and "xt=" in parts.query else None
    if scheme not in ("http", "https") or not parts.netloc or not parts.hostname:
        return None
    return value


def _url(
    msg: dict[str, Any], name: str, *, required: bool, allow_magnet: bool = True
) -> str | None:
    value = msg.get(name)
    if value is None:
        if required:
            raise _bad(name, "required")
        return None
    url = valid_url(value, allow_magnet=allow_magnet)
    if url is None:
        raise _bad(name)
    return url


def b64url_decode(value: object, name: str, *, exact: int | None = None, most: int = 4096) -> bytes:
    """Strict base64url without padding (PROTOCOL.md 'bytes')."""
    if not isinstance(value, str) or not _B64URL_RE.fullmatch(value) or len(value) % 4 == 1:
        raise _bad(name)
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        raise _bad(name) from None
    # Reject non-canonical encodings (stray low bits in the last character).
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != value:
        raise _bad(name)
    if exact is not None and len(raw) != exact:
        raise _bad(name, "length")
    if len(raw) > most:
        raise _bad(name, "length")
    return raw


def _depth(value: object, level: int = 1) -> int:
    if isinstance(value, dict):
        return max([level, *(_depth(v, level + 1) for v in value.values())])
    if isinstance(value, list):
        return max([level, *(_depth(v, level + 1) for v in value)])
    return level


def _url_list(msg: dict[str, Any], name: str, most: int) -> list[str]:
    raw = msg.get(name)
    if not isinstance(raw, list):
        raise _bad(name, "required")
    if len(raw) > most:
        raise RequestError("tooLarge", f"{name}: more than {most}")
    urls: list[str] = []
    for item in raw:
        url = valid_url(item)
        if url is not None and url not in urls:
            urls.append(url)
    return urls


def _target(msg: dict[str, Any]) -> dict[str, Any]:
    quality = msg.get("quality")
    if quality is not None and (not isinstance(quality, str) or quality not in QUALITIES):
        raise _bad("quality")
    return {
        "quality": quality or "best",
        "queueId": _int(msg, "queueId", 1, MAX_SAFE_INT, required=False),
        "folderId": _pattern(msg, "folderId", _FOLDER_ID_RE, required=False),
    }


# ----------------------------------------------------------------- messages


def _download(msg: dict[str, Any]) -> dict[str, Any]:
    return {
        "url": _url(msg, "url", required=True),
        "requestId": _pattern(msg, "requestId", _REQUEST_ID_RE, required=True),
        "title": _str(msg, "title", 0, 512, required=False),
        "pageUrl": _url(msg, "pageUrl", required=False, allow_magnet=False),
        **_target(msg),
    }


def _links(msg: dict[str, Any]) -> dict[str, Any]:
    urls = _url_list(msg, "urls", 300)
    if not urls:
        raise _bad("urls", "no valid link")
    return {
        "urls": urls,
        "requestId": _pattern(msg, "requestId", _REQUEST_ID_RE, required=True),
        "pageUrl": _url(msg, "pageUrl", required=False, allow_magnet=False),
        "pageTitle": _str(msg, "pageTitle", 0, 512, required=False),
        **_target(msg),
    }


def _status(msg: dict[str, Any]) -> dict[str, Any]:
    return {"urls": _url_list(msg, "urls", 50)}


def _recent(msg: dict[str, Any]) -> dict[str, Any]:
    return {"limit": _int(msg, "limit", 1, 20, required=False) or 5}


def _focus(msg: dict[str, Any]) -> dict[str, Any]:
    return {"target": _str(msg, "target", 0, 32, required=False)}


_JOB_FILTERS = ("all", "active", "finished", "failed")


def _jobs(msg: dict[str, Any]) -> dict[str, Any]:
    flt = msg.get("filter", "all")
    if flt is None:
        flt = "all"
    if not isinstance(flt, str) or flt not in _JOB_FILTERS:
        raise _bad("filter")
    cursor = msg.get("cursor")
    after: int | None = None
    if cursor is not None:
        after = decode_cursor(cursor)
    return {
        "limit": _int(msg, "limit", 1, 100, required=False) or 50,
        "after": after,
        "filter": flt,
    }


def encode_cursor(last_id: int) -> str:
    """Opaque to the phone: base64url (no padding) of the decimal id."""
    return base64.urlsafe_b64encode(str(last_id).encode("ascii")).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: object) -> int:
    if not isinstance(cursor, str) or len(cursor) > 64:
        raise _bad("cursor")
    raw = b64url_decode(cursor, "cursor", most=48)
    if not re.fullmatch(rb"[1-9][0-9]{0,15}", raw):
        raise _bad("cursor")
    value = int(raw)
    if value > MAX_SAFE_INT:
        raise _bad("cursor")
    return value


_CONTROL_ACTIONS = ("pause", "resume", "cancel", "retry", "remove")


def _control(msg: dict[str, Any]) -> dict[str, Any]:
    action = msg.get("action")
    if not isinstance(action, str) or action not in _CONTROL_ACTIONS:
        raise _bad("action")
    return {"jobId": _int(msg, "jobId", 1, MAX_SAFE_INT, required=True), "action": action}


_GLOBAL_ACTIONS = ("get", "pauseAll", "resumeAll", "setSpeedLimit", "clearSpeedLimit")


def _global(msg: dict[str, Any]) -> dict[str, Any]:
    action = msg.get("action")
    if not isinstance(action, str) or action not in _GLOBAL_ACTIONS:
        raise _bad("action")
    limit = None
    if action == "setSpeedLimit":
        limit = _int(msg, "bytesPerSecond", 1024, 10_000_000_000, required=True)
    return {"action": action, "bytesPerSecond": limit}


def _analyze(msg: dict[str, Any]) -> dict[str, Any]:
    return {"url": _url(msg, "url", required=True, allow_magnet=False)}


_TOPICS = ("jobs", "global", "outbox")


def _subscribe(msg: dict[str, Any]) -> dict[str, Any]:
    raw = msg.get("topics")
    if raw is None:
        return {"topics": ["jobs", "global"]}
    if not isinstance(raw, list) or len(raw) > 16:
        raise _bad("topics")
    topics = [t for t in _TOPICS if t in raw]
    return {"topics": topics}


def _empty(_msg: dict[str, Any]) -> dict[str, Any]:
    return {}


def _files(msg: dict[str, Any]) -> dict[str, Any]:
    cursor = msg.get("cursor")
    return {
        "limit": _int(msg, "limit", 1, 100, required=False) or 50,
        "after": decode_cursor(cursor) if cursor is not None else None,
    }


def _fetch(msg: dict[str, Any]) -> dict[str, Any]:
    return {
        "jobId": _int(msg, "jobId", 1, MAX_SAFE_INT, required=True),
        "offset": _int(msg, "offset", 0, MAX_SAFE_INT, required=False) or 0,
        "length": _int(msg, "length", 1, MAX_SAFE_INT, required=False),
        "etag": _str(msg, "etag", 1, 64, required=False),
    }


def _fetch_cancel(msg: dict[str, Any]) -> dict[str, Any]:
    return {"streamId": _int(msg, "streamId", 1, 2**32 - 1, required=True)}


def _file_hash(msg: dict[str, Any]) -> dict[str, Any]:
    return {"jobId": _int(msg, "jobId", 1, MAX_SAFE_INT, required=True)}


def _outbox_ack(msg: dict[str, Any]) -> dict[str, Any]:
    raw = msg.get("itemIds")
    if not isinstance(raw, list) or not raw:
        raise _bad("itemIds", "required")
    if len(raw) > 100:
        raise RequestError("tooLarge", "itemIds: more than 100")
    if not all(_is_int(v) and v >= 1 for v in raw):
        raise _bad("itemIds")
    return {"itemIds": list(dict.fromkeys(raw))}


def _pair(msg: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocolVersion": _int(msg, "protocolVersion", 0, MAX_SAFE_INT, required=True),
        "deviceName": _str(msg, "deviceName", 1, 64, required=True),
        "publicKey": b64url_decode(msg.get("publicKey"), "publicKey", exact=91),
        "codeProof": b64url_decode(msg.get("codeProof"), "codeProof", exact=32),
        "appVersion": _str(msg, "appVersion", 0, 32, required=False),
    }


def _auth(msg: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocolVersion": _int(msg, "protocolVersion", 0, MAX_SAFE_INT, required=True),
        "deviceId": _pattern(msg, "deviceId", _DEVICE_ID_RE, required=True),
        "signature": b64url_decode(msg.get("signature"), "signature", most=72),
        "appVersion": _str(msg, "appVersion", 0, 32, required=False),
    }


VALIDATORS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "pair": _pair,
    "auth": _auth,
    "ping": _empty,
    "download": _download,
    "status": _status,
    "recent": _recent,
    "focus": _focus,
    "links": _links,
    "jobs": _jobs,
    "control": _control,
    "global": _global,
    "queues": _empty,
    "analyze": _analyze,
    "subscribe": _subscribe,
    "unsubscribe": _empty,
    "unpair": _empty,
    "files": _files,
    "fetch": _fetch,
    "fetchCancel": _fetch_cancel,
    "fileHash": _file_hash,
    "outbox": _empty,
    "outboxAck": _outbox_ack,
}

COSTS = {"analyze": 5, "links": 5, "fileHash": 5}

#: Phase-4 message groups; served only when the listener enables them.
PHASE4_TYPES = frozenset({"files", "fetch", "fetchCancel", "fileHash", "outbox", "outboxAck"})


def request_id(message: object) -> str | None:
    """The request's usable id (for error replies), or None."""
    if isinstance(message, dict):
        value = message.get("id")
        if isinstance(value, str) and _ID_RE.fullmatch(value):
            return value
    return None


def validate(message: object) -> Request:
    """Check one decoded phone request. Raises ``RequestError``."""
    if not isinstance(message, dict):
        raise _bad("message", "not an object")
    if _depth(message) > MAX_NESTING:
        raise _bad("message", "nested too deeply")
    # Location names are refused before any other field check (QA-08, QA-40).
    if PATH_FIELDS.intersection(message):
        raise RequestError("forbidden", "requests may not name a location")
    rid = request_id(message)
    if rid is None:
        raise _bad("id")
    kind = message.get("type")
    if not isinstance(kind, str):
        raise _bad("type")
    validator = VALIDATORS.get(kind)
    if validator is None:
        raise RequestError("unsupported", "unknown type")
    return Request(rid, kind, validator(message))
