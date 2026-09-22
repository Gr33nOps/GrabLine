"""The cloud protocol engine: download over FTP, FTPS, SFTP, SCP, WebDAV and
S3, with resume where the protocol allows it and credentials pulled from the
store automatically.

Each source is one GrabLine job. The task writes to ``job.part_path`` and, on
success, renames to ``job.dest_path`` - the same crash-safe pattern the HTTP
segmented engine uses. Resume continues from the ``.part`` size using FTP
REST, an SFTP seek, or an HTTP Range (WebDAV/S3).
"""

from __future__ import annotations

import contextlib
import ftplib
import logging
import re
import ssl
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from app.core import net, paths
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
        self,
        db: Database,
        job: Job,
        *,
        credentials: CredentialStore | None = None,
        insecure: bool = False,
        proxy: str | None = None,
        proxy_bypass: tuple[str, ...] = (),
    ) -> None:
        self.db = db
        self.job = job
        self.store = credentials
        #: WebDAV is HTTP, so it goes through the app's proxy like everything
        #: else. (ftp/sftp/s3 use their own libraries' transports.)
        self.proxy = proxy
        self.proxy_bypass = proxy_bypass
        #: Accept an invalid/self-signed certificate for this job's transport
        #: (the manager passes ``global setting OR this job's override``).
        #: Applies to the TLS-bearing schemes - ftps, webdavs, s3 over https.
        #: sftp/scp are SSH, not TLS: their trust model is host keys, which
        #: this does not and must not touch.
        self.insecure = insecure
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

    def _sink(self, identity: str | None = None) -> tuple[Path, int]:
        """The .part file and where to resume from.

        ``identity`` is whatever the protocol can say about *which* remote
        object this is - a size and modification time, an ETag. It is recorded
        on the first run and compared on every later one: a ``.part`` is only a
        valid head of the file it was started against, and resuming it against
        a different object joins two halves of two different files into
        something that passes every size check and is garbage. When the remote
        has changed (or we cannot tell), the partial is discarded and the
        download starts over - the only answer that cannot produce a corrupt
        file.
        """
        part = self.job.part_path
        part.parent.mkdir(parents=True, exist_ok=True)
        offset = part.stat().st_size if part.exists() else 0
        if offset and identity is not None:
            stored = str(self.job.options.get(_IDENTITY_OPTION) or "")
            if stored and stored != identity:
                log.info(
                    "cloud job %s: the remote file changed since this partial "
                    "download was started - restarting rather than mixing versions",
                    self.job.id,
                )
                part.unlink(missing_ok=True)
                offset = 0
        if identity is not None and str(self.job.options.get(_IDENTITY_OPTION) or "") != identity:
            options = dict(self.job.options)
            options[_IDENTITY_OPTION] = identity
            self.job.options = options
            with contextlib.suppress(Exception):  # a note, never fatal to a download
                self.db.update_job_options(self.job.id, options)
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
        ftp = _connect_ftp(self.job.url, self.store, secure=secure, insecure=self.insecure)
        try:
            ftp.voidcmd("TYPE I")
            try:
                total = ftp.size(remote)
            except ftplib.all_errors:
                total = None
            if total:
                self.db.update_job_total(self.job.id, total)
            try:
                # MDTM is optional but widely supported; with SIZE it is enough
                # to notice the file being replaced between sessions.
                modified = ftp.voidcmd(f"MDTM {remote}").strip()
            except ftplib.all_errors:
                modified = ""
            part, offset = self._sink(f"ftp:{total}:{modified}")
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
            info = sftp.stat(remote)
            total = int(info.st_size or 0)
            if total:
                self.db.update_job_total(self.job.id, total)
            part, offset = self._sink(f"sftp:{total}:{int(info.st_mtime or 0)}")
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
        client = _s3_client(self.job.url, self.store, insecure=self.insecure)
        head = client.head_object(Bucket=bucket, Key=key)
        total = int(head.get("ContentLength") or 0)
        if total:
            self.db.update_job_total(self.job.id, total)
        # ETag identifies the bytes; VersionId pins the object on a versioned
        # bucket; LastModified catches a same-size replacement on a server that
        # recycles ETags.
        part, offset = self._sink(
            "s3:{}:{}:{}:{}".format(
                total,
                str(head.get("ETag") or "").strip('"'),
                head.get("VersionId") or "",
                head.get("LastModified") or "",
            )
        )
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
            # Through net.build_client, so WebDAV gets the same proxy, the same
            # browser-like User-Agent and the same certificate policy as every
            # other HTTP client in the app instead of httpx's bare defaults.
            net.build_client(
                proxy=self.proxy,
                insecure=self.insecure,
                bypass_hosts=self.proxy_bypass,
                follow_redirects=True,
                timeout=timeout,
            ) as client,
            client.stream("GET", http_url, headers=headers, auth=auth) as response,
        ):
            if offset and response.status_code == 416:
                # "Range not satisfiable" only means "already complete" when the
                # server says the file is exactly as long as what we hold. It
                # equally means the file SHRANK, and finalising then would
                # publish a truncated download as a finished one.
                if _complete_per_content_range(response.headers.get("content-range"), offset):
                    return self._finish(part)
                log.info(
                    "webdav job %s: 416 without a matching total - restarting from zero",
                    self.job.id,
                )
                return self._restart_webdav(part, client, http_url, auth)
            if response.status_code not in (200, 206):
                response.raise_for_status()
            if offset and response.status_code != 206:
                # We asked to resume and the server sent the WHOLE file anyway
                # (plenty ignore Range). Appending it to what we already hold
                # produces `old partial + complete file` - a corrupt download
                # that passes every size check. Start over instead.
                log.info(
                    "webdav job %s: server ignored Range (HTTP %s) - restarting from zero",
                    self.job.id,
                    response.status_code,
                )
                return self._write_webdav_body(response, part, offset=0)
            if offset and not _range_starts_at(response.headers.get("content-range"), offset):
                log.info(
                    "webdav job %s: 206 answered a different range - restarting from zero",
                    self.job.id,
                )
                return self._write_webdav_body(response, part, offset=0)
            return self._write_webdav_body(response, part, offset=offset)

    def _restart_webdav(
        self, part: Path, client: httpx.Client, url: str, auth: httpx.Auth | None
    ) -> JobStatus:
        """Re-fetch from zero after a resume attempt turned out to be unusable."""
        with client.stream("GET", url, auth=auth) as response:
            response.raise_for_status()
            return self._write_webdav_body(response, part, offset=0)

    def _write_webdav_body(self, response: httpx.Response, part: Path, *, offset: int) -> JobStatus:
        """Stream the body into ``part``. ``offset`` 0 truncates and restarts;
        anything else appends to a partial we have proved the response continues."""
        length = response.headers.get("Content-Length")
        if length is not None and length.isdigit():
            self.db.update_job_total(self.job.id, offset + int(length))
        self._downloaded = offset
        with open(part, "ab" if offset else "wb") as sink:
            for block in response.iter_bytes(_CHUNK):
                self._check()
                sink.write(block)
                self._advance(len(block))
        return self._finish(part)


