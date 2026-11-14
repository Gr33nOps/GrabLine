import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from cloud_transfer_controls import controls
from cloud_ui_support import ui_pick, ui_remove, ui_save
from common import ROOT, record

from app.core.credentials import CloudAccount, CredentialStore
from app.core.models import JobKind, JobStatus
from app.db.database import Database
from app.engines.cloud import CloudDownload, list_folder

root = ROOT / "s3-authenticated"
root.mkdir(exist_ok=True)
data = b"GrabLine Windows S3 QA\x00" * 100000
requests = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self.reply(True)

    def do_GET(self):
        self.reply(False)

    def reply(self, head):
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        requests.append(
            {"method": self.command, "path": parts.path, "page": query.get("continuation-token")}
        )
        authz = self.headers.get("Authorization", "")
        valid = False
        try:
            kind, fields = authz.split(" ", 1)
            values = dict(part.strip().split("=", 1) for part in fields.split(","))
            access, _date, region, service, _terminal = values["Credential"].split("/")
            signed = {name: self.headers[name] for name in values["SignedHeaders"].split(";")}
            request = AWSRequest(
                method=self.command,
                url=f"http://127.0.0.1:{self.server.server_port}" + self.path,
                data=b"",
                headers=signed,
            )
            request.context["timestamp"] = self.headers["X-Amz-Date"]
            signer = S3SigV4Auth(Credentials("qa-key", "qa-secret"), "s3", region)
            expected = signer.signature(
                signer.string_to_sign(request, signer.canonical_request(request)), request
            )
            valid = (
                kind == "AWS4-HMAC-SHA256"
                and access == "qa-key"
                and service == "s3"
                and hmac.compare_digest(expected, values["Signature"])
            )
        except (ValueError, KeyError, TypeError):
            pass
        if not valid:
            body = (
                b"<Error><Code>SignatureDoesNotMatch</Code>"
                b"<Message>Test credentials rejected</Message></Error>"
            )
            self.send_response(403)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head:
                self.wfile.write(body)
            return
        if "list-type" in query:
            second = query.get("continuation-token") == ["page2"]
            tail = (
                "<IsTruncated>false</IsTruncated>"
                if second
                else (
                    "<IsTruncated>true</IsTruncated><NextContinuationToken>page2</Next"
                    "ContinuationToken>"
                )
            )
            key = "folder/second.bin" if second else "folder/first.bin"
            body = (
                '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                + tail
                + "<Contents><Key>"
                + key
                + f"</Key><Size>{len(data)}</Size></Contents></ListBucketResult>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        assert parts.path == "/qa.bucket.name/folder/first.bin", parts.path
        start = 0
        end = len(data) - 1
        if r := self.headers.get("Range"):
            left, right = r.removeprefix("bytes=").split("-")
            start = int(left)
            end = int(right) if right else end
        body = data[start : end + 1]
        self.send_response(206 if start or end < len(data) - 1 else 200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", '"qa-etag"')
        self.send_header("Accept-Ranges", "bytes")
        if start or end < len(data) - 1:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        self.end_headers()
        if not head:
            self.wfile.write(body)


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
db = Database(root / "s3.db")
store = CredentialStore(db)
try:
    endpoint = f"http://127.0.0.1:{server.server_port}"
    account = CloudAccount("s3", "qa.bucket.name", "qa-key", endpoint_url=endpoint)
    ui_save(store, account, "qa-secret")
    assert store.list_accounts() == [account]
    files = list_folder("s3://qa.bucket.name/folder/", store)
    assert [f.name for f in files] == ["first.bin", "second.bin"], files
    selected = ui_pick("s3://qa.bucket.name/folder/", files)
    assert selected == [files[0].url]
    dest = root / "downloads"
    dest.mkdir(exist_ok=True)
    ui_save(store, account, "wrong-test-secret")
    bad = db.create_job(files[0].url, str(dest), "denied.bin", kind=JobKind.CLOUD)
    assert CloudDownload(db, bad, credentials=store).run() is JobStatus.FAILED
    assert not (dest / "denied.bin").exists()
    ui_save(store, account, "qa-secret")
    job = db.create_job(selected[0], str(dest), "first.bin", kind=JobKind.CLOUD)
    job.part_path.write_bytes(data[:50000])
    task = CloudDownload(db, job, credentials=store)
    status = task.run()
    assert status is JobStatus.COMPLETED, db.get_job(job.id).error
    actual = (dest / "first.bin").read_bytes()
    assert actual == data
    result = {
        "status": "PASS",
        ("scope"): (
            "Real boto3 SigV4-authenticated HTTP service; saved dotted bucket "
            "and endpoint, pagination, resume, invalid-secret rejection; no "
            "AWS account"
        ),
        "invalid_secret_rejected": True,
        "sigv4_checked": True,
        "resumed_bytes": 50000,
        "bytes": len(actual),
        "sha256": hashlib.sha256(actual).hexdigest(),
        "requests": requests,
    }
    (root / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    controls(db, store, selected[0], dest, data, "s3")
    ui_remove(store, account)
finally:
    db.close()
    server.shutdown()
    server.server_close()


record(
    "CLOUD-UI-S3-AUTHENTICATED",
    "PASS",
    "Qt account UI + authenticated local service + Windows vault",
    "Real account dialog, OS vault, denied credentials and checksum verified",
)
