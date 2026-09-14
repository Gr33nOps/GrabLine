"""Self-signed / invalid HTTPS certificates (Settings -> Security, and the
per-download override on the Add Download dialog).

These run against a real local HTTPS server holding a real self-signed
certificate, so the TLS handshake genuinely fails the way it does in the wild -
mocking httpx would prove nothing about whether the setting reaches the socket.

The properties under test:

* verification is on by default and a self-signed host fails;
* the global setting turns it off, and so does a per-download override;
* the override never writes to Settings;
* a valid certificate keeps working either way;
* the policy survives redirects, range/segment requests, retries and resume -
  everything the download does on the *same* client;
* yt-dlp is handed the matching option.
"""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from app.core import net
from app.core.manager import INSECURE_OPTION, DownloadManager
from app.core.models import JobKind, JobStatus
from app.core.resolver import Resolver
from app.core.settings import Settings
from app.db.database import Database
from app.tests.conftest import sha256_file, wait_for
from app.tests.media_server import MediaServer, payload, sha256


@pytest.fixture()
def tls_server():
    server = MediaServer(tls=True)
    server.start()
    yield server
    server.stop()


def _ssl_policy(transport: object) -> ssl.SSLContext:
    """The TLS context httpx actually handed this transport.

    Reaching into private attributes on purpose: it is the only way to assert
    the policy that will be used *without* a network round-trip, and a policy
    that only holds for the transports httpx builds itself is exactly the bug
    being guarded against.
    """
    return cast(ssl.SSLContext, cast(Any, transport)._pool._ssl_context)


def _status(db: Database, job_id: int) -> JobStatus:
    job = db.get_job(job_id)
    assert job is not None
    return job.status


def _settle(db: Database, job_id: int, timeout: float = 60.0) -> JobStatus:
    """Wait until the job stops moving, then report where it landed."""
    wait_for(
        lambda: _status(db, job_id) in (JobStatus.COMPLETED, JobStatus.FAILED),
        timeout=timeout,
    )
    return _status(db, job_id)


def _run_one(db: Database, manager: DownloadManager, job_id: int) -> JobStatus:
    return _settle(db, job_id)


# ------------------------------------------------------- 1. the default: fail


def test_self_signed_https_fails_with_everything_off(
    tls_server: MediaServer, db: Database, dest: Path
):
    """Nothing opted in: the certificate is rejected and the download fails.
    This is the behaviour that must never regress."""
    url = tls_server.add("/plain.bin", payload(200_000, 1))
    manager = DownloadManager(db, max_concurrent=2)
    try:
        assert manager.settings.insecure_ssl is False  # the shipped default
        job = manager.add_url(url, dest_dir=str(dest), filename="plain.bin")
        assert _run_one(db, manager, job.id) is JobStatus.FAILED
        fresh = db.get_job(job.id)
        assert fresh is not None and fresh.error
        assert "certificate" in fresh.error.lower()
    finally:
        manager.shutdown()


# ------------------------------------------------- 2. the global setting works


def test_global_insecure_setting_lets_a_self_signed_download_through(
    tls_server: MediaServer, db: Database, dest: Path
):
    data = payload(400_000, 2)
    url = tls_server.add("/global.bin", data)
    manager = DownloadManager(db, max_concurrent=2)
    try:
        manager.settings.insecure_ssl = True
        job = manager.add_url(url, dest_dir=str(dest), filename="global.bin")
        assert _run_one(db, manager, job.id) is JobStatus.COMPLETED
        assert sha256_file(dest / "global.bin") == sha256(data)
    finally:
        manager.shutdown()


# --------------------------------------- 3. the per-download override works


def test_per_download_override_lets_one_download_through(
    tls_server: MediaServer, db: Database, dest: Path
):
    data = payload(400_000, 3)
    url = tls_server.add("/override.bin", data)
    manager = DownloadManager(db, max_concurrent=2)
    try:
        assert manager.settings.insecure_ssl is False  # global stays off
        job = manager.add_url(url, dest_dir=str(dest), filename="override.bin", insecure=True)
        assert _run_one(db, manager, job.id) is JobStatus.COMPLETED
        assert sha256_file(dest / "override.bin") == sha256(data)
    finally:
        manager.shutdown()


# ------------------------------- 4. the override must not touch the global


