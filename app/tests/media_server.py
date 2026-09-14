"""A local HTTP server that simulates every failure mode the segmenter must
survive: no range support, redirects, mid-transfer connection drops, unknown
content length, and slow (throttleable) transfers.

It also speaks HTTPS with a freshly generated self-signed certificate
(``MediaServer(tls=True)``), which is how the certificate tests exercise a real
TLS handshake against a real untrusted chain instead of mocking httpx.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import ipaddress
import pathlib
import random
import re
import ssl
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

_RANGE = re.compile(r"bytes=(\d+)-(\d*)$")


def self_signed_cert(directory: str, host: str = "127.0.0.1") -> tuple[str, str]:
    """Write a throwaway self-signed certificate + key for ``host`` into
    ``directory`` and return their paths.

    Self-signed by construction and signed by nobody, so any client doing
    normal verification rejects it - which is precisely the condition the
    "allow invalid/self-signed certificates" setting exists for. It carries a
    SAN for the IP so the only thing wrong with it is that it is untrusted,
    not that it fails hostname matching for an unrelated reason.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = _dt.datetime.now(_dt.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)  # self-signed: issuer is itself
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(host))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = str(pathlib.Path(directory) / "self-signed.pem")
    key_path = str(pathlib.Path(directory) / "self-signed.key")
    with open(cert_path, "wb") as handle:
        handle.write(certificate.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as handle:
        handle.write(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    return cert_path, key_path


def payload(size: int, seed: int = 0) -> bytes:
    """Deterministic pseudo-random bytes so checksums are reproducible."""
    return random.Random(seed).randbytes(size)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class Resource:
    data: bytes
    supports_ranges: bool = True
    etag: str | None = None
    content_disposition: str | None = None
    content_type: str | None = None
    send_content_length: bool = True
    chunk_size: int = 64 * 1024
    delay_per_chunk: float = 0.0
    # Abruptly close the connection after `cut_after` body bytes, for requests
    # numbered cut_from..cut_until (1-based, counted per path, probe included).
    cut_after: int | None = None
    cut_from: int = 1
    cut_until: int = 0
    redirect_to: str | None = None
    # Refuse (403) unless the request carries every one of these headers with
    # the exact value - lets a test model a login-gated file.
    required_headers: dict[str, str] | None = None
    # Extra response headers (e.g. Server, Set-Cookie) for the inspector tests.
    extra_headers: tuple[tuple[str, str], ...] = ()
    # Answer requests numbered fail_from..fail_until (1-based, per path, probe
    # included) with this status instead of the body - a flaky CDN node.
    fail_status: int | None = None
    fail_from: int = 1
    fail_until: int = 0


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _owner(self) -> MediaServer:
        server: Any = self.server
        return server.owner  # type: ignore[no-any-return]

    def do_HEAD(self) -> None:
        # Mirror the servers that reject HEAD; the probe must never rely on it.
        self.send_error(405)

    def do_GET(self) -> None:
        owner = self._owner()
        path = urlsplit(self.path).path
        resource = owner.resources.get(path)
        if resource is None:
            self.send_error(404)
            return
        request_number = owner.bump(path)
        owner.record_headers(path, {k.lower(): v for k, v in self.headers.items()})

        if resource.required_headers:
            missing = any(
                self.headers.get(name) != value for name, value in resource.required_headers.items()
            )
            if missing:
                self.send_error(403)
                return

        if (
            resource.fail_status is not None
            and resource.fail_from <= request_number <= resource.fail_until
        ):
            self.send_error(resource.fail_status)
            return

        if resource.redirect_to is not None:
            self.send_response(302)
            self.send_header("Location", resource.redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        data = resource.data
        status, start, end = 200, 0, len(data) - 1
        range_header = self.headers.get("Range")
        if resource.supports_ranges and range_header:
            match = _RANGE.match(range_header.strip())
            if match:
                start = int(match.group(1))
                if start >= len(data):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(data)}")
                    self.send_header("Content-Length", "0")
                    if resource.etag:
                        self.send_header("ETag", resource.etag)
                    self.end_headers()
                    return
                end = int(match.group(2)) if match.group(2) else len(data) - 1
                end = min(end, len(data) - 1)
                status = 206

        body = data[start : end + 1]
        cut_active = (
            resource.cut_after is not None
            and resource.cut_from <= request_number <= resource.cut_until
        )

        self.send_response(status)
        for name, value in resource.extra_headers:
            self.send_header(name, value)
        if resource.supports_ranges:
            self.send_header("Accept-Ranges", "bytes")
        if resource.etag:
            self.send_header("ETag", resource.etag)
        if resource.content_disposition:
            self.send_header("Content-Disposition", resource.content_disposition)
        if resource.content_type:
            self.send_header("Content-Type", resource.content_type)
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        if resource.send_content_length:
            self.send_header("Content-Length", str(len(body)))
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()

        sent = 0
        while sent < len(body):
            piece = body[sent : sent + resource.chunk_size]
            if cut_active and resource.cut_after is not None:
                budget = resource.cut_after - sent
                if len(piece) >= budget:
                    piece = piece[:budget]
                    if piece:
                        self.wfile.write(piece)
                        self.wfile.flush()
                        owner.add_served(path, len(piece))
                    self.close_connection = True  # drop mid-body: client must retry
                    return
            self.wfile.write(piece)
            sent += len(piece)
            owner.add_served(path, len(piece))
            if resource.delay_per_chunk:
                time.sleep(resource.delay_per_chunk)

    def log_message(self, format: str, *args: object) -> None:
        pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    owner: MediaServer

    def handle_error(self, request: object, client_address: object) -> None:
        pass  # broken pipes are expected: clients abort probes and get killed


class MediaServer:
    def __init__(self, *, tls: bool = False) -> None:
        #: Serve over HTTPS with a self-signed certificate (untrusted on
        #: purpose - see self_signed_cert).
        self.tls = tls
        self._tls_dir: tempfile.TemporaryDirectory[str] | None = None
        self.cert_path: str | None = None
        self.resources: dict[str, Resource] = {}
        self._counts: Counter[str] = Counter()
        self._served: Counter[str] = Counter()
        self._received: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()
        self._httpd: _Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._httpd = _Server(("127.0.0.1", 0), _Handler)
        self._httpd.owner = self
        if self.tls:
            self._tls_dir = tempfile.TemporaryDirectory()
            self.cert_path, key_path = self_signed_cert(self._tls_dir.name)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(self.cert_path, key_path)
            self._httpd.socket = context.wrap_socket(self._httpd.socket, server_side=True)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="media-server", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._tls_dir is not None:
            self._tls_dir.cleanup()
            self._tls_dir = None

    def add(self, path: str, data: bytes = b"", **options: Any) -> str:
        resource = Resource(data=data, **options)
        if resource.etag is None and resource.supports_ranges:
            resource.etag = f'"{sha256(data)[:16]}"'
        self.resources[path] = resource
        return self.url(path)

    def url(self, path: str) -> str:
        assert self._httpd is not None, "server not started"
        port = self._httpd.server_address[1]
        scheme = "https" if self.tls else "http"
        return f"{scheme}://127.0.0.1:{port}{path}"

    def bump(self, path: str) -> int:
        with self._lock:
            self._counts[path] += 1
            return self._counts[path]

    def add_served(self, path: str, count: int) -> None:
        with self._lock:
            self._served[path] += count

    def record_headers(self, path: str, headers: dict[str, str]) -> None:
        with self._lock:
            self._received[path] = headers

    def received_headers(self, path: str) -> dict[str, str]:
        """The (lower-cased) headers of the most recent request for ``path``."""
        with self._lock:
            return dict(self._received.get(path, {}))

    def request_count(self, path: str) -> int:
        with self._lock:
            return self._counts[path]

    def served_bytes(self, path: str) -> int:
        with self._lock:
            return self._served[path]
