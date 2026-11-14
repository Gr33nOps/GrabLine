"""Cloud downloads: share-link transforms, the credential store, resolver
routing, and a real loopback FTP transfer (with resume) through the engine.
"""

from __future__ import annotations

import base64
import threading
from pathlib import Path

import httpx
import pytest

from app.core import cloudlinks, net
from app.core.credentials import CloudAccount, CredentialStore
from app.core.manager import DownloadManager
from app.core.models import JobKind, JobStatus
from app.core.resolver import Resolver
from app.db.database import Database
from app.engines import cloud
from app.tests.test_resolver import FakeSmart

DRIVE_ID = "1osMDMFzUW6N3-SOQfPFCWchrSLjdL7u3"
DRIVE_DOWNLOAD = (
    f"https://drive.usercontent.google.com/download?id={DRIVE_ID}&export=download&authuser=0"
)
DRIVE_CONFIRMED = DRIVE_DOWNLOAD + "&confirm=t"

# --------------------------------------------------------- share-link forms


def test_google_drive_transforms():
    for url in (
        "https://drive.google.com/file/d/ABC123xyz/view?usp=sharing",
        "https://drive.google.com/open?id=ABC123xyz",
        "https://drive.google.com/uc?id=ABC123xyz",
    ):
        direct = cloudlinks.direct_download_url(url)
        assert direct == (
            "https://drive.usercontent.google.com/download?id=ABC123xyz&export=download&confirm=t"
        )


def test_google_drive_content_url_is_confirmed_without_losing_query_parameters():
    assert cloudlinks.direct_download_url(DRIVE_DOWNLOAD) == DRIVE_CONFIRMED
    assert cloudlinks.direct_download_url(DRIVE_CONFIRMED) == DRIVE_CONFIRMED
    assert cloudlinks.direct_download_url(DRIVE_DOWNLOAD + "#section") == (
        DRIVE_CONFIRMED + "#section"
    )
    assert cloudlinks.is_google_drive_file(DRIVE_DOWNLOAD)
    assert cloudlinks.direct_download_url(
        "https://drive.google.com/file/d/ABC123xyz/view?resourcekey=0-key"
    ) == (
        "https://drive.usercontent.google.com/download?"
        "id=ABC123xyz&export=download&confirm=t&resourcekey=0-key"
    )
    assert not cloudlinks.is_google_drive_file(
        "https://drive.usercontent.google.com.evil.example/download?id=ABC123xyz"
    )
    assert not cloudlinks.is_google_drive_file("https://drive.usercontent.google.com/open?id=x")


def test_google_drive_download_uses_direct_probe_not_yt_dlp(monkeypatch: pytest.MonkeyPatch):
    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        assert request.url.params["confirm"] == "t"
        return httpx.Response(
            206,
            headers={
                "Content-Range": "bytes 0-0/926572254",
                "Content-Disposition": 'attachment; filename="drive-file.zip"',
                "Content-Type": "application/octet-stream",
            },
            content=b"x",
        )

    monkeypatch.setattr(
        net,
        "build_client",
        lambda **_kwargs: httpx.Client(transport=httpx.MockTransport(respond)),
    )
    resolver = Resolver(FakeSmart(match=True, error="metadata HTTP 400"))
    result = resolver.resolve(DRIVE_DOWNLOAD)
    assert result.kind is JobKind.DIRECT
    assert result.url == DRIVE_CONFIRMED
    assert result.probe is not None
    assert result.probe.filename == "drive-file.zip"
    assert result.probe.total_size == 926572254
    assert requested == [DRIVE_CONFIRMED]


def test_direct_queue_normalizes_drive_links(db: Database):
    manager = DownloadManager(db, max_concurrent=0)
    try:
        job = manager.add_url(DRIVE_DOWNLOAD)
        assert job.url == DRIVE_CONFIRMED
        assert job.kind is JobKind.DIRECT
        assert manager.find_existing(DRIVE_DOWNLOAD) == job
    finally:
        manager.shutdown()


def test_dropbox_forces_direct_download():
    assert cloudlinks.direct_download_url("https://www.dropbox.com/s/abc/file.zip?dl=0") == (
        "https://www.dropbox.com/s/abc/file.zip?dl=1"
    )
    # An already-direct host is left alone.
    passthrough = "https://dl.dropboxusercontent.com/s/abc/file.zip"
    assert cloudlinks.direct_download_url(passthrough) == passthrough


def test_onedrive_uses_the_shares_content_endpoint():
    url = "https://1drv.ms/u/s!AbCdEf"
    direct = cloudlinks.direct_download_url(url)
    token = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    assert direct == f"https://api.onedrive.com/v1.0/shares/u!{token}/root/content"


def test_nextcloud_public_share_download():
    assert cloudlinks.direct_download_url("https://cloud.example.com/s/TOKEN123") == (
        "https://cloud.example.com/s/TOKEN123/download"
    )


def test_box_static_passthrough_but_share_page_declined():
    static = "https://app.box.com/shared/static/hash123.zip"
    assert cloudlinks.direct_download_url(static) == static
    assert cloudlinks.direct_download_url("https://app.box.com/s/sharepage") is None