def test_the_override_never_changes_the_global_setting(
    tls_server: MediaServer, db: Database, dest: Path
):
    url = tls_server.add("/one.bin", payload(100_000, 4))
    other = tls_server.add("/two.bin", payload(100_000, 5))
    manager = DownloadManager(db, max_concurrent=2)
    try:
        allowed = manager.add_url(url, dest_dir=str(dest), filename="one.bin", insecure=True)
        assert _run_one(db, manager, allowed.id) is JobStatus.COMPLETED

        # The stored preference is untouched...
        assert manager.settings.insecure_ssl is False
        assert Settings(db).insecure_ssl is False
        assert db.get_setting("insecure_ssl") in (None, "0")
        # ...the flag lives on the job that opted in, and only that one.
        opted_in = db.get_job(allowed.id)
        assert opted_in is not None and opted_in.options.get(INSECURE_OPTION) is True

        # ...so the very next download still verifies, and still fails.
        plain = manager.add_url(other, dest_dir=str(dest), filename="two.bin")
        assert db.get_job(plain.id).options.get(INSECURE_OPTION) is None  # type: ignore[union-attr]
        assert _run_one(db, manager, plain.id) is JobStatus.FAILED
    finally:
        manager.shutdown()


def test_effective_policy_is_global_or_override(db: Database, dest: Path):
    """The documented formula, checked directly on the manager rather than
    through a download: ignore = global OR per-download."""
    manager = DownloadManager(db, max_concurrent=1)
    try:
        plain = manager.add_url("https://x.test/a.bin", dest_dir=str(dest), filename="a.bin")
        opted = manager.add_url(
            "https://x.test/b.bin", dest_dir=str(dest), filename="b.bin", insecure=True
        )
        manager.pause(plain.id)
        manager.pause(opted.id)

        assert manager.insecure_for(plain) is False
        assert manager.insecure_for(opted) is True

        manager.settings.insecure_ssl = True
        # Global on: both, regardless of the per-job flag.
        assert manager.insecure_for(plain) is True
        assert manager.insecure_for(opted) is True
    finally:
        manager.shutdown()


# ------------------------------------------- 5. a valid certificate still works


def test_a_trusted_server_still_downloads_with_verification_on(
    server: MediaServer, db: Database, dest: Path
):
    """The plain (non-TLS) loopback server stands in for "a server whose
    transport GrabLine has no complaint about": nothing about adding the
    setting may disturb the normal path."""
    data = payload(300_000, 6)
    url = server.add("/ok.bin", data)
    manager = DownloadManager(db, max_concurrent=2)
    try:
        job = manager.add_url(url, dest_dir=str(dest), filename="ok.bin")
        assert _run_one(db, manager, job.id) is JobStatus.COMPLETED
        assert sha256_file(dest / "ok.bin") == sha256(data)
    finally:
        manager.shutdown()


def test_verification_stays_on_for_every_other_client_by_default():
    """build_client verifies unless asked not to - including when a transport
    is built for it (the IPv4 bind / bypass mounts), where a Client-level
    ``verify`` would have been ignored."""
    with net.build_client() as client:
        assert _ssl_policy(client._transport).verify_mode == ssl.CERT_REQUIRED
    # The explicit spellings, without touching the network.
    secure = net.build_client(insecure=False)
    insecure = net.build_client(insecure=True)
    try:
        assert _ssl_policy(secure._transport).verify_mode == ssl.CERT_REQUIRED
        assert _ssl_policy(insecure._transport).verify_mode == ssl.CERT_NONE
        assert _ssl_policy(secure._transport).check_hostname is True
        assert _ssl_policy(insecure._transport).check_hostname is False
    finally:
        secure.close()
        insecure.close()


# ------------------- 6. redirects, ranges, retries and resume keep the policy


def test_the_policy_survives_a_redirect(tls_server: MediaServer, db: Database, dest: Path):
    """A download that lands somewhere else must carry its TLS policy to the
    redirect target - the whole chain runs on the one client."""
    data = payload(250_000, 7)
    tls_server.add("/real.bin", data)
    url = tls_server.add("/go.bin", b"", redirect_to="/real.bin")
    manager = DownloadManager(db, max_concurrent=2)
    try:
        job = manager.add_url(url, dest_dir=str(dest), filename="redirected.bin", insecure=True)
        assert _run_one(db, manager, job.id) is JobStatus.COMPLETED
        assert sha256_file(dest / "redirected.bin") == sha256(data)
        assert tls_server.request_count("/real.bin") >= 1
    finally:
        manager.shutdown()


