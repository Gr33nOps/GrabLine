"""Real reputation-service errors and bounded public DHT/gateway discovery."""

from __future__ import annotations

import hashlib
import json
import socket
import struct
import time
from unittest.mock import patch

import httpx
from common import ROOT, Blocked, case, record, save

from app.core import reputation
from app.engines.torrent import _DHT_ROUTERS, _lt

for provider, function, args in (
    (
        "VIRUSTOTAL",
        reputation.virustotal_lookup,
        (hashlib.sha256(b"GrabLine disposable QA fixture").hexdigest(), "qa-invalid-key"),
    ),
    (
        "SAFEBROWSING",
        reputation.safebrowsing_check,
        ("https://example.com/grabline-disposable-qa", "qa-invalid-key"),
    ),
):
    with case(
        "EXTERNAL-" + provider + "-REAL-ERROR", "production function + genuine external HTTPS"
    ) as e:
        observed = []
        method = "get" if provider == "VIRUSTOTAL" else "post"
        actual = getattr(httpx, method)

        def observe(*a, actual=actual, observed=observed, **kw):
            response = actual(*a, **kw)
            observed.append(response.status_code)
            return response

        with patch.object(httpx, method, observe):
            result = function(*args)
        if not observed:
            raise Blocked(
                "No HTTP response from the real service; TLS/network prerequisite unavailable"
            )
        assert observed[0] in (400, 401, 403), observed
        assert result is None
        e.update(
            http_status=observed[0],
            rejected_synthetic_key=True,
            submitted="synthetic fixture hash"
            if provider == "VIRUSTOTAL"
            else "synthetic example.com URL",
        )
    record(
        "EXTERNAL-" + provider + "-VALID-KEY",
        "BLOCKED",
        "live authenticated provider",
        "No dedicated API key configured; user chose error checks only.",
    )

with case("TORRENT-PUBLIC-DHT", "real libtorrent UDP bootstrap + DHT/session alerts") as e:
    lt = _lt()
    mask = lt.alert.category_t.all_categories
    session = lt.session(
        {
            "listen_interfaces": "0.0.0.0:0",
            "enable_dht": True,
            "enable_upnp": False,
            "enable_natpmp": False,
            "enable_lsd": False,
            "dht_bootstrap_nodes": _DHT_ROUTERS,
            "alert_mask": mask,
        }
    )
    samples = []
    alerts = []
    deadline = time.monotonic() + 75
    try:
        while time.monotonic() < deadline:
            session.post_session_stats()
            session.post_dht_stats()
            session.wait_for_alert(500)
            for alert in session.pop_alerts():
                if isinstance(alert, lt.session_stats_alert):
                    values = alert.values
                    samples.append({k: v for k, v in values.items() if k.startswith("dht.")})
                if isinstance(alert, lt.dht_bootstrap_alert):
                    alerts.append("dht_bootstrap_alert")
                if isinstance(alert, lt.dht_stats_alert):
                    nodes = sum(b["num_nodes"] for b in alert.routing_table)
                    if nodes:
                        e["routing_nodes"] = nodes
            save(
                "public-dht-samples.json",
                {"samples": samples, "alerts": alerts, "listen_port": session.listen_port()},
            )
            if e.get("routing_nodes", 0) > 0:
                break
        e.update(listen_port=session.listen_port(), bootstrap_completed=bool(alerts))
        if not e.get("routing_nodes", 0):
            raise Blocked(
                "No public DHT peers learned within 75 seconds; UDP reachability is not established"
            )
    finally:
        session.pause()
        session = None

with case(
    "ROUTER-UPNP-DISCOVERY", "real SSDP multicast discovery; no router settings changed"
) as e:
    message = (
        b"M-SEARCH * HTTP/1.1\r\nHOST:239.255.255.250:1900\r\n"
        b'MAN:"ssdp:discover"\r\nMX:2\r\n'
        b"ST:urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n"
    )
    replies = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(1)
        sock.sendto(message, ("239.255.255.250", 1900))
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            try:
                data, _ = sock.recvfrom(65536)
                replies.append(data.decode("utf-8", "replace"))
            except TimeoutError:
                continue
    e["gateway_responses"] = len(replies)
    if not replies:
        raise Blocked(
            "No UPnP Internet Gateway Device answered on this LAN; mapping cannot be certified"
        )

# The default gateway is supplied by read-only Windows route inspection.
gateway_file = ROOT / "gateway.json"
with case(
    "ROUTER-NATPMP-DISCOVERY", "real gateway UDP query; no port mapping or router settings changed"
) as e:
    if not gateway_file.exists():
        raise Blocked("No default gateway discovered")
    gateway = json.loads(gateway_file.read_text(encoding="utf-8-sig"))["NextHop"]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(3)
        sock.sendto(b"\x00\x00", (gateway, 5351))
        try:
            data, peer = sock.recvfrom(64)
        except (TimeoutError, OSError) as exc:
            raise Blocked("Gateway did not answer NAT-PMP public-address query") from exc
    assert peer[0] == gateway and len(data) >= 12
    version, op, result = struct.unpack("!BBH", data[:4])
    assert version == 0 and op == 128
    e.update(result_code=result, public_address_reported=result == 0)
    if result:
        raise Blocked(f"Gateway rejected NAT-PMP query with result {result}")

record(
    "ROUTER-EXTERNAL-INBOUND-MAPPING",
    "BLOCKED",
    "physical gateway and off-LAN client",
    (
        "Discovery is separate from verified inbound reachability and cleanup; "
        "dedicated external peer required."
    ),
)
