"""Connection-boundary regressions for saved cloud accounts and S3 listing."""

from __future__ import annotations

import ftplib
import json
from typing import Any

import pytest

from app.core import net
from app.core.credentials import CloudAccount, CredentialStore
from app.db.database import Database
from app.engines import cloud
from app.tests.test_cloud import fake_keyring  # noqa: F401


@pytest.fixture
def store(db: Database, request: pytest.FixtureRequest) -> CredentialStore:
    request.getfixturevalue("fake_keyring")
    return CredentialStore(db)


@pytest.mark.parametrize(
    ("scheme", "saved_port", "url_port", "expected"),
    [("ftp", 2121, "", 2121), ("ftps", 2990, "", 2990), ("ftp", 2121, ":2021", 2021)],
)
def test_ftp_saved_port_and_explicit_url_precedence(
    store, monkeypatch, scheme, saved_port, url_port, expected
):
    connected: dict[str, Any] = {}

    class FTP:
        def connect(self, host, port, timeout):
            connected.update(host=host, port=port)

        def login(self, user, password):
            pass

        def prot_p(self):
            pass

    monkeypatch.setattr(ftplib, "FTP", FTP)

    class TLS(FTP):
        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr(ftplib, "FTP_TLS", TLS)
    store.save_account(CloudAccount(scheme, "files.example", "qa", saved_port), "test-only")
    cloud._connect_ftp(f"{scheme}://files.example{url_port}/file", store, secure=scheme == "ftps")
    assert connected["port"] == expected


@pytest.fixture
def ssh_connection(monkeypatch, tmp_path):
    import paramiko

    connected: dict[str, Any] = {}

    class SSH:
        def load_system_host_keys(self):
            pass

        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, **kwargs):
            connected.update(kwargs)

        def open_sftp(self):
            return object()

    monkeypatch.setattr(paramiko, "SSHClient", SSH)
    monkeypatch.setattr(cloud, "known_hosts_path", lambda: tmp_path / "known_hosts")
    return connected


@pytest.mark.parametrize(
    ("scheme", "url_port", "expected"),
    [("sftp", "", 2222), ("scp", "", 2222), ("sftp", ":2200", 2200)],
)
def test_ssh_saved_port_and_explicit_url_precedence(
    store, ssh_connection, scheme, url_port, expected
):
    store.save_account(CloudAccount(scheme, "files.example", "qa", 2222), "test-only")
    cloud._sftp_client(f"{scheme}://files.example{url_port}/file", store)
    assert ssh_connection["port"] == expected


def test_scp_uses_its_saved_private_key_and_passphrase(store, ssh_connection):
    store.save_account(
        CloudAccount("scp", "files.example", "qa", key_file="qa-private-key"), "test-passphrase"
    )
    cloud._sftp_client("scp://files.example/file", store)
    assert ssh_connection.get("key_filename") == "qa-private-key"
    assert ssh_connection.get("passphrase") == "test-passphrase"
    assert "password" not in ssh_connection


@pytest.fixture
def s3_connection(monkeypatch):
    import boto3

    connected: dict[str, Any] = {}

    def client(service, **kwargs):
        connected.update(kwargs)
        return object()

    monkeypatch.setattr(boto3, "client", client)
    return connected


def test_s3_dotted_bucket_is_not_an_endpoint(store, s3_connection):
    store.save_account(CloudAccount("s3", "qa.bucket.name", "qa-key"), "test-only")
    cloud._s3_client("s3://qa.bucket.name/object.bin", store)
    assert "endpoint_url" not in s3_connection
    assert s3_connection["aws_access_key_id"] == "qa-key"


def test_s3_explicit_endpoint_survives_account_storage(db, store, s3_connection):
    # Load the public persisted representation, including the optional endpoint.
    db.set_setting(
        "cloud_accounts",
        json.dumps(
            [{"service": "s3", "host": "qa-bucket", "endpoint_url": "https://minio.example:9000"}]
        ),
    )
    account = store.list_accounts()[0]
    assert getattr(account, "endpoint_url", "") == "https://minio.example:9000"
    cloud._s3_client("s3://qa-bucket/object.bin", store)
    assert s3_connection.get("endpoint_url") == "https://minio.example:9000"