def test_the_policy_survives_range_requests_and_a_mid_transfer_retry(
    tls_server: MediaServer, db: Database, dest: Path
):
    """Several parallel range requests plus a dropped connection: each retry
    and each segment is a fresh request, and every one of them must still be
    allowed to talk to the self-signed host."""
    data = payload(1_500_000, 8)
    url = tls_server.add(
        "/segmented.bin",
        data,
        chunk_size=32 * 1024,
        # Kill some requests part-way so the downloader has to reconnect.
        cut_after=64 * 1024,
        cut_from=2,
        cut_until=4,
    )
    manager = DownloadManager(db, max_concurrent=1, connections=4)
    try:
        job = manager.add_url(url, dest_dir=str(dest), filename="segmented.bin", insecure=True)
        assert _settle(db, job.id, timeout=90) is JobStatus.COMPLETED
        assert sha256_file(dest / "segmented.bin") == sha256(data)
        # Really segmented + really retried, not one plain GET.
        assert tls_server.request_count("/segmented.bin") > 4
    finally:
        manager.shutdown()


def test_a_certificate_failure_is_never_retried_with_verification_off(
    tls_server: MediaServer, db: Database, dest: Path
):
    """The rule that keeps the default meaningful: a rejected certificate is
    reported, never quietly re-attempted insecurely."""
    url = tls_server.add("/never.bin", payload(100_000, 9))
    manager = DownloadManager(db, max_concurrent=1)
    try:
        manager.settings.auto_retry = False
        job = manager.add_url(url, dest_dir=str(dest), filename="never.bin")
        assert _run_one(db, manager, job.id) is JobStatus.FAILED
        # Nothing was written, the flag was not set on the job, and the global
        # preference was not flipped behind the user's back.
        assert not (dest / "never.bin").exists()
        fresh = db.get_job(job.id)
        assert fresh is not None and fresh.options.get(INSECURE_OPTION) is None
        assert manager.settings.insecure_ssl is False
    finally:
        manager.shutdown()


def test_the_resolver_probe_follows_the_same_policy(tls_server: MediaServer):
    """Resolution happens before the download; if it verified when the
    download would not, an opted-in host could never be added at all."""
    url = tls_server.add("/probe.bin", payload(50_000, 10), content_type="application/octet-stream")
    resolver = Resolver()

    rejected = resolver.resolve(url)
    assert rejected.kind is None
    assert "certificate" in (rejected.message or "").lower()

    allowed = resolver.resolve(url, insecure=True)
    assert allowed.kind is JobKind.DIRECT
    assert allowed.probe is not None and allowed.probe.total_size == 50_000


# ------------------------------------------------ 7. yt-dlp gets the option


def test_yt_dlp_is_told_not_to_check_certificates_only_when_opted_in(
    db: Database, dest: Path
) -> None:
    from app.engines.smart import SmartDownload

    job = db.create_job(
        "https://self-signed.test/video",
        str(dest),
        "video.mp4",
        kind=JobKind.SMART,
        options={"format_spec": "b"},
    )

    secure_opts: dict[str, Any] = SmartDownload(db, job)._build_options(with_runtime=False)
    assert "nocheckcertificate" not in secure_opts

    insecure_opts = SmartDownload(db, job, insecure=True)._build_options(with_runtime=False)
    assert insecure_opts["nocheckcertificate"] is True


def test_smart_analysis_passes_the_option_to_yt_dlp() -> None:
    """The metadata/probe side of the smart engine, not just the download."""
    from app.engines import smart

    opts: dict[str, Any] = {}
    smart._apply_network_guards(opts, None)
    assert "nocheckcertificate" not in opts

    opts = {}
    smart._apply_network_guards(opts, None, insecure=True)
    assert opts["nocheckcertificate"] is True


def test_the_manager_hands_each_engine_the_effective_policy(db: Database, dest: Path) -> None:
    """The last link: whatever the job says, the task actually built for it
    carries the same answer. A value that stops here is a setting that looks
    like it works and does nothing."""
    from app.engines.hls import HlsDownload
    from app.engines.smart import SmartDownload

    manager = DownloadManager(db, max_concurrent=1)
    try:
        manager.settings.auto_start_downloads = False  # keep them out of the scheduler
        direct = manager.add_url("https://x.test/f.bin", dest_dir=str(dest), filename="f.bin")
        stream = manager.add_hls("https://x.test/s.m3u8", dest_dir=str(dest), title="s")
        video = db.create_job(
            "https://x.test/v",
            str(dest),
            "v.mp4",
            kind=JobKind.SMART,
            options={"format_spec": "b"},
        )

        for job in (direct, stream, video):
            task = manager._create_task(job)
            assert task.insecure is False  # type: ignore[attr-defined]

        manager.settings.insecure_ssl = True
        assert manager._create_task(direct).insecure is True  # type: ignore[attr-defined]
        hls_task = manager._create_task(stream)
        assert isinstance(hls_task, HlsDownload) and hls_task.insecure is True
        smart_task = manager._create_task(video)
        assert isinstance(smart_task, SmartDownload) and smart_task.insecure is True
        # FFmpeg is told too, or a self-signed stream host still fails there.
        assert "-tls_verify" in hls_task._command(dest / "out.part")
    finally:
        manager.shutdown()