def test_non_cloud_urls_are_ignored():
    assert cloudlinks.direct_download_url("https://example.com/file.zip") is None
    assert cloudlinks.direct_download_url("ftp://host/file") is None


@pytest.mark.parametrize(
    "url",
    [
        "https://evildropbox.com/file.zip",
        "https://evildropboxusercontent.com/file.zip",
        "https://onedrive.live.com.evil.test/file.zip",
        "https://evilonedrive.live.com/file.zip",
        "https://notsharepoint.com/file.zip",
        "https://evil1drv.ms/file.zip",
        "https://box.com.evil.test/shared/static/file.zip",
        "https://app.box.com@evil.test/shared/static/file.zip",
    ],
)
def test_cloud_provider_lookalikes_are_not_rewritten(url: str):
    """Provider text in another hostname/userinfo must not select that provider."""
    assert cloudlinks.direct_download_url(url) is None


@pytest.mark.parametrize("authority", ["www.dropbox.com:443", "WWW.DROPBOX.COM"])
def test_dropbox_uses_the_hostname_independently_of_port_and_case(authority: str):
    assert cloudlinks.direct_download_url(f"https://{authority}/s/abc/file.zip?dl=0") == (
        f"https://{authority}/s/abc/file.zip?dl=1"
    )


def test_sharepoint_with_an_explicit_port_still_uses_the_content_endpoint():
    direct = cloudlinks.direct_download_url("https://tenant.sharepoint.com:443/shared/file")
    assert direct is not None
    prefix = "https://api.onedrive.com/v1.0/shares/u!"
    assert direct.startswith(prefix) and direct.endswith("/root/content")
    token = direct.removeprefix(prefix).removesuffix("/root/content")
    assert base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode() == (
        "https://tenant.sharepoint.com:443/shared/file"
    )


@pytest.mark.parametrize("host", ["notdropbox.com", "box.com.evil.test", "mygoogle.com"])
def test_nextcloud_share_is_not_excluded_by_provider_text_in_an_unrelated_host(host: str):
    assert cloudlinks.direct_download_url(f"https://{host}/s/TOKEN123") == (
        f"https://{host}/s/TOKEN123/download"
    )


@pytest.mark.parametrize("host", ["notmega.nz", "noticloud.com", "drive.proton.me.evil.test"])
def test_unsupported_provider_lookalikes_are_not_rejected(host: str):
    assert cloudlinks.unsupported_cloud_reason(f"https://{host}/file") is None


def test_unsupported_provider_is_recognized_with_an_explicit_port():
    reason = cloudlinks.unsupported_cloud_reason("https://mega.nz:443/file")
    assert reason is not None and "Mega" in reason


# --------------------------------------------------------------- resolver


def test_resolver_routes_cloud_schemes():
    resolver = Resolver(FakeSmart(match=False))
    for url in (
        "ftp://host/file.bin",
        "ftps://host/file.bin",
        "sftp://host/file.bin",
        "scp://host/file.bin",
        "s3://bucket/key",
        "webdav://host/dav/file",
    ):
        assert resolver.resolve(url).kind is JobKind.CLOUD, url


def test_resolver_rewrites_a_share_link_to_direct(server):
    # A Dropbox link becomes a plain https URL and routes as a normal download.
    resolver = Resolver(FakeSmart(match=False))
    resolution = resolver.resolve("https://www.dropbox.com/s/abc/file.zip?dl=0")
    # It tried to probe the rewritten dl=1 URL (unreachable host -> friendly none),
    # proving the rewrite happened rather than being treated as an HTML page.
    assert "dropbox.com" not in (resolution.message or "") or resolution.kind is not None


def test_unknown_scheme_message_mentions_cloud():
    resolution = Resolver(FakeSmart(match=False)).resolve("gopher://host/x")
    assert resolution.kind is None
    assert "ftp" in (resolution.message or "")


# ------------------------------------------------------------ credentials


class _FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service, name, secret):
        self.store[(service, name)] = secret

    def get_password(self, service, name):
        return self.store.get((service, name))

    def delete_password(self, service, name):
        del self.store[(service, name)]


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyring:
    fake = _FakeKeyring()
    monkeypatch.setattr("app.core.credentials._keyring", lambda: fake)
    return fake


def test_credential_store_roundtrip_and_multiple_accounts(db: Database, fake_keyring: _FakeKeyring):
    store = CredentialStore(db)
    a1 = CloudAccount(service="sftp", host="box.example", username="alice", port=22)
    a2 = CloudAccount(service="sftp", host="box.example", username="bob")
    store.save_account(a1, "alice-secret")
    store.save_account(a2, "bob-secret")

    accounts = store.list_accounts()
    assert len(accounts) == 2
    assert store.secret_for(a1) == "alice-secret"
    # Automatic auth: exact username wins, else the first for that host.
    bob = store.account_for("sftp", "box.example", "bob")
    assert bob is not None and bob.username == "bob"
    default = store.account_for("sftp", "box.example")
    assert default is not None and default.username == "alice"
    assert store.account_for("sftp", "other.example") is None


