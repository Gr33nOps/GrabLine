"""A scripted fake host PC for the link-client tests: a real TLS WebSocket
server on loopback that says what a test tells it to, so the client's handling
of a wrong proof, a changed key, an old protocol or a removed pairing can be
tested without a full ``RemoteListener``. Tests may listen (the gate skips
``app/tests``).
"""

from __future__ import annotations

import contextlib
import json
import secrets
import socket
import ssl
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.remote import certs, wire
from app.remote.devices import b64url_encode

Script = Callable[["FakeConn"], None]


class FakeConn:
    """One accepted connection, after the WebSocket upgrade."""

    def __init__(self, sock: ssl.SSLSocket, host: FakeHost) -> None:
        self.sock = sock
        self.host = host
        self.nonce = secrets.token_bytes(32)
        self.parser = wire.FrameParser(1_048_576, 1003, masked=True)
        self.frames: list[bytes] = []  # raw bytes of every client frame, for masking checks
        self._events: list[tuple[str, object]] = []

    def send(self, message: dict[str, Any]) -> None:
        self.sock.sendall(wire.encode_frame(wire.OP_TEXT, json.dumps(message).encode()))

    def hello(self, **changes: Any) -> None:
        message: dict[str, Any] = {
            "type": "hello",
            "protocolVersion": 6,
            "minClientVersion": 6,
            "pcId": self.host.pc_id,
            "pcName": self.host.pc_name,
            "nonce": b64url_encode(self.nonce),
        }
        message.update(changes)
        self.send(message)

    def recv(self, timeout: float = 5.0) -> dict[str, Any] | None:
        """The next text message the client sent, or None on close/timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._events:
                self.sock.settimeout(0.1)
                try:
                    data = self.sock.recv(65536)
                except (TimeoutError, ssl.SSLWantReadError):
                    continue
                except (OSError, ssl.SSLError):
                    return None
                if not data:
                    return None
                self._events = self.parser.feed(data)
            while self._events:
                kind, value = self._events.pop(0)
                if kind == "text" and isinstance(value, str):
                    loaded = json.loads(value)
                    assert isinstance(loaded, dict)
                    return loaded
                if kind in ("close", "fail"):
                    return None
        return None

    def close(self, code: int | None = None) -> None:
        with contextlib.suppress(OSError, ssl.SSLError):
            if code is not None:
                self.sock.sendall(wire.encode_close(code))
            self.sock.close()


class FakeHost:
    def __init__(
        self,
        folder: Path,
        script: Script,
        *,
        pc_id: str = "A3D7Za2aw26jbGofVV3ZAQ",
        pc_name: str = "DESKTOP",
    ) -> None:
        self.pc_id = pc_id
        self.pc_name = pc_name
        self.script = script
        identity = certs.load_or_create(pc_id, folder)
        self.pin = identity.pin
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._context.load_cert_chain(
            str(folder / certs.CERT_FILENAME), str(folder / certs.KEY_FILENAME)
        )
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(8)
        self._server.settimeout(0.1)
        self.port = int(self._server.getsockname()[1])
        self.connections = 0
        self.errors: list[BaseException] = []
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._accepter = threading.Thread(target=self._accept, daemon=True)
        self._accepter.start()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                raw, _peer = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            thread = threading.Thread(target=self._serve, args=(raw,), daemon=True)
            self._threads.append(thread)
            thread.start()

    def _serve(self, raw: socket.socket) -> None:
        self.connections += 1
        try:
            raw.settimeout(5)
            sock = self._context.wrap_socket(raw, server_side=True)
        except (OSError, ssl.SSLError):
            # A client that refuses the key ends the handshake here: expected.
            with contextlib.suppress(OSError):
                raw.close()
            return
        conn = FakeConn(sock, self)
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = sock.recv(4096)
                if not chunk:
                    return
                head += chunk
            request, _, _rest = head.partition(b"\r\n\r\n")
            status, accept = wire.parse_upgrade(request)
            sock.sendall(wire.upgrade_response(status, accept))
            if status != 101:
                return
            self.script(conn)
        except (OSError, ssl.SSLError):
            pass
        except BaseException as exc:  # a failed assertion in a script
            self.errors.append(exc)
        finally:
            conn.close()

    def stop(self) -> None:
        self._stop.set()
        with contextlib.suppress(OSError):
            self._server.close()
        for thread in self._threads:
            thread.join(2.0)
        self._accepter.join(2.0)