def test_ffmpeg_verifies_by_default_and_is_given_a_ca_bundle(
    db: Database, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FFmpeg has no certificate policy of its own worth relying on: its
    default was "accept anything" through 7.x and "verify" from 8.0. Both
    answers are now stated explicitly, with the same CA roots httpx trusts."""
    from app.core import ffmpeg as ffmpeg_module
    from app.engines.hls import HlsDownload

    monkeypatch.setattr(ffmpeg_module, "supports_tls_verify", lambda _path: True)
    monkeypatch.setattr(ffmpeg_module, "ca_bundle", lambda: "/etc/ssl/cacert.pem")

    job = db.create_job("https://x.test/s.m3u8", str(dest), "s.mp4", kind=JobKind.HLS)
    secure = HlsDownload(db, job, ffmpeg_path="/usr/bin/ffmpeg")._command(dest / "s.part")
    assert "-tls_verify" in secure
    assert secure[secure.index("-tls_verify") + 1] == "1"
    assert secure[secure.index("-ca_file") + 1] == "/etc/ssl/cacert.pem"

    opted_in = HlsDownload(db, job, ffmpeg_path="/usr/bin/ffmpeg", insecure=True)._command(
        dest / "s.part"
    )
    assert opted_in[opted_in.index("-tls_verify") + 1] == "0"
    assert "-ca_file" not in opted_in


def test_ffmpeg_tls_flags_are_skipped_for_a_plain_http_stream(db: Database, dest: Path) -> None:
    """No TLS to police, and no reason to pay for the capability probe."""
    from app.engines.hls import HlsDownload

    job = db.create_job("http://x.test/s.m3u8", str(dest), "s.mp4", kind=JobKind.HLS)
    command = HlsDownload(db, job, ffmpeg_path="/usr/bin/ffmpeg")._command(dest / "s.part")
    assert "-tls_verify" not in command and "-ca_file" not in command


def test_ffmpeg_tls_flags_are_skipped_on_a_build_that_does_not_know_them(
    db: Database, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown option is a hard FFmpeg error, so an older build keeps its
    own default rather than failing every stream."""
    from app.core import ffmpeg as ffmpeg_module
    from app.engines.hls import HlsDownload

    monkeypatch.setattr(ffmpeg_module, "supports_tls_verify", lambda _path: False)
    job = db.create_job("https://x.test/s.m3u8", str(dest), "s.mp4", kind=JobKind.HLS)
    command = HlsDownload(db, job, ffmpeg_path="/usr/bin/ffmpeg")._command(dest / "s.part")
    assert "-tls_verify" not in command


def test_socks4_and_bypass_transports_carry_the_policy() -> None:
    """The transports httpx would otherwise own: a bypass mount built for a
    proxied client is a separate TLS context, and it has to agree."""
    import ssl

    client = net.build_client(proxy="http://127.0.0.1:3128", bypass_hosts=("intranet.test",))
    try:
        for transport in client._mounts.values():
            assert transport is not None
            assert _ssl_policy(transport).verify_mode == ssl.CERT_REQUIRED
    finally:
        client.close()

    client = net.build_client(
        proxy="http://127.0.0.1:3128", bypass_hosts=("intranet.test",), insecure=True
    )
    try:
        for transport in client._mounts.values():
            assert transport is not None
            assert _ssl_policy(transport).verify_mode == ssl.CERT_NONE
    finally:
        client.close()


def test_an_https_error_message_names_the_certificate(tls_server: MediaServer):
    """So the failure points at the setting that fixes it rather than reading
    as a generic network error."""
    url = tls_server.add("/msg.bin", payload(1000, 11))
    with net.build_client(timeout=10) as client, pytest.raises(httpx.HTTPError) as caught:
        client.get(url)
    assert "certificate" in str(caught.value).lower()


# ------------------------------------ the other engines: cloud and torrents


def test_webdav_goes_through_the_shared_client_and_carries_the_policy(
    db: Database, dest: Path
) -> None:
    """WebDAV used to build a bare ``httpx.Client``, so it had neither the
    app's proxy/User-Agent defaults nor any certificate policy at all."""
    from app.engines.cloud import CloudDownload

    job = db.create_job("webdavs://box.test/f.bin", str(dest), "f.bin", kind=JobKind.CLOUD)
    assert CloudDownload(db, job).insecure is False
    assert CloudDownload(db, job, insecure=True).insecure is True


def test_ftps_control_and_data_channels_follow_the_policy() -> None:
    from app.engines.cloud import _tls_context

    secure = _tls_context(insecure=False)
    assert secure.verify_mode == ssl.CERT_REQUIRED
    assert secure.check_hostname is True

    opted_in = _tls_context(insecure=True)
    assert opted_in.verify_mode == ssl.CERT_NONE
    assert opted_in.check_hostname is False


def test_s3_client_only_disables_verification_when_opted_in(monkeypatch: pytest.MonkeyPatch):
    """A self-hosted S3-compatible endpoint (MinIO, Ceph) behind its own
    certificate is the case this serves."""
    from app.engines import cloud

    captured: list[dict[str, Any]] = []

    class _FakeBoto:
        @staticmethod
        def client(_name: str, **kwargs: Any) -> object:
            captured.append(kwargs)
            return object()

    monkeypatch.setitem(__import__("sys").modules, "boto3", _FakeBoto)

    cloud._s3_client("s3://bucket/key", None)
    assert "verify" not in captured[-1]

    cloud._s3_client("s3://bucket/key", None, insecure=True)
    assert captured[-1]["verify"] is False


def test_the_manager_hands_a_cloud_job_the_effective_policy(db: Database, dest: Path) -> None:
    from app.engines.cloud import CloudDownload

    manager = DownloadManager(db, max_concurrent=1)
    try:
        manager.settings.auto_start_downloads = False
        job = manager.add_cloud("webdavs://box.test/f.bin", dest_dir=str(dest), filename="f.bin")
        task = manager._create_task(job)
        assert isinstance(task, CloudDownload) and task.insecure is False

        manager.settings.insecure_ssl = True
        assert manager._create_task(job).insecure is True  # type: ignore[attr-defined]
    finally:
        manager.shutdown()


def test_a_torrent_fetch_verifies_by_default_and_can_be_opted_out(tls_server: MediaServer):
    """A .torrent hosted over HTTPS: the fetch used to be a bare httpx.get with
    no proxy, no User-Agent and no policy of its own."""
    from app.core.errors import DownloadError
    from app.engines.torrent import fetch_torrent_bytes

    url = tls_server.add("/x.torrent", b"d4:infod4:name1:xee")

    with pytest.raises(DownloadError) as caught:
        fetch_torrent_bytes(url)
    assert "certificate" in str(caught.value).lower()

    assert fetch_torrent_bytes(url, insecure=True) == b"d4:infod4:name1:xee"
    # ...and it now looks like a browser, like every other request the app makes.
    assert tls_server.received_headers("/x.torrent")["user-agent"] == net.DEFAULT_USER_AGENT


def test_the_torrent_session_validates_https_trackers_unless_opted_out(db: Database) -> None:
    from app.engines.torrent import SESSION

    settings = Settings(db)
    assert SESSION._pack(settings)["validate_https_trackers"] is True
    settings.insecure_ssl = True
    assert SESSION._pack(settings)["validate_https_trackers"] is False


# ------------------------------------------------- the shared TLS context


def test_the_ssl_context_is_built_once_per_policy_and_is_correct() -> None:
    """The context is cached because building it parses the whole CA bundle
    (~30 ms). Same object back, and the right policy on it."""
    first = net.ssl_context(verify=True)
    assert net.ssl_context(verify=True) is first
    assert first.verify_mode == ssl.CERT_REQUIRED
    assert first.check_hostname is True

    relaxed = net.ssl_context(verify=False)
    assert relaxed is not first
    assert net.ssl_context(verify=False) is relaxed
    assert relaxed.verify_mode == ssl.CERT_NONE
    assert relaxed.check_hostname is False


def test_building_a_verifying_client_is_cheap_now() -> None:
    """Regression guard on the cost, not just the behaviour: this used to be
    ~30 ms per client, paid several times per add."""
    import time

    net.ssl_context(verify=True)  # warm the cache, as a running app would be
    net.ipv6_broken()
    start = time.perf_counter()
    for _ in range(20):
        net.build_client().close()
    per_client = (time.perf_counter() - start) / 20
    assert per_client < 0.010, (
        f"{per_client * 1000:.1f} ms per client - the CA cache is not working"
    )
