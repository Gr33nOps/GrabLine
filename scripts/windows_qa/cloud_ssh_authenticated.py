import json
import os
import socket
import threading

import paramiko
from cloud_transfer_controls import controls
from cloud_ui_support import ui_pick, ui_remove, ui_save
from common import ROOT, record

from app.core.credentials import CloudAccount, CredentialStore
from app.core.models import JobKind, JobStatus
from app.db.database import Database
from app.engines.cloud import CloudDownload, list_folder
from app.tests.media_server import payload, sha256

QA = ROOT
out = QA / "followup/ssh-authenticated"
out.mkdir(exist_ok=True, parents=True)
os.environ["LOCALAPPDATA"] = str(out / "profile")
data = payload(512 * 1024, 317)
remote = out / "file.bin"
remote.write_bytes(data)
client_key = paramiko.RSAKey.generate(2048)
key_path = out / "test-private-key"
client_key.write_private_key_file(str(key_path), password="qa-key-pass")


class Auth(paramiko.ServerInterface):
    def check_auth_publickey(self, u, key):
        return paramiko.AUTH_SUCCESSFUL if u == "qa" and key == client_key else paramiko.AUTH_FAILED

    def check_auth_password(self, u, p):
        return (
            paramiko.AUTH_SUCCESSFUL if (u, p) == ("qa", "qa-test-only") else paramiko.AUTH_FAILED
        )

    def get_allowed_auths(self, u):
        return "password,publickey"

    def check_channel_request(self, kind, cid):
        return (
            paramiko.OPEN_SUCCEEDED
            if kind == "session"
            else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        )


class Files(paramiko.SFTPServerInterface):
    def stat(self, path):
        if path != "/file.bin":
            return paramiko.SFTP_NO_SUCH_FILE
        return paramiko.SFTPAttributes.from_stat(remote.stat())

    def list_folder(self, path):
        a = paramiko.SFTPAttributes.from_stat(remote.stat())
        a.filename = "file.bin"
        return [a]

    def open(self, path, flags, attr):
        if path != "/file.bin":
            return paramiko.SFTP_NO_SUCH_FILE
        h = paramiko.SFTPHandle(flags)
        h.readfile = remote.open("rb")
        return h


server = socket.socket()
server.bind(("127.0.0.1", 0))
server.listen(5)
server.settimeout(0.3)
port = server.getsockname()[1]
key = paramiko.RSAKey.generate(2048)
stop = threading.Event()
transports = []


def serve():
    while not stop.is_set():
        try:
            conn, _ = server.accept()
        except TimeoutError:
            continue
        except OSError:
            break
        t = paramiko.Transport(conn)
        transports.append(t)
        t.add_server_key(key)
        t.set_subsystem_handler("sftp", paramiko.SFTPServer, Files)
        t.start_server(server=Auth())


thread = threading.Thread(target=serve, daemon=True)
thread.start()
results = []
try:
    for scheme in ["sftp", "scp"]:
        db = Database(out / (scheme + ".db"))
        dest = out / scheme
        dest.mkdir(exist_ok=True)
        store = CredentialStore(db)
        account = CloudAccount(
            scheme, "127.0.0.1", "qa", port, key_file=str(key_path) if scheme == "scp" else ""
        )
        ui_save(store, account, "qa-key-pass" if scheme == "scp" else "qa-test-only")
        url = f"{scheme}://127.0.0.1/file.bin"
        try:
            ui_save(store, account, "wrong-test-secret")
            bad = db.create_job(url, str(dest), "bad-auth.bin", kind=JobKind.CLOUD)
            assert CloudDownload(db, bad, credentials=store).run() is JobStatus.FAILED
            assert not (dest / "bad-auth.bin").exists()
            ui_save(store, account, "qa-key-pass" if scheme == "scp" else "qa-test-only")
            job = db.create_job(url, str(dest), "file.bin", kind=JobKind.CLOUD)
            status = CloudDownload(db, job, credentials=store).run()
            assert status is JobStatus.COMPLETED, db.get_job(job.id).error
            assert sha256((dest / "file.bin").read_bytes()) == sha256(data)
            resume_job = db.create_job(url, str(dest), "resumed.bin", kind=JobKind.CLOUD)
            resume_job.part_path.write_bytes(data[:131072])
            assert CloudDownload(db, resume_job, credentials=store).run() is JobStatus.COMPLETED
            assert sha256(resume_job.dest_path.read_bytes()) == sha256(data)
            listing = list_folder(url.rsplit("/", 1)[0] + "/", store)
            assert (
                len(listing) == 1 and listing[0].name == "file.bin" and listing[0].size == len(data)
            )
            selected = ui_pick(url.rsplit("/", 1)[0] + "/", listing)
            assert selected == [listing[0].url]
            picked = db.create_job(selected[0], str(dest), "picked.bin", kind=JobKind.CLOUD)
            assert CloudDownload(db, picked, credentials=store).run() is JobStatus.COMPLETED
            assert sha256(picked.dest_path.read_bytes()) == sha256(data)
            controls(db, store, selected[0], dest, data, scheme)
            ui_remove(store, account)
            results.append(
                {
                    "feature": scheme.upper()
                    + " saved custom port, real local SSH transfer and listing"
                    + (
                        " using encrypted saved private key"
                        if scheme == "scp"
                        else " using saved password"
                    ),
                    "status": "PASS",
                    "size": len(data),
                    "checksum_matches": True,
                    "invalid_credentials_rejected": True,
                    "resumed_prefix_bytes": 131072,
                }
            )
        finally:
            db.close()
finally:
    stop.set()
    server.close()
    for t in transports:
        t.close()

(QA / "followup/ssh-authenticated-results.json").write_text(json.dumps(results, indent=2))
print(json.dumps(results, indent=2))

record(
    "CLOUD-UI-SSH-AUTHENTICATED",
    "PASS",
    "Qt account UI + authenticated local service + Windows vault",
    "Real account dialog, OS vault, denied credentials and checksum verified",
)