def test_account_editor_saves_an_explicit_s3_endpoint():
    from app.tests.qt_app import ensure_app
    from app.ui.cloud_dialog import _AccountEditor

    ensure_app()
    editor = _AccountEditor()
    try:
        editor.service.setCurrentText("s3")
        editor.host.setText("qa.bucket.name")
        editor.endpoint.setText("https://minio.example:9000")
        account, _secret = editor.result_account()
        assert account.host == "qa.bucket.name"
        assert account.endpoint_url == "https://minio.example:9000"
        editor.service.setCurrentText("sftp")
        account, _secret = editor.result_account()
        assert account.endpoint_url == ""
    finally:
        editor.deleteLater()


def test_s3_folder_collects_all_pages_and_skips_directory_markers(monkeypatch):
    class S3:
        def list_objects_v2(self, **kwargs):
            assert kwargs["Bucket"] == "qa-bucket"
            assert kwargs["Prefix"] == "folder/"
            if kwargs.get("ContinuationToken") == "second-page":
                return {"IsTruncated": False, "Contents": [{"Key": "folder/second.bin", "Size": 2}]}
            assert "ContinuationToken" not in kwargs
            return {
                "IsTruncated": True,
                "NextContinuationToken": "second-page",
                "Contents": [{"Key": "folder/", "Size": 0}, {"Key": "folder/first.bin", "Size": 1}],
            }

    monkeypatch.setattr(cloud, "_s3_client", lambda *args, **kwargs: S3())
    files = cloud.list_folder("s3://qa-bucket/folder/")
    assert [(f.name, f.size) for f in files] == [("first.bin", 1), ("second.bin", 2)]
    assert [f.url for f in files] == [
        "s3://qa-bucket/folder/first.bin",
        "s3://qa-bucket/folder/second.bin",
    ]


@pytest.mark.parametrize("scheme", ["ftp", "sftp", "s3", "webdav"])
def test_connection_rejection_marks_job_failed_instead_of_escaping(db, dest, monkeypatch, scheme):
    from app.core.models import JobKind, JobStatus

    def rejected(*args, **kwargs):
        raise PermissionError("test server rejected credentials")

    if scheme == "webdav":
        monkeypatch.setattr(net, "build_client", rejected)
    else:
        monkeypatch.setattr(
            cloud,
            {"ftp": "_connect_ftp", "sftp": "_sftp_client", "s3": "_s3_client"}[scheme],
            rejected,
        )
    job = db.create_job(
        f"{scheme}://files.example/file.bin", str(dest), "file.bin", kind=JobKind.CLOUD
    )
    assert cloud.CloudDownload(db, job).run() is JobStatus.FAILED
    fresh = db.get_job(job.id)
    assert fresh is not None and fresh.status is JobStatus.FAILED
    assert fresh.error is not None and "rejected credentials" in fresh.error


@pytest.mark.parametrize("scheme", ["webdav", "webdavs"])
def test_saved_webdav_login_is_available_over_http_and_https(store, scheme):
    store.save_account(CloudAccount("webdav", "files.example", "qa"), "qa-password")
    assert cloud._creds(f"{scheme}://files.example/file.bin", store) == ("qa", "qa-password")


@pytest.mark.parametrize(
    ("scheme", "url_port", "expected"),
    [("webdav", "", 9123), ("webdavs", "", 9123), ("webdavs", ":9999", 9999)],
)
def test_webdav_saved_port_and_explicit_url_precedence(
    db, dest, store, monkeypatch, scheme, url_port, expected
):
    import httpx

    from app.core.models import JobKind, JobStatus

    seen = []

    def respond(request):
        seen.append(request.url.port)
        return httpx.Response(200, content=b"test", headers={"Content-Length": "4"})

    monkeypatch.setattr(
        net, "build_client", lambda **kwargs: httpx.Client(transport=httpx.MockTransport(respond))
    )
    store.save_account(CloudAccount("webdav", "files.example", "qa", 9123), "qa-password")
    job = db.create_job(
        f"{scheme}://files.example{url_port}/file.bin", str(dest), "file.bin", kind=JobKind.CLOUD
    )
    assert cloud.CloudDownload(db, job, credentials=store).run() is JobStatus.COMPLETED
    assert seen == [expected]
    assert job.dest_path.read_bytes() == b"test"


def test_webdav_ipv6_url_keeps_host_brackets():
    assert cloud._webdav_http_url("webdavs://qa:secret@[::1]:9123/file.bin?token=qa") == (
        "https://[::1]:9123/file.bin?token=qa"
    )