#: Job option holding "which remote object this .part belongs to".
_IDENTITY_OPTION = "remote_identity"

_CONTENT_RANGE_TOTAL = re.compile(r"bytes\s+\*/(\d+)\s*$")
_CONTENT_RANGE_SPAN = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)\s*$")


def _complete_per_content_range(header: str | None, local_size: int) -> bool:
    """Does a 416's ``Content-Range: bytes */TOTAL`` prove we already hold the
    whole file? Missing or malformed proves nothing, so the answer is no."""
    if not header:
        return False
    match = _CONTENT_RANGE_TOTAL.match(header.strip())
    return match is not None and int(match.group(1)) == local_size


def _range_starts_at(header: str | None, offset: int) -> bool:
    """Does a 206's ``Content-Range`` confirm the body continues from
    ``offset``? A 206 with no parsable Content-Range confirms nothing."""
    if not header:
        return False
    match = _CONTENT_RANGE_SPAN.match(header.strip())
    if match is None:
        return False
    start, end = int(match.group(1)), int(match.group(2))
    if start != offset or end < start:
        return False
    total = match.group(3)
    return total == "*" or int(total) > end >= start


def _webdav_http_url(url: str) -> str:
    """The http(s) URL behind a ``webdav://`` / ``webdavs://`` address.

    The query survives: plenty of WebDAV endpoints (Nextcloud public shares,
    signed URLs) carry the credential to reach the file in ``?token=...``, and
    dropping it turned a working share link into a 401. Userinfo is dropped on
    purpose - it is lifted out separately and sent as Basic auth rather than
    left in a URL that ends up in logs.
    """
    parts = urlsplit(url)
    scheme = "https" if parts.scheme == "webdavs" else "http"
    netloc = parts.hostname or ""
    with contextlib.suppress(ValueError):  # malformed port: fall back to the host
        if parts.port:
            netloc += f":{parts.port}"
    return urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment))


