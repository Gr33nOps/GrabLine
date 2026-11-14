import base64
import hashlib
import json
import logging
import os
import ssl
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cloud_transfer_controls import controls
from cloud_ui_support import ui_pick, ui_remove, ui_save
from common import ROOT, record
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler, TLS_FTPHandler
from pyftpdlib.servers import FTPServer

from app.core.credentials import CloudAccount, CredentialStore
from app.core.models import JobKind, JobStatus
from app.db.database import Database
from app.engines.cloud import CloudDownload, list_folder
from app.tests.media_server import payload, self_signed_cert

logging.getLogger("pyftpdlib").setLevel(logging.ERROR)
root = Path(tempfile.mkdtemp(prefix="private-auth-", dir=ROOT))
os.environ["LOCALAPPDATA"] = str(root / "profile")
body = payload(4 * 1024**2, 475)
(root / "source").mkdir(exist_ok=True)
(root / "source/file.bin").write_bytes(body)
cert, key = self_signed_cert(str(root))
results = []


def transfer(db, store, url, dest, name, insecure=False, expect=JobStatus.COMPLETED, resume=False):
    dest.mkdir(exist_ok=True, parents=True)
    job = db.create_job(url, str(dest), name, kind=JobKind.CLOUD)
    if resume:
        job.part_path.write_bytes(body[:131072])
    status = CloudDownload(db, job, credentials=store, insecure=insecure).run()
    fresh = db.get_job(job.id)
    assert status is expect, (status, fresh.error)
    if expect is JobStatus.COMPLETED:
        assert (dest / name).read_bytes() == body
    else:
        assert not (dest / name).exists()
    return fresh.error


for scheme in ("ftp", "ftps"):
    authorizer = DummyAuthorizer()
    authorizer.add_user("qa", "qa-password", str((root / "source").resolve()), perm="elr")
    Parent = TLS_FTPHandler if scheme == "ftps" else FTPHandler

    class Handler(Parent):
        pass

    Handler.authorizer = authorizer
    Handler.auth_failed_timeout = 0.05
    if scheme == "ftps":
        Handler.certfile = cert
        Handler.keyfile = key
        Handler.tls_control_required = True
        Handler.tls_data_required = True
    server = FTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(
        target=lambda server=server: server.serve_forever(timeout=0.1, handle_exit=False),
        daemon=True,
    ).start()
    db = Database(root / (scheme + ".db"))
    store = CredentialStore(db)
    account = CloudAccount(scheme, "127.0.0.1", "qa", server.socket.getsockname()[1])
    url = f"{scheme}://127.0.0.1/file.bin"
    dest = root / scheme
    try:
        ui_save(store, account, "wrong-password")
        transfer(
            db, store, url, dest, "denied.bin", insecure=scheme == "ftps", expect=JobStatus.FAILED
        )
        ui_save(store, account, "qa-password")
        if scheme == "ftps":
            transfer(db, store, url, dest, "untrusted.bin", expect=JobStatus.FAILED)
        transfer(db, store, url, dest, "complete.bin", insecure=scheme == "ftps", resume=True)
        listing = list_folder(f"{scheme}://127.0.0.1/", store, insecure=scheme == "ftps")
        assert any(f.name == "file.bin" and f.size == len(body) for f in listing), listing
        selected = ui_pick(f"{scheme}://127.0.0.1/", listing)
        transfer(db, store, selected[0], dest, "picked.bin", insecure=scheme == "ftps")
        controls(db, store, selected[0], dest, body, scheme, insecure=scheme == "ftps")
        ui_remove(store, account)
        results.append(
            {
                "service": scheme,
                "status": "PASS",
                "saved_port": True,
                "resumed_bytes": 131072,
                "bad_password_rejected": True,
                "folder_listing": True,
                "tls_verification_rejected_self_signed": scheme == "ftps",
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        )
    finally:
        db.close()
        server.close_all()


class Dav(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        expected = "Basic " + base64.b64encode(b"qa:qa-password").decode()
        if self.headers.get("Authorization") != expected:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="QA"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        offset = int(self.headers.get("Range", "bytes=0-").split("=")[1].split("-")[0])
        chunk = body[offset:]
        self.send_response(206 if offset else 200)
        self.send_header("Content-Length", str(len(chunk)))
        if offset:
            self.send_header("Content-Range", f"bytes {offset}-{len(body) - 1}/{len(body)}")
        self.end_headers()
        try:
            for start in range(0, len(chunk), 16384):
                self.wfile.write(chunk[start : start + 16384])
                self.wfile.flush()
                time.sleep(0.01)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLEOFError):
            pass  # A paused/cancelled client intentionally closes its socket.


server = ThreadingHTTPServer(("127.0.0.1", 0), Dav)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(cert, key)
server.socket = ctx.wrap_socket(server.socket, server_side=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
db = Database(root / "webdavs.db")
store = CredentialStore(db)
account = CloudAccount("webdav", "127.0.0.1", "qa", server.server_port)
url = "webdavs://127.0.0.1/file.bin"
dest = root / "webdavs"
try:
    ui_save(store, account, "wrong-password")
    transfer(db, store, url, dest, "denied.bin", insecure=True, expect=JobStatus.FAILED)
    ui_save(store, account, "qa-password")
    transfer(db, store, url, dest, "untrusted.bin", expect=JobStatus.FAILED)
    transfer(db, store, url, dest, "complete.bin", insecure=True, resume=True)
    for action in ("pause", "cancel"):
        job = db.create_job(url, str(dest), action + ".bin", kind=JobKind.CLOUD)
        task = CloudDownload(db, job, credentials=store, insecure=True)
        status = []
        worker = threading.Thread(target=lambda task=task, status=status: status.append(task.run()))
        worker.start()
        deadline = time.monotonic() + 8
        while task.bytes_downloaded == 0 and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert task.bytes_downloaded > 0 and worker.is_alive()
        getattr(task, action)()
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert status == [JobStatus.PAUSED if action == "pause" else JobStatus.CANCELLED]
        if action == "pause":
            assert 0 < job.part_path.stat().st_size < len(body)
            assert (
                CloudDownload(db, db.get_job(job.id), credentials=store, insecure=True).run()
                is JobStatus.COMPLETED
            )
            assert job.dest_path.read_bytes() == body
        else:
            assert not job.dest_path.exists() and not job.part_path.exists()
    record(
        "CLOUD-WEBDAV-PAUSE-CANCEL",
        "PASS",
        "authenticated local HTTPS + real cloud worker",
        (
            "Live pause retained a partial, resume matched full hash, "
            "live cancellation removed its partial"
        ),
        sha256=hashlib.sha256(body).hexdigest(),
    )
    ui_remove(store, account)
    results.append(
        {
            "service": "webdavs",
            "status": "PASS",
            "bad_password_rejected": True,
            "resumed_bytes": 131072,
            "tls_verification_rejected_self_signed": True,
            "sha256": hashlib.sha256(body).hexdigest(),
        }
    )
finally:
    db.close()
    server.shutdown()
    server.server_close()
(root.parent / "ftp-webdav-authenticated-results.json").write_text(json.dumps(results, indent=2))
print(json.dumps(results, indent=2))

record(
    "CLOUD-UI-FTP-WEBDAV",
    "PASS",
    "Qt account UI + authenticated local service + Windows vault",
    "Real account dialog, OS vault, denied credentials and checksum verified",
)
