"""One blocking TLS WebSocket client connection (PROTOCOL.md sections 2, 4, 21).

No Qt and no threads: the caller (a pairing attempt or a link's session
thread) owns the object and calls it from that one thread. The trust decision
is the pinned key, exactly as on the phone:

* the certificate is NOT validated against any store; its public key is hashed
  (SPKI DER, SHA-256, base64url) and compared in constant time with the stored
  pin, right after the TLS handshake and before one byte of HTTP is sent;
* with ``pin=None`` (first pairing only) the presented key is recorded as
  ``peer_pin`` and the caller decides whether to keep it, which it does only
  after ``serverProof`` verified (section 5.4);
* no hostname check, no redirects, no proxy, TLS 1.2 or newer, no compression.

Frames sent are masked (RFC 6455) and capped at the protocol's 1 MiB; frames
received must be unmasked and are capped the same way. Nothing is logged.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from app.remote import wire
from app.remote.certs import pin_from_spki

DEFAULT_PORT = 47821
FRAME_LIMIT = 1_048_576
CONNECT_TIMEOUT_S = 2.0
HANDSHAKE_TIMEOUT_S = 5.0
SEND_TIMEOUT_S = 10.0
#: How long one ``read`` waits for bytes before it gives the caller the thread back.
POLL_S = 0.05


class TransportError(Exception):
    """The connection could not be made or was lost. Carries no secrets."""


class IdentityMismatch(TransportError):
    """The server's key is not the pinned one (the host's identity changed, or
    something else answers at that address)."""


class ConnectionClosed(TransportError):
    """The peer closed the connection (``code`` is its close code, or None)."""

    def __init__(self, code: int | None = None) -> None:
        super().__init__("closed")
        self.code = code


@dataclass(frozen=True)
class Address:
    host: str
    port: int

    @property
    def host_header(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"


def parse_address(text: str, default_port: int = DEFAULT_PORT) -> Address | None:
    """``host[:port]`` as typed by a person (section 5.4): IPv6 in brackets,
    the port optional. None when it is not usable."""
    text = text.strip()
    if not text or any(ch.isspace() for ch in text) or "\x00" in text:
        return None
    host, port = text, default_port
    if text.startswith("["):
        end = text.find("]")
        if end < 2:
            return None
        host, rest = text[1:end], text[end + 1 :]
        if rest:
            if not rest.startswith(":") or not rest[1:].isascii() or not rest[1:].isdigit():
                return None
            port = int(rest[1:])
    elif text.count(":") == 1:
        host, _, port_text = text.partition(":")
        if not port_text.isascii() or not port_text.isdigit():
            return None
        port = int(port_text)
    elif text.count(":") > 1:
        return None  # a bare IPv6 address needs its brackets to carry a port
    host = host.split("%", 1)[0]
    if not host or not 1 <= port <= 65535:
        return None
    return Address(host, port)


def spki_pin(der_certificate: bytes) -> str:
    """The pin (section 4) of a DER certificate: SHA-256 of its SPKI."""
    certificate = x509.load_der_x509_certificate(der_certificate)
    return pin_from_spki(
        certificate.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )


def _context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # the pin is the whole trust decision
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.options |= ssl.OP_NO_COMPRESSION
    return context


class WsConnection:
    """An open, upgraded connection. Build one with :meth:`open`."""

    def __init__(self, sock: ssl.SSLSocket, peer_pin: str, leftover: bytes) -> None:
        self._sock = sock
        self.peer_pin = peer_pin
        self._parser = wire.FrameParser(FRAME_LIMIT, wire.CLOSE_PROTOCOL_ERROR, masked=False)
        self._pending: list[wire.Event] = self._parser.feed(leftover) if leftover else []
        self._closed = False

    @classmethod
    def open(
        cls,
        address: Address,
        *,
        pin: str | None,
        timeout: float = CONNECT_TIMEOUT_S,
    ) -> WsConnection:
        """Connect, check the key, upgrade. ``pin`` is the stored pin, or None
        to accept any key and record it as ``peer_pin`` (first pairing).

        Raises ``IdentityMismatch`` for a key that is not the pin and
        ``TransportError`` for everything else."""
        raw: socket.socket | None = None
        tls: ssl.SSLSocket | None = None
        try:
            raw = socket.create_connection((address.host, address.port), timeout=timeout)
            raw.settimeout(max(timeout, HANDSHAKE_TIMEOUT_S))
            tls = _context().wrap_socket(raw, server_hostname=None)
            raw = None  # owned by ``tls`` now
            der = tls.getpeercert(binary_form=True)
            if not der:
                raise TransportError("no certificate")
            seen = spki_pin(der)
            if pin is not None and not hmac.compare_digest(
                seen.encode("ascii"), pin.encode("ascii", "replace")
            ):
                raise IdentityMismatch("identity changed")
            key = wire.new_key()
            tls.sendall(wire.build_upgrade_request(address.host_header, key))
            head, rest = cls._read_head(tls)
            if not wire.check_upgrade_response(head, key):
                raise TransportError("not a GrabLine remote")
            tls.settimeout(POLL_S)
            return cls(tls, seen, rest)
        except TransportError:
            cls._discard(tls, raw)
            raise
        except (OSError, ssl.SSLError, ValueError, UnicodeError):
            cls._discard(tls, raw)
            raise TransportError("connection failed") from None

    @staticmethod
    def _discard(tls: ssl.SSLSocket | None, raw: socket.socket | None) -> None:
        for sock in (tls, raw):
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.close()

    @staticmethod
    def _read_head(tls: ssl.SSLSocket) -> tuple[bytes, bytes]:
        data = b""
        deadline = time.monotonic() + HANDSHAKE_TIMEOUT_S
        while b"\r\n\r\n" not in data:
            if len(data) > wire.MAX_UPGRADE_BYTES or time.monotonic() > deadline:
                raise TransportError("no handshake answer")
            chunk = tls.recv(4096)
            if not chunk:
                raise TransportError("closed during the handshake")
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        return head, rest

    # ------------------------------------------------------------- sending

    def send_text(self, text: str) -> None:
        """One masked text frame. ``TransportError`` if it is too big or the
        connection is gone."""
        payload = text.encode("utf-8")
        if len(payload) > FRAME_LIMIT:
            raise TransportError("message too large")
        self._send(wire.encode_client_frame(wire.OP_TEXT, payload))

    def send_json(self, message: dict[str, Any]) -> None:
        self.send_text(
            json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        )

    def send_ping(self) -> None:
        self._send(wire.encode_client_frame(wire.OP_PING, b""))

    def _send(self, frame: bytes) -> None:
        if self._closed:
            raise TransportError("closed")
        try:
            self._sock.settimeout(SEND_TIMEOUT_S)  # a partial write must not time out
            self._sock.sendall(frame)
            self._sock.settimeout(POLL_S)
        except (OSError, ssl.SSLError):
            self._closed = True
            raise TransportError("send failed") from None

    # ------------------------------------------------------------- reading

    def read(self, timeout: float = POLL_S) -> list[dict[str, Any]]:
        """Wait up to ``timeout`` for frames. Returns the decoded JSON objects
        that arrived (usually none or one). Answers pings itself.

        Raises ``ConnectionClosed`` when the peer closes or the stream is
        malformed, ``TransportError`` when the socket fails."""
        messages: list[dict[str, Any]] = []
        events = self._pending
        self._pending = []
        if not events:
            events = self._receive(timeout)
        for kind, value in events:
            if kind == "text" and isinstance(value, str):
                try:
                    message = json.loads(value)
                except (ValueError, RecursionError):
                    continue  # never crash on one bad frame; it is dropped
                if isinstance(message, dict):
                    messages.append(message)
            elif kind == "ping" and isinstance(value, bytes):
                with contextlib.suppress(TransportError):
                    self._send(wire.encode_client_frame(wire.OP_PONG, value))
            elif kind == "close":
                self._closed = True
                raise ConnectionClosed(value if isinstance(value, int) else None)
            elif kind == "fail":
                self.close(value if isinstance(value, int) else wire.CLOSE_PROTOCOL_ERROR)
                raise ConnectionClosed(None)
        return messages

    def _receive(self, timeout: float) -> list[wire.Event]:
        try:
            self._sock.settimeout(max(timeout, 0.001))
            data = self._sock.recv(65536)
        except (TimeoutError, ssl.SSLWantReadError):
            return []
        except (OSError, ssl.SSLError):
            self._closed = True
            raise ConnectionClosed(None) from None
        finally:
            with contextlib.suppress(OSError):
                self._sock.settimeout(POLL_S)
        if not data:
            self._closed = True
            raise ConnectionClosed(None)
        return self._parser.feed(data)

    # ------------------------------------------------------------- closing

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self, code: int = wire.CLOSE_NORMAL) -> None:
        """Say goodbye (best effort) and drop the socket."""
        if not self._closed:
            self._closed = True
            with contextlib.suppress(OSError, ssl.SSLError):
                self._sock.settimeout(1.0)
                self._sock.sendall(wire.encode_client_close(code))
        with contextlib.suppress(OSError, ssl.SSLError):
            self._sock.close()