# ----------------------------------------------------- connection helpers


def _tls_context(*, insecure: bool) -> ssl.SSLContext:
    """The TLS context for an FTPS control/data channel.

    Built by the same factory every HTTP client in the app uses, rather than
    hand-rolled here. Two reasons: FTPS then trusts exactly the roots the rest
    of GrabLine trusts (certifi, plus ``SSL_CERT_FILE``/``SSL_CERT_DIR`` if the
    machine sets them) instead of whatever the system store happens to hold,
    and the "accept anything" variant exists in exactly one place in the
    codebase - reachable only through the user's explicit opt-in.
    """
    context: ssl.SSLContext = net.ssl_context(verify=not insecure)
    return context


def _connect_ftp(
    url: str, store: CredentialStore | None, *, secure: bool, insecure: bool = False
) -> ftplib.FTP:
    parts = urlsplit(url)
    user, password = _creds(url, store)
    port = parts.port or _DEFAULT_PORTS["ftp"]
    # ftplib.FTP (plain, unencrypted) is deliberate: ftp:// is a scheme
    # GrabLine supports because users have servers that only speak it. It is
    # opt-in per URL - nothing upgrades or downgrades a scheme behind the user,
    # and ftps:// right beside it gets a verified TLS channel.
    ftp: ftplib.FTP = (
        ftplib.FTP_TLS(context=_tls_context(insecure=insecure))
        if secure
        else ftplib.FTP()  # plaintext: ftp:// is a scheme the user chose
    )
    ftp.connect(parts.hostname or "", port, timeout=30)
    ftp.login(user or "anonymous", password or "anonymous@")
    if secure and isinstance(ftp, ftplib.FTP_TLS):
        ftp.prot_p()  # encrypt the data channel too, not just the command one
    return ftp


def known_hosts_path() -> Path:
    """GrabLine's own known_hosts, inside the private data directory."""
    return paths.data_dir() / "known_hosts"


class _TrustOnFirstUse:
    """Accept a host key the first time, remember it, refuse a change.

    ``paramiko.AutoAddPolicy`` accepts *every* unknown key, every time, and
    never writes it down - so a server that swaps identity between two
    downloads is accepted just as readily as the first one, which is precisely
    the case host keys exist to catch. This records the key instead, and a
    later mismatch reaches the user as a failure rather than a shrug.
    """

    def __init__(self, store: Path) -> None:
        self._store = store

    def missing_host_key(self, client: Any, hostname: str, key: Any) -> None:
        import paramiko

        fingerprint = key.get_base64()
        log.info(
            "sftp: trusting %s on first use (%s %s)", hostname, key.get_name(), _sha256_fp(key)
        )
        client.get_host_keys().add(hostname, key.get_name(), key)
        try:
            self._store.parent.mkdir(parents=True, exist_ok=True)
            client.get_host_keys().save(str(self._store))
        except (OSError, paramiko.SSHException) as exc:
            # Not fatal - the connection is still authenticated for this
            # session - but say so, because it means the next run cannot tell
            # a changed key from a first sighting.
            log.warning("sftp: could not record the host key for %s (%s)", hostname, exc)
        del fingerprint


