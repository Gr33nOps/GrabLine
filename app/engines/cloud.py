"""The cloud protocol engine: download over FTP, FTPS, SFTP, SCP, WebDAV and
S3, with resume where the protocol allows it and credentials pulled from the
store automatically.

Each source is one GrabLine job. The task writes to ``job.part_path`` and, on
success, renames to ``job.dest_path`` - the same crash-safe pattern the HTTP
segmented engine uses. Resume continues from the ``.part`` size using FTP
REST, an SFTP seek, or an HTTP Range (WebDAV/S3).
"""

from __future__ import annotations

import ftplib
import logging
import ssl
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from app.core.credentials import CredentialStore
from app.core.errors import DownloadError
from app.core.models import Job, JobStatus
from app.db.database import Database

log = logging.getLogger(__name__)

_CHUNK = 256 * 1024
_PERSIST_SECONDS = 0.3
_DEFAULT_PORTS = {"ftp": 21, "ftps": 21, "sftp": 22, "scp": 22}

#: Schemes this engine owns.
CLOUD_SCHEMES = ("ftp", "ftps", "sftp", "scp", "s3", "webdav", "webdavs")


@dataclass(frozen=True)
class RemoteFile:
    """One file found when listing a remote folder (folder download)."""

    url: str
    name: str
    size: int | None = None


def is_cloud_scheme(url: str) -> bool:
    return urlsplit(url).scheme.lower() in CLOUD_SCHEMES


