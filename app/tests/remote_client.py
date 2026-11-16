"""Shared test client for the phone-remote tests: a TLS WebSocket "phone".

Used by test_remote_listener.py (against a bare ``RemoteListener``) and
test_remote_service.py (against the real ``RemoteService``). TLS is Python's
``ssl`` on the client side, with the certificate checked against the pin.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import queue
import socket
import ssl
import struct
import threading
import time
from collections.abc import Callable
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from PySide6.QtCore import QCoreApplication

from app.remote import wire
from app.remote.certs import pin_from_spki
from app.remote.devices import auth_transcript, b64url_encode
from app.remote.pairing import compute_code_proof


def pump(condition: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Run the GUI thread's event loop until ``condition`` holds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if condition():
            return True
        time.sleep(0.005)
    QCoreApplication.processEvents()
    return condition()


def settle(seconds: float = 0.2) -> None:
    pump(lambda: False, timeout=seconds)


def mask_frame(opcode: int, payload: bytes, declared: int | None = None) -> bytes:
    size = len(payload) if declared is None else declared
    head = bytes([0x80 | opcode])
    if size < 126:
        head += bytes([0x80 | size])
    elif size < 65536:
        head += bytes([0x80 | 126]) + struct.pack(">H", size)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", size)
    mask = os.urandom(4)
    count = len(payload)
    if not count:
        return head + mask
    key = (mask * (count // 4 + 1))[:count]
    body = (int.from_bytes(payload, "big") ^ int.from_bytes(key, "big")).to_bytes(count, "big")
    return head + mask + body


class Client:
    """A minimal phone: TLS with a pin check, then WebSocket text frames."""

    def __init__(
        self,
        port: int,
        *,
        pin: str | None = None,
        path: str = "/remote",
        headers: tuple[str, ...] = (),
    ) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # the pin below is the trust decision
        raw = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.sock = context.wrap_socket(raw)
        self.tls_version = self.sock.version()
        der = self.sock.getpeercert(binary_form=True)
        assert der is not None
        certificate = x509.load_der_x509_certificate(der)
        self.pin = pin_from_spki(
            certificate.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )
        if pin is not None:
            assert self.pin == pin, "the server's key does not match the pin"
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            f"GET {path} HTTP/1.1",
            "Host: pc",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            "Sec-WebSocket-Extensions: permessage-deflate",
            *headers,
        ]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            head += chunk
        self.response, _, self._rest = head.partition(b"\r\n\r\n")
        self.status = int(self.response.split(b" ")[1]) if self.response else 0
        if self.status == 101:
            assert wire.accept_key(key).encode() in self.response
            assert b"permessage-deflate" not in self.response.lower()
        self.sock.settimeout(None)
        self.inbox: queue.Queue[tuple[str, Any]] = queue.Queue()
        #: Every frame as it arrived: (opcode, raw payload, time.monotonic()).
        self.frames: list[tuple[int, bytes, float]] = []
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _exact(self, size: int) -> bytes:
        data = self._rest[:size]
        self._rest = self._rest[size:]
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def _read(self) -> None:
        try:
            while True:
                first, second = self._exact(2)
                size = second & 0x7F
                if size == 126:
                    size = struct.unpack(">H", self._exact(2))[0]
                elif size == 127:
                    size = struct.unpack(">Q", self._exact(8))[0]
                payload = self._exact(size)
                opcode = first & 0x0F
                self.frames.append((opcode, payload, time.monotonic()))
                if opcode == wire.OP_TEXT:
                    self.inbox.put(("text", json.loads(payload)))
                elif opcode == wire.OP_CLOSE:
                    self.inbox.put(("close", struct.unpack(">H", payload[:2])[0]))
                elif opcode == wire.OP_PONG:
                    self.inbox.put(("pong", payload))
        except (OSError, EOFError, ValueError):
            self.inbox.put(("eof", None))

    def next(self, timeout: float = 5.0) -> tuple[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            QCoreApplication.processEvents()
            try:
                return self.inbox.get(timeout=0.005)
            except queue.Empty:
                continue
        raise AssertionError("no frame from the listener")

    def message(self, timeout: float = 5.0) -> dict[str, Any]:
        kind, value = self.next(timeout)
        assert kind == "text", f"expected a message, got {kind} {value}"
        assert isinstance(value, dict)
        return value

    def closed_with(self, timeout: float = 5.0) -> int:
        kind, value = self.next(timeout)
        assert kind == "close", f"expected a close frame, got {kind} {value}"
        return int(value)

    def send(self, message: dict[str, Any]) -> None:
        self.sock.sendall(mask_frame(wire.OP_TEXT, json.dumps(message).encode()))

    def send_raw(self, data: bytes) -> None:
        self.sock.sendall(data)

    def close(self) -> None:
        # On Linux close() does not wake a thread blocked in recv(), and the
        # freed descriptor number is handed to the next connection, which the
        # stale reader then reads from (bad record MAC on the new client).
        # shutdown() wakes it first; the join keeps it from outliving us.
        with contextlib.suppress(OSError):
            socket.socket.shutdown(self.sock, socket.SHUT_RDWR)
        if self._reader is not threading.current_thread():
            self._reader.join(2.0)
        with contextlib.suppress(OSError):
            self.sock.close()


class Phone:
    """A device key plus the pairing and login steps."""

    def __init__(self, name: str = "Pixel 8") -> None:
        self.name = name
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.public = self.key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        self.device_id = ""

    def pair_request(self, code: str, pin: str, nonce: bytes, version: int = 6) -> dict[str, Any]:
        proof = compute_code_proof(code, nonce, pin, self.public)
        return {
            "id": "r-1",
            "type": "pair",
            "protocolVersion": version,
            "deviceName": self.name,
            "publicKey": b64url_encode(self.public),
            "codeProof": b64url_encode(proof),
        }

    def auth_request(self, pin: str, nonce: bytes, version: int = 6) -> dict[str, Any]:
        signature = self.key.sign(
            auth_transcript(nonce, pin, self.device_id), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": "r-2",
            "type": "auth",
            "protocolVersion": version,
            "deviceId": self.device_id,
            "signature": b64url_encode(signature),
        }
