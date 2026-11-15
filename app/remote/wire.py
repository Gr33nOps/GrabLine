"""The server side of the WebSocket wire format (RFC 6455), phone to PC.

Pure bytes in, events out: no sockets, no Qt, no TLS. The listener feeds it
the decrypted stream. It is deliberately small and strict (PROTOCOL.md
section 2):

* the upgrade request must be ``GET /remote HTTP/1.1``; any other path is
  404 and any ``Origin`` header is 403;
* no extension is ever negotiated, so ``permessage-deflate`` cannot be on
  and a frame with a reserved bit set is a protocol error;
* a data frame is refused from its header alone when it (or the message it
  continues) is over the limit: the payload is never buffered or parsed;
* phone frames must be masked text; a binary frame or bad UTF-8 is malformed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import struct

_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
PATH = "/remote"
MAX_UPGRADE_BYTES = 8192

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_PROTOCOL_ERROR = 1002
CLOSE_TOO_BIG = 1009

_REASONS = {
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    426: "Upgrade Required",
}

#: One parser event: ("text", str), ("ping", bytes), ("pong", bytes),
#: ("close", int | None) or ("fail", close_code).
Event = tuple[str, object]


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1(key.encode("ascii") + _GUID).digest()).decode("ascii")


def parse_upgrade(head: bytes) -> tuple[int, str]:
    """Judge an HTTP request head (without the final blank line).

    Returns ``(101, accept)`` for a valid upgrade to ``/remote``, else
    ``(status, "")`` with status 400, 403, 404, 405 or 426.
    """
    try:
        lines = head.decode("ascii").split("\r\n")
    except UnicodeDecodeError:
        return 400, ""
    request = lines[0].split(" ")
    if len(request) != 3 or request[2] != "HTTP/1.1":
        return 400, ""
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep or not name or name != name.strip() or "\n" in line or "\r" in line:
            return 400, ""
        headers.setdefault(name.lower(), []).append(value.strip())
    if "origin" in headers:
        return 403, ""  # browsers always send one; the phone never does
    if request[1] != PATH:
        return 404, ""
    if request[0] != "GET":
        return 405, ""
    upgrade = headers.get("upgrade", [])
    connection = [t.strip().lower() for v in headers.get("connection", []) for t in v.split(",")]
    keys = headers.get("sec-websocket-key", [])
    if len(upgrade) != 1 or upgrade[0].lower() != "websocket" or "upgrade" not in connection:
        return 400, ""
    if headers.get("sec-websocket-version") != ["13"]:
        return 426, ""
    if len(keys) != 1:
        return 400, ""
    try:
        if len(base64.b64decode(keys[0], validate=True)) != 16:
            return 400, ""
    except (binascii.Error, ValueError):
        return 400, ""
    return 101, accept_key(keys[0])


def upgrade_response(status: int, accept: str = "") -> bytes:
    """The HTTP answer. Nothing identifies the software or its version."""
    if status == 101:
        return (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode("ascii")
    extra = "Sec-WebSocket-Version: 13\r\n" if status == 426 else ""
    return (
        f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}\r\n"
        f"{extra}Content-Length: 0\r\nConnection: close\r\n\r\n"
    ).encode("ascii")


def encode_frame(opcode: int, payload: bytes = b"") -> bytes:
    """One unmasked, final frame (what a server sends)."""
    size = len(payload)
    if size < 126:
        head = bytes((0x80 | opcode, size))
    elif size < 65536:
        head = bytes((0x80 | opcode, 126)) + struct.pack(">H", size)
    else:
        head = bytes((0x80 | opcode, 127)) + struct.pack(">Q", size)
    return head + payload


def encode_close(code: int, reason: str = "") -> bytes:
    return encode_frame(OP_CLOSE, struct.pack(">H", code) + reason.encode("utf-8")[:123])


def _unmask(data: bytes, mask: bytes) -> bytes:
    size = len(data)
    if not size:
        return b""
    key = (mask * (size // 4 + 1))[:size]
    return (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(size, "big")


class FrameParser:
    """Incremental parser for the frames a client sends.

    ``max_message`` may be changed at any time (4 KiB before login, 1 MiB
    after). ``malformed_code`` is the close code for a binary frame or a text
    message that is not UTF-8. After a ``close`` or ``fail`` event the parser
    is dead and ignores further input.
    """

    def __init__(self, max_message: int, malformed_code: int = 1003) -> None:
        self.max_message = max_message
        self._malformed = malformed_code
        self._buffer = bytearray()
        self._message: bytearray | None = None
        self._dead = False

    def feed(self, data: bytes) -> list[Event]:
        events: list[Event] = []
        if self._dead:
            return events
        self._buffer += data
        while True:
            event = self._next()
            if event is None:
                break
            if event[0] == "more":
                continue
            events.append(event)
            if event[0] in ("close", "fail"):
                self._dead = True
                self._buffer.clear()
                self._message = None
                break
        return events

    def _next(self) -> Event | None:
        buf = self._buffer
        if len(buf) < 2:
            return None
        first, second = buf[0], buf[1]
        opcode = first & 0x0F
        final = bool(first & 0x80)
        size = second & 0x7F
        if first & 0x70 or not second & 0x80:
            return "fail", CLOSE_PROTOCOL_ERROR  # an extension bit, or not masked
        offset = 2
        if size == 126:
            if len(buf) < 4:
                return None
            size = struct.unpack_from(">H", buf, 2)[0]
            offset = 4
        elif size == 127:
            if len(buf) < 10:
                return None
            size = struct.unpack_from(">Q", buf, 2)[0]
            offset = 10
        control = opcode >= 0x8
        if control:
            if opcode not in (OP_CLOSE, OP_PING, OP_PONG) or not final or offset != 2:
                return "fail", CLOSE_PROTOCOL_ERROR
        elif opcode == OP_BINARY:
            return "fail", self._malformed
        elif opcode == OP_TEXT:
            if self._message is not None:
                return "fail", CLOSE_PROTOCOL_ERROR
        elif opcode == OP_CONTINUATION:
            if self._message is None:
                return "fail", CLOSE_PROTOCOL_ERROR
        else:
            return "fail", CLOSE_PROTOCOL_ERROR
        if not control:
            # Judged from the header alone: an oversized payload is never read.
            so_far = len(self._message) if self._message is not None else 0
            if size > self.max_message or so_far + size > self.max_message:
                return "fail", CLOSE_TOO_BIG
        end = offset + 4 + size
        if len(buf) < end:
            return None
        payload = _unmask(bytes(buf[offset + 4 : end]), bytes(buf[offset : offset + 4]))
        del buf[:end]
        if opcode == OP_PING:
            return "ping", payload
        if opcode == OP_PONG:
            return "pong", payload
        if opcode == OP_CLOSE:
            if len(payload) == 1:
                return "fail", CLOSE_PROTOCOL_ERROR
            return "close", struct.unpack(">H", payload[:2])[0] if payload else None
        if self._message is None:
            self._message = bytearray()
        self._message += payload
        if not final:
            return "more", None
        message, self._message = self._message, None
        try:
            return "text", message.decode("utf-8")
        except UnicodeDecodeError:
            return "fail", self._malformed