def _creds(url: str, store: CredentialStore | None) -> tuple[str, str]:
    """(username, secret) for a URL: inline user:pass wins, otherwise a stored
    account for the host, otherwise anonymous."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    user = unquote(parts.username) if parts.username else ""
    password = unquote(parts.password) if parts.password else ""
    if password:
        return user, password
    if store is not None:
        account = store.account_for(scheme, parts.hostname or "", user)
        if account is not None:
            secret = store.secret_for(account) or ""
            return account.username or user, secret
    return user, password


def suggested_filename(url: str) -> str:
    name = unquote(Path(urlsplit(url).path).name)
    return name or "download"


class CloudDownload:
    """Runs one cloud job. One-shot object, like the other engine tasks."""

    def __init__(
        self, db: Database, job: Job, *, credentials: CredentialStore | None = None
    ) -> None:
        self.db = db
        self.job = job
        self.store = credentials
        self._pause = threading.Event()
        self._cancel = threading.Event()
        self._downloaded = 0

    # ------------------------------------------------------------- control

    def pause(self) -> None:
        self._pause.set()

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def bytes_downloaded(self) -> int:
        return self._downloaded

    # ----------------------------------------------------------------- run

    def run(self) -> JobStatus:
        self.db.set_job_status(self.job.id, JobStatus.DOWNLOADING)
        scheme = urlsplit(self.job.url).scheme.lower()
        try:
            if scheme in ("ftp", "ftps"):
                return self._run_ftp(secure=scheme == "ftps")
            if scheme in ("sftp", "scp"):
                return self._run_sftp()
            if scheme == "s3":
                return self._run_s3()
            if scheme in ("webdav", "webdavs"):
                return self._run_webdav()
        except _Paused:
            return self._paused()
        except _Cancelled:
            return self._cancelled()
        except (DownloadError, OSError, ssl.SSLError, ftplib.all_errors) as exc:  # type: ignore[misc]
            return self._failed(str(exc))
        except Exception as exc:  # paramiko/boto3 raise their own error trees
            return self._failed(str(exc))
        return self._failed(f"unsupported cloud scheme: {scheme}")

    # -------------------------------------------------------- state helpers

    def _sink(self) -> tuple[Path, int]:
        """The .part file and where to resume from (its current size)."""
        part = self.job.part_path
        part.parent.mkdir(parents=True, exist_ok=True)
        offset = part.stat().st_size if part.exists() else 0
        self._downloaded = offset
        return part, offset

    def _check(self) -> None:
        if self._cancel.is_set():
            raise _Cancelled
        if self._pause.is_set():
            raise _Paused

    _last_persist = 0.0

    def _advance(self, n: int) -> None:
        import time

        self._downloaded += n
        now = time.monotonic()
        if now - self._last_persist >= _PERSIST_SECONDS:
            self._last_persist = now
            self.db.update_job_downloaded(self.job.id, self._downloaded)
        self._check()

    def _finish(self, part: Path) -> JobStatus:
        part.replace(self.job.dest_path)
        self.db.update_job_downloaded(self.job.id, self._downloaded)
        if self._downloaded:
            self.db.update_job_total(self.job.id, self._downloaded)
        self.db.set_job_status(self.job.id, JobStatus.COMPLETED)
        return JobStatus.COMPLETED

    def _paused(self) -> JobStatus:
        self.db.update_job_downloaded(self.job.id, self._downloaded)
        self.db.set_job_status(self.job.id, JobStatus.PAUSED)
        return JobStatus.PAUSED

    def _cancelled(self) -> JobStatus:
        self.job.part_path.unlink(missing_ok=True)
        self.db.update_job_downloaded(self.job.id, 0)
        self.db.set_job_status(self.job.id, JobStatus.CANCELLED)
        return JobStatus.CANCELLED

    def _failed(self, message: str) -> JobStatus:
        log.info("cloud job %s failed: %s", self.job.id, message)
        self.db.set_job_status(self.job.id, JobStatus.FAILED, error=message)
        return JobStatus.FAILED

    # ------------------------------------------------------------- FTP/FTPS

    def _run_ftp(self, *, secure: bool) -> JobStatus:
        parts = urlsplit(self.job.url)
        remote = unquote(parts.path)
        ftp = _connect_ftp(self.job.url, self.store, secure=secure)
        try:
            ftp.voidcmd("TYPE I")
            try:
                total = ftp.size(remote)
            except ftplib.all_errors:
                total = None
            if total:
                self.db.update_job_total(self.job.id, total)
            part, offset = self._sink()
            if total is not None and offset >= total and offset > 0:
                return self._finish(part)
            mode = "ab" if offset else "wb"
            with open(part, mode) as sink:
                # rest=offset asks the server to resume mid-file (REST command).
                conn = ftp.transfercmd(f"RETR {remote}", rest=offset or None)
                try:
                    while True:
                        self._check()
                        block = conn.recv(_CHUNK)
                        if not block:
                            break
                        sink.write(block)
                        self._advance(len(block))
                finally:
                    conn.close()
            ftp.voidresp()
            return self._finish(part)
        finally:
            try:
                ftp.quit()
            except ftplib.all_errors:
                ftp.close()

    # ----------------------------------------------------------- SFTP / SCP

    def _run_sftp(self) -> JobStatus:
        remote = unquote(urlsplit(self.job.url).path)
        client, sftp = _sftp_client(self.job.url, self.store)
        try:
            total = int(sftp.stat(remote).st_size or 0)
            if total:
                self.db.update_job_total(self.job.id, total)
            part, offset = self._sink()
            if total and offset >= total:
                return self._finish(part)
            with sftp.open(remote, "rb") as source, open(part, "ab" if offset else "wb") as sink:
                source.prefetch(total) if total else None
                if offset:
                    source.seek(offset)
                while True:
                    self._check()
                    block = source.read(_CHUNK)
                    if not block:
                        break
                    sink.write(block)
                    self._advance(len(block))
            return self._finish(part)
        finally:
            sftp.close()
            client.close()

    # ------------------------------------------------------------------- S3

    def _run_s3(self) -> JobStatus:
        parts = urlsplit(self.job.url)
        bucket = parts.netloc
        key = unquote(parts.path).lstrip("/")
        client = _s3_client(self.job.url, self.store)
        head = client.head_object(Bucket=bucket, Key=key)
        total = int(head.get("ContentLength") or 0)
        if total:
            self.db.update_job_total(self.job.id, total)
        part, offset = self._sink()
        if total and offset >= total:
            return self._finish(part)
        extra = {"Range": f"bytes={offset}-"} if offset else {}
        body = client.get_object(Bucket=bucket, Key=key, **extra)["Body"]
        with open(part, "ab" if offset else "wb") as sink:
            for block in body.iter_chunks(_CHUNK):
                self._check()
                sink.write(block)
                self._advance(len(block))
        return self._finish(part)

    # --------------------------------------------------------------- WebDAV

    def _run_webdav(self) -> JobStatus:
        http_url = _webdav_http_url(self.job.url)
        user, secret = _creds(self.job.url, self.store)
        auth = httpx.BasicAuth(user, secret) if user else None
        part, offset = self._sink()
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        timeout = httpx.Timeout(30.0, connect=15.0)
        with (
            httpx.Client(follow_redirects=True, timeout=timeout) as client,
            client.stream("GET", http_url, headers=headers, auth=auth) as response,
        ):
            if response.status_code not in (200, 206):
                if offset and response.status_code == 416:  # already complete
                    return self._finish(part)
                response.raise_for_status()
            total = response.headers.get("Content-Length")
            if total is not None:
                self.db.update_job_total(self.job.id, offset + int(total))
            with open(part, "ab" if offset else "wb") as sink:
                for block in response.iter_bytes(_CHUNK):
                    self._check()
                    sink.write(block)
                    self._advance(len(block))
        return self._finish(part)


def _webdav_http_url(url: str) -> str:
    parts = urlsplit(url)
    scheme = "https" if parts.scheme == "webdavs" else "http"
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    return f"{scheme}://{netloc}{parts.path}"


# ----------------------------------------------------- connection helpers


def _connect_ftp(url: str, store: CredentialStore | None, *, secure: bool) -> ftplib.FTP:
    parts = urlsplit(url)
    user, password = _creds(url, store)
    port = parts.port or _DEFAULT_PORTS["ftp"]
    ftp: ftplib.FTP = ftplib.FTP_TLS() if secure else ftplib.FTP()
    ftp.connect(parts.hostname or "", port, timeout=30)
    ftp.login(user or "anonymous", password or "anonymous@")
    if secure and isinstance(ftp, ftplib.FTP_TLS):
        ftp.prot_p()  # encrypt the data channel too, not just the command one
    return ftp


def _sftp_client(url: str, store: CredentialStore | None) -> tuple[Any, Any]:
    import paramiko

    parts = urlsplit(url)
    user, secret = _creds(url, store)
    port = parts.port or _DEFAULT_PORTS["sftp"]
    account = store.account_for("sftp", parts.hostname or "", user) if store else None
    client = paramiko.SSHClient()
    # Trust-on-first-use: accept unknown host keys (a desktop app can't ship a
    # known_hosts for the whole internet). The transfer itself is integrity-
    # checked by SSH.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect: dict[str, Any] = {
        "hostname": parts.hostname or "",
        "port": port,
        "username": user or None,
        "timeout": 30,
    }
    if account is not None and account.key_file:
        connect["key_filename"] = account.key_file
        connect["passphrase"] = secret or None
    else:
        connect["password"] = secret or None
    client.connect(**connect)
    return client, client.open_sftp()


def _s3_client(url: str, store: CredentialStore | None) -> Any:
    import boto3

    parts = urlsplit(url)
    user, secret = _creds(url, store)
    account = store.account_for("s3", parts.hostname or "", user) if store else None
    kwargs: dict[str, Any] = {}
    if account is not None and account.host and "." in account.host:
        kwargs["endpoint_url"] = f"https://{account.host}"  # S3-compatible host
    if user and secret:
        kwargs["aws_access_key_id"] = user
        kwargs["aws_secret_access_key"] = secret
    # No creds -> boto3 falls back to env/instance profile, or the bucket is
    # public. Either is a legitimate way to reach S3.
    return boto3.client("s3", **kwargs)


# --------------------------------------------------------- folder listing


def list_folder(url: str, store: CredentialStore | None = None) -> list[RemoteFile]:
    """The files directly inside a remote folder (one level), for the
    "download this whole folder" flow. FTP, SFTP and S3 are supported."""
    scheme = urlsplit(url).scheme.lower()
    if scheme in ("ftp", "ftps"):
        return _list_ftp(url, store, secure=scheme == "ftps")
    if scheme in ("sftp", "scp"):
        return _list_sftp(url, store)
    if scheme == "s3":
        return _list_s3(url, store)
    raise DownloadError(f"folder download is not supported for {scheme}:// yet")


def _base(url: str) -> str:
    parts = urlsplit(url)
    root = f"{parts.scheme}://"
    if parts.username:
        root += parts.username + ("@" if not parts.password else f":{parts.password}@")
    root += parts.hostname or ""
    if parts.port:
        root += f":{parts.port}"
    return root


def _list_ftp(url: str, store: CredentialStore | None, *, secure: bool) -> list[RemoteFile]:
    ftp = _connect_ftp(url, store, secure=secure)
    base = _base(url)
    path = unquote(urlsplit(url).path).rstrip("/")
    files: list[RemoteFile] = []
    try:
        for name, facts in ftp.mlsd(path or "/"):
            if facts.get("type") == "file":
                size = int(facts["size"]) if facts.get("size", "").isdigit() else None
                files.append(RemoteFile(f"{base}{path}/{name}", name, size))
    except ftplib.all_errors:
        for name in ftp.nlst(path or "/"):  # older servers without MLSD
            leaf = name.rsplit("/", 1)[-1]
            files.append(RemoteFile(f"{base}{path}/{leaf}", leaf))
    finally:
        ftp.close()
    return files


def _list_sftp(url: str, store: CredentialStore | None) -> list[RemoteFile]:
    import stat as stat_module

    client, sftp = _sftp_client(url, store)
    base = _base(url)
    path = unquote(urlsplit(url).path).rstrip("/") or "/"
    try:
        files = [
            RemoteFile(f"{base}{path}/{entry.filename}", entry.filename, int(entry.st_size or 0))
            for entry in sftp.listdir_attr(path)
            if not stat_module.S_ISDIR(entry.st_mode or 0)
        ]
    finally:
        sftp.close()
        client.close()
    return files


def _list_s3(url: str, store: CredentialStore | None) -> list[RemoteFile]:
    parts = urlsplit(url)
    bucket = parts.netloc
    prefix = unquote(parts.path).lstrip("/")
    client = _s3_client(url, store)
    result = client.list_objects_v2(Bucket=bucket, Prefix=prefix)
    files: list[RemoteFile] = []
    for obj in result.get("Contents", []):
        key = obj["Key"]
        if key.endswith("/"):
            continue
        files.append(RemoteFile(f"s3://{bucket}/{key}", key.rsplit("/", 1)[-1], int(obj["Size"])))
    return files


class _Paused(Exception):
    pass


class _Cancelled(Exception):
    pass