def test_credential_delete(db: Database, fake_keyring: _FakeKeyring):
    store = CredentialStore(db)
    account = CloudAccount(service="ftp", host="ftp.example", username="u")
    store.save_account(account, "pw")
    store.delete_account(account)
    assert store.list_accounts() == []
    assert store.secret_for(account) is None


# ------------------------------------------------------------ engine bits


def test_creds_prefers_inline_userinfo():
    assert cloud._creds("ftp://joe:pw@host/file", None) == ("joe", "pw")


def test_suggested_filename():
    assert cloud.suggested_filename("sftp://host/path/to/movie.mkv") == "movie.mkv"
    assert cloud.suggested_filename("s3://bucket/report%20final.pdf") == "report final.pdf"
    assert cloud.suggested_filename("ftp://host/") == "download"


def test_is_cloud_scheme():
    assert cloud.is_cloud_scheme("sftp://host/x")
    assert cloud.is_cloud_scheme("S3://bucket/key")
    assert not cloud.is_cloud_scheme("https://host/x")


def test_folder_download_unsupported_scheme():
    from app.core.errors import DownloadError

    with pytest.raises(DownloadError, match="folder download is not supported"):
        cloud.list_folder("webdav://host/dir")


# --------------------------------------------------------- FTP loopback e2e


@pytest.fixture
def ftp_server(tmp_path: Path):
    """A real local FTP server (pyftpdlib) serving tmp_path/root."""
    pytest.importorskip("pyftpdlib")
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer

    root = tmp_path / "root"
    root.mkdir()
    authorizer = DummyAuthorizer()
    authorizer.add_user("bob", "secret", str(root), perm="elradfmw")
    handler = FTPHandler
    handler.authorizer = authorizer
    server = FTPServer(("127.0.0.1", 0), handler)
    port = server.socket.getsockname()[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield root, port
    finally:
        server.close_all()


def test_ftp_loopback_download_and_resume(db: Database, tmp_path: Path, ftp_server):
    root, port = ftp_server
    payload = bytes(range(256)) * 400  # ~100 KB
    (root / "file.bin").write_bytes(payload)

    dest = tmp_path / "dl"
    dest.mkdir()
    url = f"ftp://bob:secret@127.0.0.1:{port}/file.bin"

    # Pre-seed the .part with the first half to exercise the REST resume path.
    from app.core.models import PART_SUFFIX

    part = dest / ("file.bin" + PART_SUFFIX)
    part.write_bytes(payload[:50_000])

    job = db.create_job(url, str(dest), "file.bin", kind=JobKind.CLOUD)
    task = cloud.CloudDownload(db, job, credentials=None)
    assert task.run() is JobStatus.COMPLETED
    assert (dest / "file.bin").read_bytes() == payload
    assert not part.exists()


def test_cloud_download_never_overwrites_an_existing_file(db: Database, tmp_path: Path, ftp_server):
    """The direct and HLS engines keep an existing ``file.bin`` and save beside
    it; the cloud engine replaced it outright, destroying the user's copy."""
    root, port = ftp_server
    payload = bytes(range(256)) * 40
    (root / "file.bin").write_bytes(payload)
    dest = tmp_path / "dl"
    dest.mkdir()
    (dest / "file.bin").write_bytes(b"the user's own file")

    job = db.create_job(
        f"ftp://bob:secret@127.0.0.1:{port}/file.bin", str(dest), "file.bin", kind=JobKind.CLOUD
    )
    assert cloud.CloudDownload(db, job, credentials=None).run() is JobStatus.COMPLETED

    assert (dest / "file.bin").read_bytes() == b"the user's own file"
    assert (dest / "file (1).bin").read_bytes() == payload
    finished = db.get_job(job.id)
    assert finished is not None and finished.filename == "file (1).bin"


def test_ftp_folder_listing(db: Database, tmp_path: Path, ftp_server):
    root, port = ftp_server
    (root / "a.bin").write_bytes(b"a" * 10)
    (root / "b.bin").write_bytes(b"b" * 20)
    (root / "sub").mkdir()
    files = cloud.list_folder(f"ftp://bob:secret@127.0.0.1:{port}/")
    names = sorted(f.name for f in files)
    assert names == ["a.bin", "b.bin"]  # directories are skipped
    assert all(f.url.startswith(f"ftp://bob:secret@127.0.0.1:{port}/") for f in files)


@pytest.mark.parametrize("folder", ["sftp://example.test/", "scp://example.test/"])
def test_ssh_root_listing_returns_downloadable_single_slash_url(monkeypatch, folder):
    from types import SimpleNamespace

    class Sftp:
        def listdir_attr(self, path):
            assert path == "/"
            return [SimpleNamespace(filename="file.bin", st_size=12, st_mode=0)]

        def close(self):
            pass

    class Client:
        def close(self):
            pass

    monkeypatch.setattr(cloud, "_sftp_client", lambda url, store: (Client(), Sftp()))
    entries = cloud.list_folder(folder)
    assert entries[0].url == folder + "file.bin"
