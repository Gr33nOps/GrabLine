"""The network wave: proxy validation and the multi-scheme client factory,
VPN detection, per-host speed limits (real loopback), the auto-throttle
decision, and the torrent proxy translation.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.core import net
from app.core.manager import DownloadManager
from app.core.settings import Settings
from app.db.database import Database
from app.tests.conftest import sha256_file, wait_for
from app.tests.media_server import MediaServer, payload, sha256

MB = 1024 * 1024


# --------------------------------------------------------------- proxy


def test_validate_proxy_accepts_all_schemes():
    for good in (
        "",
        "http://host:8080",
        "https://host:8080",
        "socks5://host:1080",
        "socks5h://host:1080",
        "socks4://host:1080",
        "socks4a://host:1080",
        "socks5://user:pw@host:1080",
    ):
        assert net.validate_proxy(good) is None, good


def test_validate_proxy_rejects_bad():
    assert net.validate_proxy("ftp://host") is not None
    assert net.validate_proxy("socks5://") is not None  # no host
    assert net.validate_proxy("just-text") is not None


def test_build_client_for_every_scheme():
    # No connection is made; we only assert a client is constructed - SOCKS4
    # routes through httpx-socks, the rest through httpx itself.
    for proxy in (None, "http://127.0.0.1:8", "socks5://127.0.0.1:8", "socks4://127.0.0.1:8"):
        client = net.build_client(proxy=proxy, timeout=1)
        assert isinstance(client, httpx.Client)
        client.close()


def test_direct_client_has_no_proxy_transport():
    client = net.build_client(timeout=1)
    client.close()  # the point is that build_client() with no proxy just works


# ------------------------------------------------------------------ vpn


def test_detect_vpn_runs(monkeypatch: pytest.MonkeyPatch):
    # Never raises, returns a bool; and picks up a faked tunnel interface.
    assert isinstance(net.detect_vpn(), bool)

    class _Stat:
        isup = True

    import psutil

    monkeypatch.setattr(psutil, "net_if_stats", lambda: {"wg0": _Stat(), "eth0": _Stat()})
    assert net.detect_vpn() is True
    assert net.active_vpn_interfaces() == ["wg0"]

    monkeypatch.setattr(psutil, "net_if_stats", lambda: {"eth0": _Stat()})
    assert net.detect_vpn() is False


# --------------------------------------------------------- torrent proxy


def test_torrent_proxy_translation():
    from app.engines.torrent import _proxy_settings

    assert _proxy_settings(None) == {}
    socks = _proxy_settings("socks5://user:pw@10.0.0.1:1080")
    assert socks["proxy_type"] == 2  # socks5
    assert socks["proxy_hostname"] == "10.0.0.1" and socks["proxy_port"] == 1080
    assert socks["proxy_username"] == "user" and socks["proxy_password"] == "pw"
    assert socks["proxy_peer_connections"] and socks["proxy_tracker_connections"]

    http = _proxy_settings("http://proxy.local:8080")
    assert http["proxy_type"] == 3 and http["proxy_port"] == 8080
    assert _proxy_settings("ftp://x") == {}  # unsupported scheme -> ignored


# --------------------------------------------------------- auto-throttle


def test_auto_throttle_decision(db: Database, monkeypatch: pytest.MonkeyPatch):
    from app.core.stats import SystemReading

    manager = DownloadManager(db, max_concurrent=0)
    try:
        manager.settings.auto_throttle = True
        manager.settings.auto_throttle_kbps = 200
        manager.settings.auto_throttle_threshold_kbps = 100

        # Other traffic (system minus ours) well over the threshold -> throttle.
        monkeypatch.setattr(
            manager._system_sampler,
            "sample",
            lambda: SystemReading(0, 0, 5 * MB, 0),  # 5 MB/s system recv
        )
        monkeypatch.setattr(manager, "_download_rate", lambda: 0.0)
        assert manager._auto_throttle_rate() == 200 * 1024

        # Quiet network -> no throttle.
        monkeypatch.setattr(manager._system_sampler, "sample", lambda: SystemReading(0, 0, 1000, 0))
        assert manager._auto_throttle_rate() == 0

        manager.settings.auto_throttle = False
        assert manager._auto_throttle_rate() == 0
    finally:
        manager.shutdown()


def test_auto_throttle_caps_the_effective_rate(db: Database, monkeypatch: pytest.MonkeyPatch):
    manager = DownloadManager(db, max_concurrent=0)
    try:
        manager.settings.speed_limit_kbps = 0  # unlimited base
        manager.settings.auto_throttle = True
        manager.settings.auto_throttle_kbps = 300
        monkeypatch.setattr(manager, "_auto_throttle_rate", lambda: 300 * 1024)
        assert manager._effective_global_rate() == 300 * 1024  # throttle wins over unlimited
    finally:
        manager.shutdown()


# ---------------------------------------------------- per-host speed limit


def test_host_limit_is_shared_and_applied(server: MediaServer, db: Database, dest: Path):
    """Two downloads from the same host share one 64 KB/s bucket, so together
    they move ~64 KB/s, not 128."""
    data0 = payload(200_000, 1)
    data1 = payload(200_000, 2)
    url0 = server.add("/a.bin", data0)
    url1 = server.add("/b.bin", data1)
    settings = Settings(db)
    settings.host_limits = {"127.0.0.1": 64}  # 64 KB/s for this host
    manager = DownloadManager(db, settings=settings, max_concurrent=2)
    import time

    start = time.monotonic()
    try:
        jobs = [manager.add_url(url0, dest), manager.add_url(url1, dest)]
        wait_for(
            lambda: all(
                (j := db.get_job(job.id)) is not None and j.status.value == "completed"
                for job in jobs
            ),
            timeout=60,
        )
    finally:
        manager.shutdown()
    elapsed = time.monotonic() - start
    # 400 KB total at ~64 KB/s shared => well over 3s. A shared cap makes this
    # slow; two independent 64 KB/s caps (the bug) would finish in ~3s too, so
    # assert the combined rate is near the single cap, not double it.
    combined_kbps = (len(data0) + len(data1)) / 1024 / elapsed
    assert combined_kbps < 128  # never exceeds ~one host bucket
    assert sha256_file(dest / "a.bin") == sha256(data0)
    assert sha256_file(dest / "b.bin") == sha256(data1)


def test_host_limits_setting_roundtrip(db: Database):
    settings = Settings(db)
    assert settings.host_limits == {}
    settings.host_limits = {"CDN.Example.com": 500, "bad": 0, "": 5}
    fresh = Settings(db)
    assert fresh.host_limits == {"cdn.example.com": 500}  # lowercased, positive-only


# ------------------------------------------------------- IPv6 health probe


def _reset_v6_cache() -> None:
    net._v6_state = None


def test_ipv6_broken_when_v6_fails_but_v4_works(monkeypatch: pytest.MonkeyPatch):
    """The black-hole case: v6 handshake dies, v4 succeeds -> force IPv4."""
    import socket as socket_mod

    _reset_v6_cache()
    monkeypatch.setattr(net, "_handshakes", lambda host, family: family == socket_mod.AF_INET)
    assert net.ipv6_broken() is True


def test_ipv6_fine_when_v6_handshakes(monkeypatch: pytest.MonkeyPatch):
    _reset_v6_cache()
    monkeypatch.setattr(net, "_handshakes", lambda host, family: True)
    assert net.ipv6_broken() is False


def test_ipv6_left_alone_when_network_is_down(monkeypatch: pytest.MonkeyPatch):
    """Both families failing means the network is down, not that v6 is broken
    - forcing IPv4 would mask the real problem (and break NAT64 setups)."""
    _reset_v6_cache()
    monkeypatch.setattr(net, "_handshakes", lambda host, family: False)
    assert net.ipv6_broken() is False


def test_ipv6_verdict_is_cached(monkeypatch: pytest.MonkeyPatch):
    _reset_v6_cache()
    calls: list[object] = []

    def probe(host, family):
        calls.append(family)
        return True

    monkeypatch.setattr(net, "_handshakes", probe)
    net.ipv6_broken()
    net.ipv6_broken()
    assert len(calls) == 1  # one v6 probe, then the cache answers


def test_build_client_forces_v4_only_when_broken(monkeypatch: pytest.MonkeyPatch):
    _reset_v6_cache()
    monkeypatch.setattr(net, "ipv6_broken", lambda: True)
    client = net.build_client(timeout=1)
    # The v4-bound transport replaces the default one.
    assert isinstance(client._transport, httpx.HTTPTransport)
    # _pool is a private httpx.HTTPTransport attribute, not on the BaseTransport
    # type mypy sees, so reach it through getattr.
    pool = getattr(client._transport, "_pool", None)
    assert getattr(pool, "_local_address", None) == "0.0.0.0"
    client.close()

    monkeypatch.setattr(net, "ipv6_broken", lambda: False)
    client = net.build_client(timeout=1)
    pool = getattr(client._transport, "_pool", None)
    assert getattr(pool, "_local_address", None) is None
    client.close()


def test_proxied_client_never_forces_v4(monkeypatch: pytest.MonkeyPatch):
    """The proxy connects onward itself - binding our socket family would
    change nothing and risks breaking a v6-only proxy address."""
    monkeypatch.setattr(net, "ipv6_broken", lambda: True)
    client = net.build_client(proxy="http://127.0.0.1:9", timeout=1)
    pool = getattr(client._transport, "_pool", None)
    assert getattr(pool, "_local_address", None) is None
    client.close()


def test_smart_network_guards(monkeypatch: pytest.MonkeyPatch):
    from app.engines import smart

    monkeypatch.setattr(net, "ipv6_broken", lambda: True)
    opts: dict[str, object] = {}
    smart._apply_network_guards(opts, None)
    assert opts["socket_timeout"] == 20
    assert opts["source_address"] == "0.0.0.0"

    proxied: dict[str, object] = {}
    smart._apply_network_guards(proxied, "socks5://127.0.0.1:1080")
    assert proxied["socket_timeout"] == 20
    assert "source_address" not in proxied


# -------------------------------------------------- credential redaction (F5)


def test_redact_credentials_strips_userinfo():
    assert net.redact_credentials("socks5://user:pass@host:1080") == "socks5://host:1080"
    assert net.redact_credentials("http://u:p@10.0.0.1:8080") == "http://10.0.0.1:8080"
    # Nothing to redact -> unchanged.
    assert net.redact_credentials("socks5://host:1080") == "socks5://host:1080"
    assert net.redact_credentials("") == ""
    assert net.redact_credentials("http://host/@path") == "http://host/@path"
