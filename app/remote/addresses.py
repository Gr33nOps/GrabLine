"""Which local addresses the listener binds and which peers it accepts.

PROTOCOL.md section 3: one socket per local address that is private or
link-local (``10/8``, ``172.16/12``, ``192.168/16``, ``169.254/16``,
``fc00::/7``, ``fe80::/10``), never a wildcard or a public address, and a
peer outside those ranges is dropped before the TLS handshake. Loopback
(``127.0.0.1`` only) is bound and accepted only in loopback test mode, where
the QR also lists ``10.0.2.2`` (the Android emulator's name for this PC).

The classification is plain ``ipaddress`` and Qt-free; only
:func:`bind_addresses` asks Qt for the interface list.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Sequence

LOOPBACK = "127.0.0.1"
EMULATOR_HOST = "10.0.2.2"
MAX_ADVERTISED = 8

_ALLOWED_V4 = tuple(
    ipaddress.IPv4Network(net)
    for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")
)
_UNIQUE_LOCAL = ipaddress.IPv6Network("fc00::/7")
_LINK_LOCAL_V6 = ipaddress.IPv6Network("fe80::/10")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def parse(address: object) -> tuple[IPAddress, str] | None:
    """``(ip, zone)`` for an address string, or None if it is not one.

    Accepts a zone id (``fe80::1%eth0``) and brackets. An IPv4-mapped IPv6
    address is returned as its IPv4 address, so it is judged by the IPv4 rules.
    """
    if not isinstance(address, str):
        return None
    text = address.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    text, _, zone = text.partition("%")
    try:
        ip: IPAddress = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped, ""
    return ip, zone


def _is_private_or_link_local(ip: IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv4Address):
        return any(ip in net for net in _ALLOWED_V4)
    return ip in _UNIQUE_LOCAL or ip in _LINK_LOCAL_V6


def is_client_target(address: object) -> bool:
    """True for an address a link may connect to when the host itself supplied
    it (``authOk.addresses``): an IP literal that is private, link-local or
    loopback. Host names and public addresses are refused, so a host cannot
    steer this PC to somewhere else on the network or the internet."""
    parsed = parse(address)
    if parsed is None:
        return False
    ip = parsed[0]
    return ip.is_loopback or _is_private_or_link_local(ip)


def is_bindable(address: object, test_mode: bool = False) -> bool:
    """True if the listener may open a socket on this local address."""
    parsed = parse(address)
    if parsed is None:
        return False
    ip, zone = parsed
    if test_mode and str(ip) == LOOPBACK:
        return True
    if not _is_private_or_link_local(ip):
        return False
    # A link-local IPv6 address cannot be bound without its interface.
    return not (isinstance(ip, ipaddress.IPv6Address) and ip in _LINK_LOCAL_V6 and not zone)


def is_allowed_peer(address: object, test_mode: bool = False) -> bool:
    """True if a connection from this remote address may be served."""
    parsed = parse(address)
    if parsed is None:
        return False
    ip = parsed[0]
    if str(ip) == LOOPBACK:
        return test_mode
    return _is_private_or_link_local(ip)


def _rank(address: str) -> int:
    parsed = parse(address)
    if parsed is None:
        return 3
    ip = parsed[0]
    if isinstance(ip, ipaddress.IPv4Address):
        return 0
    return 2 if ip in _LINK_LOCAL_V6 else 1


def select_bind_addresses(candidates: Iterable[str], test_mode: bool = False) -> list[str]:
    """The addresses to listen on, out of ``candidates``: IPv4 first,
    link-local IPv6 last, no duplicates, loopback only in test mode."""
    chosen: list[str] = []
    for candidate in candidates:
        if is_bindable(candidate, test_mode) and candidate not in chosen:
            chosen.append(candidate)
    chosen.sort(key=_rank)  # stable
    if test_mode:
        # First, so that a test's port 0 is picked on the loopback socket.
        chosen = [LOOPBACK, *(a for a in chosen if a != LOOPBACK)]
    return chosen


def bind_addresses(test_mode: bool = False) -> list[str]:
    """This PC's private and link-local addresses, read from Qt just now.

    Link-local IPv6 entries carry their scope id (``fe80::1%ethernet_3``).
    Interfaces that are down are skipped.
    """
    from PySide6.QtNetwork import QNetworkInterface

    flags = QNetworkInterface.InterfaceFlag
    candidates: list[str] = []
    for interface in QNetworkInterface.allInterfaces():
        state = interface.flags()
        if not (state & flags.IsUp) or not (state & flags.IsRunning):
            continue
        candidates.extend(entry.ip().toString() for entry in interface.addressEntries())
    return select_bind_addresses(candidates, test_mode)


def advertised(bound: Sequence[str], test_mode: bool = False) -> list[str]:
    """What the QR and ``authOk.addresses`` list for these bound addresses:
    no zone id, IPv6 in brackets, IPv4 first, link-local IPv6 last, at most 8.

    Loopback is never listed (a phone cannot use it); in test mode the
    emulator's ``10.0.2.2`` is listed first instead.
    """
    listed: list[str] = [EMULATOR_HOST] if test_mode else []
    for address in sorted(bound, key=_rank):
        parsed = parse(address)
        if parsed is None or str(parsed[0]) == LOOPBACK:
            continue
        ip = parsed[0]
        text = str(ip) if isinstance(ip, ipaddress.IPv4Address) else f"[{ip}]"
        if text not in listed:
            listed.append(text)
    return listed[:MAX_ADVERTISED]
