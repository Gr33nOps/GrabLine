"""Select a loopback port available to both torrent transports on Windows."""

from __future__ import annotations

import secrets
import socket


def torrent_port() -> int:
    # Hyper-V may reserve a UDP range that differs from TCP's exclusions.
    # A TCP-only ephemeral-port selection does not establish UDP availability.
    for _ in range(200):
        with socket.socket() as tcp, socket.socket(type=socket.SOCK_DGRAM) as udp:
            # Sequential ephemeral allocation can stay inside one large UDP
            # exclusion for hundreds of attempts. Probe independent candidates.
            port = 20000 + secrets.randbelow(20000)
            try:
                tcp.bind(("127.0.0.1", port))
                udp.bind(("127.0.0.1", port))
            except OSError:
                continue
            return int(port)
    raise OSError("could not select a loopback port available for TCP and UDP")
