"""A cheap "is the internet reachable?" probe for the wait-for-network
setting: a TCP handshake to well-known public DNS servers, no data sent.
"""

from __future__ import annotations

import socket

_PROBES = (("1.1.1.1", 53), ("8.8.8.8", 53))


def is_online(timeout: float = 1.5) -> bool:
    for host, port in _PROBES:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False
