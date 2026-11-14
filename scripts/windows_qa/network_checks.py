"""Real authenticated HTTP/SOCKS5 proxies, bypass and transfer integrity."""

from __future__ import annotations

import base64
import select
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from common import ROOT, case, digest

from app.core import net
from app.core.manager import DownloadManager
from app.core.models import JobStatus
from app.core.settings import Settings
from app.db.database import Database
from app.tests.media_server import MediaServer, payload, sha256

body = payload(1024**2, 923)
origin = MediaServer()
origin.start()
url = origin.add("/test.bin", body)
requests = []


class HTTPProxy(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if (
            self.headers.get("Proxy-Authorization")
            != ("Basic ") + base64.b64encode(b"qa:qa-only").decode()
        ):
            self.send_response(407)
            self.send_header("Proxy-Authenticate", 'Basic realm="Disposable QA"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        assert self.path.startswith(origin.url("")), self.path
        requests.append(self.path)
        headers = {k: v for k, v in self.headers.items() if k.lower() in {"range", "if-range"}}
        response = httpx.get(self.path, headers=headers, trust_env=False)
        self.send_response(response.status_code)
        for key in ("Content-Length", "Content-Range", "Accept-Ranges", "Content-Type", "ETag"):
            if key in response.headers:
                self.send_header(key, response.headers[key])
        self.end_headers()
        self.wfile.write(response.content)


def read_exact(stream, size):
    result = b""
    while len(result) < size:
        chunk = stream.recv(size - len(result))
        if not chunk:
            raise EOFError
        result += chunk
    return result


class SOCKS(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        try:
            version, count = read_exact(client, 2)
            methods = read_exact(client, count)
            assert version == 5 and 2 in methods
            client.sendall(b"\x05\x02")
            assert read_exact(client, 1) == b"\x01"
            username = read_exact(client, read_exact(client, 1)[0])
            secret = read_exact(client, read_exact(client, 1)[0])
            if (username, secret) != (b"qa", b"qa-only"):
                client.sendall(b"\x01\x01")
                return
            client.sendall(b"\x01\x00")
            version, command, _, address_type = read_exact(client, 4)
            assert version == 5 and command == 1
            if address_type == 1:
                host = socket.inet_ntoa(read_exact(client, 4))
            else:
                assert address_type == 3
                host = read_exact(client, read_exact(client, 1)[0]).decode()
            port = int.from_bytes(read_exact(client, 2), "big")
            assert host in {"127.0.0.1", "localhost"}
            assert origin._httpd and port == origin._httpd.server_port
            with socket.create_connection((host, port), timeout=5) as remote:
                client.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
                requests.append("socks:" + host)
                while True:
                    ready, _, _ = select.select([client, remote], [], [], 5)
                    if not ready:
                        return
                    for stream in ready:
                        data = stream.recv(65536)
                        if not data:
                            return
                        (remote if stream is client else client).sendall(data)
        except (EOFError, OSError):
            return


http = ThreadingHTTPServer(("127.0.0.1", 0), HTTPProxy)
socks = socketserver.ThreadingTCPServer(("127.0.0.1", 0), SOCKS)
socks.daemon_threads = True
for server in (http, socks):
    threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    for scheme, server in (("http", http), ("socks5", socks)):
        proxy = f"{scheme}://qa:qa-only@127.0.0.1:{server.server_address[1]}"
        wrong = proxy.replace("qa-only", "incorrect")
        with case("NETWORK-PROXY-" + scheme.upper(), "real local proxy integration") as evidence:
            with net.build_client(proxy=proxy, timeout=5) as client:
                response = client.get(url)
                assert response.content == body
            denied = False
            try:
                with net.build_client(proxy=wrong, timeout=2) as client:
                    denied = client.get(url).status_code == 407
            except httpx.ProxyError:
                denied = True
            assert denied
            count = len(requests)
            with net.build_client(proxy=wrong, bypass_hosts=("127.0.0.1",), timeout=5) as client:
                assert client.get(url).content == body
            assert len(requests) == count
            directory = ROOT / ("proxy-" + scheme)
            directory.mkdir(exist_ok=True)
            db = Database(directory / "jobs.db")
            settings = Settings(db)
            settings.proxy = proxy
            settings.auto_retry = False
            settings.download_dir = directory
            manager = DownloadManager(db, settings=settings, max_concurrent=1)
            try:
                job = manager.add_url(url)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    fresh = db.get_job(job.id)
                    if fresh and fresh.status in {JobStatus.COMPLETED, JobStatus.FAILED}:
                        break
                    time.sleep(0.05)
                assert fresh and fresh.status is JobStatus.COMPLETED, fresh
                assert digest(fresh.dest_path) == sha256(body)
                evidence.update(
                    sha256=digest(fresh.dest_path), denied_credentials=True, bypass=True
                )
            finally:
                manager.shutdown()
                db.close()
finally:
    for server in (http, socks):
        server.shutdown()
        server.server_close()
    origin.stop()