def _sha256_fp(key: Any) -> str:
    """OpenSSH-style ``SHA256:...`` fingerprint, for messages a user can check."""
    import base64
    import hashlib

    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _sftp_client(url: str, store: CredentialStore | None) -> tuple[Any, Any]:
    import paramiko

    parts = urlsplit(url)
    user, secret = _creds(url, store)
    port = parts.port or _DEFAULT_PORTS["sftp"]
    account = store.account_for("sftp", parts.hostname or "", user) if store else None
    client = paramiko.SSHClient()
    # The user's own ~/.ssh/known_hosts first (a server they already trust in a
    # terminal is a server they trust here), then GrabLine's own store.
    with contextlib.suppress(OSError, paramiko.SSHException):
        client.load_system_host_keys()
    trusted = known_hosts_path()
    if trusted.is_file():
        with contextlib.suppress(OSError, paramiko.SSHException):
            client.load_host_keys(str(trusted))
    client.set_missing_host_key_policy(_TrustOnFirstUse(trusted))
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
    try:
        client.connect(**connect)
    except paramiko.BadHostKeyException as exc:
        # The server answered with a different key than the one we recorded.
        # Never replace it silently: that is the whole point of storing it.
        raise DownloadError(
            f"the SSH host key for {parts.hostname} has changed "
            f"(expected {_sha256_fp(exc.expected_key)}, got {_sha256_fp(exc.key)}). "
            "Someone may be impersonating the server. If you changed it yourself, "
            f"remove the old entry from {known_hosts_path()} and try again."
        ) from exc
    return client, client.open_sftp()


def _s3_client(url: str, store: CredentialStore | None, *, insecure: bool = False) -> Any:
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
    if insecure:
        # botocore's spelling of "accept this certificate". Only reachable via
        # the opt-in; without it botocore verifies against its own bundle, as
        # it always has. Chiefly for a self-hosted S3-compatible endpoint
        # (MinIO, Ceph) behind its own certificate.
        kwargs["verify"] = False
    # No creds -> boto3 falls back to env/instance profile, or the bucket is
    # public. Either is a legitimate way to reach S3.
    return boto3.client("s3", **kwargs)


# --------------------------------------------------------- folder listing


def list_folder(
    url: str, store: CredentialStore | None = None, *, insecure: bool = False
) -> list[RemoteFile]:
    """The files directly inside a remote folder (one level), for the
    "download this whole folder" flow. FTP, SFTP and S3 are supported.

    ``insecure`` matches the download's own certificate policy: listing a
    folder on a self-signed host the user has allowed must not fail where the
    download from that same host would succeed."""
    scheme = urlsplit(url).scheme.lower()
    if scheme in ("ftp", "ftps"):
        return _list_ftp(url, store, secure=scheme == "ftps", insecure=insecure)
    if scheme in ("sftp", "scp"):
        return _list_sftp(url, store)
    if scheme == "s3":
        return _list_s3(url, store, insecure=insecure)
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


def _list_ftp(
    url: str, store: CredentialStore | None, *, secure: bool, insecure: bool = False
) -> list[RemoteFile]:
    ftp = _connect_ftp(url, store, secure=secure, insecure=insecure)
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


def _list_s3(
    url: str, store: CredentialStore | None, *, insecure: bool = False
) -> list[RemoteFile]:
    parts = urlsplit(url)
    bucket = parts.netloc
    prefix = unquote(parts.path).lstrip("/")
    client = _s3_client(url, store, insecure=insecure)
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
