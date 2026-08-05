"""Native Messaging wire format: uint32 little-endian length + UTF-8 JSON.

Browsers cap host->browser messages at 1 MB and browser->host at 64 MB; this
is a control channel only (URLs and status), so we enforce 1 MB both ways.
"""

from __future__ import annotations

import json
import struct
from typing import Any, BinaryIO

MAX_MESSAGE_BYTES = 1024 * 1024


class ProtocolError(Exception):
    pass


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    """Read one message; None on clean EOF (the browser closed the pipe)."""
    header = stream.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise ProtocolError("truncated message header")
    (length,) = struct.unpack("<I", header)
    if length > MAX_MESSAGE_BYTES:
        raise ProtocolError(f"message too large ({length} bytes)")
    payload = stream.read(length)
    if len(payload) != length:
        raise ProtocolError("truncated message body")
    try:
        message = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise ProtocolError("message must be a JSON object")
    return message


def write_message(stream: BinaryIO, message: dict[str, Any]) -> None:
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ProtocolError("refusing to send an oversized message")
    stream.write(struct.pack("<I", len(payload)))
    stream.write(payload)
    stream.flush()
