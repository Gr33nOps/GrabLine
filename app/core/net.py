"""Networking helpers: a central httpx client factory that understands every
proxy scheme, proxy-URL validation, and a best-effort VPN check.

httpx speaks HTTP, HTTPS and SOCKS5 (with ``socksio``) natively; SOCKS4/4a it
does not, so those route through an httpx-socks transport. Every httpx client
in the app is built here so one proxy setting covers all of them.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

#: Proxy schemes GrabLine accepts.
PROXY_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4", "socks4a")
_SOCKS4 = ("socks4", "socks4a")

#: Dual-stacked host the v6 health probe handshakes against. It must be a
#: destination the app actually talks to: v6 brokenness is per-route (peering,
#: tunnels), so a generic anycast host can answer fine while this one's v6
#: packets vanish - the exact case observed in the wild.
_V6_PROBE_HOST = "www.youtube.com"
_V6_PROBE_TIMEOUT = 1.5
_V6_RECHECK = 600.0  # networks change (wifi roaming, VPN up/down)
#: How long the very first caller will wait for a verdict. Later callers never
#: wait at all - they use the cached answer while a refresh runs behind them.
_V6_FIRST_WAIT = 2.0
_v6_lock = threading.Lock()
_v6_state: tuple[float, bool] | None = None  # (checked at, force IPv4?)
_v6_probe_done: threading.Event | None = None  # set when an in-flight probe lands


def _handshakes(host: str, family: socket.AddressFamily) -> bool:
    """Can a TCP handshake to ``host`` complete over this address family?"""
    try:
        infos = socket.getaddrinfo(host, 443, family, socket.SOCK_STREAM)
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.settimeout(_V6_PROBE_TIMEOUT)
            probe.connect(infos[0][4])
        return True
    except OSError:
        return False


def _run_v6_probe(done: threading.Event) -> None:
    global _v6_state
    try:
        broken = not _handshakes(_V6_PROBE_HOST, socket.AF_INET6) and _handshakes(
            _V6_PROBE_HOST, socket.AF_INET
        )
    except Exception:  # a probe must never take a thread down with it
        log.debug("IPv6 probe failed", exc_info=True)
        broken = False
    else:
        if broken:
            log.info("IPv6 to %s is unusable - forcing IPv4 connections", _V6_PROBE_HOST)
    with _v6_lock:
        _v6_state = (time.monotonic(), broken)
    done.set()


def ipv6_broken() -> bool:
    """True when connections should be forced onto IPv4.

    The failure this exists for: the OS resolves AAAA records and routes v6,
    but the v6 SYNs to a host vanish into a black hole. Neither httpx nor
    yt-dlp's urllib handler do happy-eyeballs, so each request serially times
    out per v6 address before reaching v4 - measured 62s for one YouTube page
    fetch on such a network, which is what "analysis is stuck" turns out to be.

    Broken means: v6 to the probe host fails while v4 to the same host
    succeeds - so a machine with no v6 at all (v6 fails instantly, but so would
    any app; the OS skips it) still counts, harmlessly, and a v6-only network
    (v4 fails too) is left alone.

    The probe runs on its own thread and this call never blocks on it beyond a
    short first-call grace period. That matters because ``getaddrinfo`` has no
    timeout of its own: on a network with a hung resolver the probe can sit for
    the OS resolver's patience, and this function is on the path of every
    client build - i.e. every download and every analysis. Callers get the last
    known answer (or "not broken") instead of inheriting that stall.
    """
    with _v6_lock:
        state = _v6_state
        if state is not None and time.monotonic() - state[0] < _V6_RECHECK:
            return state[1]
        done = _start_v6_probe_locked()
    if state is not None:
        return state[1]  # stale but serviceable; the refresh lands behind us
    done.wait(_V6_FIRST_WAIT)
    with _v6_lock:
        return _v6_state[1] if _v6_state is not None else False


def _start_v6_probe_locked() -> threading.Event:
    """Start a probe unless one is already in flight. Caller holds _v6_lock."""
    global _v6_probe_done
    done = _v6_probe_done
    if done is not None and not done.is_set():
        return done
    done = threading.Event()
    _v6_probe_done = done
    threading.Thread(target=_run_v6_probe, args=(done,), name="gl-v6probe", daemon=True).start()
    return done


def validate_proxy(url: str) -> str | None:
    """None if ``url`` is a usable proxy address (or blank), else a message."""
    url = url.strip()
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in PROXY_SCHEMES:
        return f"Proxy must start with one of: {', '.join(s + '://' for s in PROXY_SCHEMES)}"
    if not parts.hostname:
        return "The proxy address needs a host, e.g. socks5://127.0.0.1:1080"
    return None


def redact_credentials(url: str) -> str:
    """Strip any ``user:pass@`` from a URL, keeping the rest intact.

    A proxy address can embed credentials (``socks5://user:pass@host:1080``).
    Those are a stored secret, so anything that leaves the machine - a settings
    export, a diagnostics dump, a log line - must not carry them (CWE-522).
    Returns the URL unchanged when there is no userinfo. Never raises.
    """
    if not url or "@" not in url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.username:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return parts._replace(netloc=host).geturl()


def _client_kwargs(proxy: str | None) -> dict[str, Any]:
    """httpx.Client kwargs that apply ``proxy``. SOCKS4/4a get an httpx-socks
    transport; everything else uses httpx's own proxy support."""
    if not proxy:
        return {}
    scheme = urlsplit(proxy).scheme
    if scheme in _SOCKS4:
        from httpx_socks import SyncProxyTransport

        return {"transport": SyncProxyTransport.from_url(proxy)}
    return {"proxy": proxy}


def build_client(
    *,
    proxy: str | None = None,
    bypass_hosts: tuple[str, ...] = (),
    user_agent: str | None = None,
    **kwargs: Any,
) -> httpx.Client:
    """An httpx.Client honoring ``proxy`` for any supported scheme.

    ``bypass_hosts`` connect directly even with a proxy set; ``user_agent``
    overrides the default UA header for every request from this client."""
    client_kwargs = _client_kwargs(proxy)
    if proxy and bypass_hosts:
        mounts = dict(client_kwargs.get("mounts") or {})
        for host in bypass_hosts:
            mounts[f"all://{host}"] = httpx.HTTPTransport()
            mounts[f"all://*.{host}"] = httpx.HTTPTransport()
        client_kwargs["mounts"] = mounts
    if not proxy and ipv6_broken():
        # Direct connections on a black-holed-v6 network: bind IPv4 so no
        # request waits out a v6 timeout first. (With a proxy, the proxy does
        # the onward connecting and this is its problem, not ours.)
        client_kwargs["transport"] = httpx.HTTPTransport(
            local_address="0.0.0.0", http2=bool(kwargs.pop("http2", False))
        )
    if user_agent:
        headers = dict(kwargs.pop("headers", None) or {})
        headers.setdefault("User-Agent", user_agent)
        kwargs["headers"] = headers
    return httpx.Client(**client_kwargs, **kwargs)


#: Interface-name fragments that strongly suggest a VPN / tunnel is up.
_VPN_HINTS = ("tun", "tap", "wg", "ppp", "utun", "ipsec", "nordlynx", "proton", "mullvad", "wintun")


def detect_vpn() -> bool:
    """True when a VPN-like network interface appears to be up. A heuristic -
    it looks for tunnel adapters (WireGuard, OpenVPN, IKEv2, ...), so it is a
    hint, not a guarantee. Never raises."""
    try:
        import psutil

        stats = psutil.net_if_stats()
    except Exception:  # pragma: no cover - no psutil / permissions
        return False
    for name, info in stats.items():
        lowered = name.lower()
        if getattr(info, "isup", False) and any(hint in lowered for hint in _VPN_HINTS):
            return True
    return False


def active_vpn_interfaces() -> list[str]:
    """The names of the up VPN-like interfaces (for the dashboard tooltip)."""
    try:
        import psutil

        stats = psutil.net_if_stats()
    except Exception:  # pragma: no cover
        return []
    return [
        name
        for name, info in stats.items()
        if getattr(info, "isup", False) and any(h in name.lower() for h in _VPN_HINTS)
    ]


def resolves(host: str) -> bool:  # pragma: no cover - trivial DNS probe
    try:
        socket.getaddrinfo(host, None)
        return True
    except OSError:
        return False
