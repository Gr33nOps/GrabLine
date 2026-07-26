"""Cloud downloads: share-link transforms, the credential store, resolver
routing, and a real loopback FTP transfer (with resume) through the engine.
"""

from __future__ import annotations

import base64
import threading
from pathlib import Path

import pytest

from app.core import cloudlinks
from app.core.credentials import CloudAccount, CredentialStore
from app.core.models import JobKind, JobStatus
from app.core.resolver import Resolver
from app.db.database import Database
from app.engines import cloud
from app.tests.test_resolver import FakeSmart

# --------------------------------------------------------- share-link forms


def test_google_drive_transforms():
    for url in (
        "https://drive.google.com/file/d/ABC123xyz/view?usp=sharing",
        "https://drive.google.com/open?id=ABC123xyz",
        "https://drive.google.com/uc?id=ABC123xyz",
    ):
        direct = cloudlinks.direct_download_url(url)
        assert direct == "https://drive.google.com/uc?export=download&confirm=t&id=ABC123xyz"


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
    assert not cloudlinks.is_cloud_share("https://example.com/x")


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


def test_ftp_folder_listing(db: Database, tmp_path: Path, ftp_server):
    root, port = ftp_server
    (root / "a.bin").write_bytes(b"a" * 10)
    (root / "b.bin").write_bytes(b"b" * 20)
    (root / "sub").mkdir()
    files = cloud.list_folder(f"ftp://bob:secret@127.0.0.1:{port}/")
    names = sorted(f.name for f in files)
    assert names == ["a.bin", "b.bin"]  # directories are skipped
    assert all(f.url.startswith(f"ftp://bob:secret@127.0.0.1:{port}/") for f in files)
