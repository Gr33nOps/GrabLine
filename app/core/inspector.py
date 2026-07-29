"""The Download Inspector: everything GrabLine can learn about a URL from a
single live probe plus a DNS lookup and a TLS handshake - all done locally.

Deliberately no geo-IP: GrabLine has no telemetry, so it does not send the
server's address to a third-party location service. "Server location" is
presented as the real, local-only signals instead - the resolved IP, its
reverse-DNS name, and the CDN inferred from the response headers.
"""

from __future__ import annotations

import socket
import ssl
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from app.core import net

_TIMEOUT = httpx.Timeout(15.0, connect=10.0)

#: Response-header fingerprints that identify a CDN / edge provider. The first
#: match wins; each entry is (header, needle-in-lowercased-value, CDN name).
#: A needle of "" means "the header merely being present is enough".
_CDN_SIGNATURES: tuple[tuple[str, str, str], ...] = (
    ("cf-ray", "", "Cloudflare"),
    ("server", "cloudflare", "Cloudflare"),
    ("x-amz-cf-id", "", "Amazon CloudFront"),
    ("x-amz-cf-pop", "", "Amazon CloudFront"),
    ("x-served-by", "", "Fastly"),
    ("x-fastly-request-id", "", "Fastly"),
    ("x-akamai-request-id", "", "Akamai"),
    ("x-akamai-transformed", "", "Akamai"),
    ("x-azure-ref", "", "Azure CDN"),
    ("x-msedge-ref", "", "Azure CDN"),
    ("x-goog-generation", "", "Google Cloud"),
    ("server", "gws", "Google"),
    ("server", "gse", "Google"),
    ("x-github-request-id", "", "GitHub"),
    ("server", "bunnycdn", "Bunny CDN"),
    ("server", "keycdn", "KeyCDN"),
    ("x-cache", "bunnycdn", "Bunny CDN"),
    ("server", "cachefly", "CacheFly"),
    ("x-sucuri-id", "", "Sucuri"),
    ("server", "ecacc", "Edgecast"),
)


@dataclass(frozen=True)
class TlsInfo:
    version: str
    cipher: str
    subject: str
    issuer: str
    valid_from: str
    valid_until: str


@dataclass(frozen=True)
class InspectionReport:
    url: str
    final_url: str
    status: int | None = None
    reachable: bool = True
    error: str = ""
    ip_addresses: tuple[str, ...] = ()
    reverse_dns: str = ""
    cdn: str = ""
    server: str = ""
    mime_type: str = ""
    content_length: int | None = None
    response_ms: int | None = None
    headers: tuple[tuple[str, str], ...] = ()
    cookies: tuple[str, ...] = ()
    redirect_chain: tuple[tuple[int, str], ...] = ()
    tls: TlsInfo | None = None
    #: Filled in by the caller for a known job, not by the probe itself.
    mirrors: tuple[str, ...] = field(default_factory=tuple)
    checksum: str = ""


def detect_cdn(headers: httpx.Headers) -> str:
    for header, needle, name in _CDN_SIGNATURES:
        value = headers.get(header)
        if value is not None and (not needle or needle in value.lower()):
            return name
    via = headers.get("via", "")
    if via:
        return f"via {via}"
    return ""


def _resolve(host: str) -> tuple[tuple[str, ...], str]:
    """(unique IPs, reverse-DNS name of the first) - best effort, never raises."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return (), ""
    seen: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in seen:
            seen.append(address)
    reverse = ""
    if seen:
        try:
            reverse = socket.gethostbyaddr(seen[0])[0]
        except OSError:
            reverse = ""
    return tuple(seen), reverse


def tls_info(host: str, port: int = 443) -> TlsInfo | None:
    """The peer certificate and negotiated parameters, or None on any failure
    (plain http, handshake error, timeout)."""
    context = ssl.create_default_context()
    try:
        with (
            socket.create_connection((host, port), timeout=10) as raw,
            context.wrap_socket(raw, server_hostname=host) as tls,
        ):
            cert = tls.getpeercert()
            version = tls.version() or ""
            cipher = tls.cipher()
            cipher_name = cipher[0] if cipher else ""
    except (OSError, ssl.SSLError, ValueError):
        return None
    if not cert:
        return None
    return TlsInfo(
        version=version,
        cipher=cipher_name,
        subject=_name_from_cert(cert.get("subject")),
        issuer=_name_from_cert(cert.get("issuer")),
        valid_from=str(cert.get("notBefore", "")),
        valid_until=str(cert.get("notAfter", "")),
    )


def _name_from_cert(rdns: object) -> str:
    """The common name (or first attribute) from a cert's subject/issuer, which
    the ssl module hands over as a tuple of tuples of (key, value) pairs."""
    if not isinstance(rdns, (tuple, list)):
        return ""
    fields: dict[str, str] = {}
    for rdn in rdns:
        if isinstance(rdn, (tuple, list)):
            for attr in rdn:
                if isinstance(attr, (tuple, list)) and len(attr) == 2:
                    fields[str(attr[0])] = str(attr[1])
    return (
        fields.get("commonName")
        or fields.get("organizationName")
        or next(iter(fields.values()), "")
    )


def inspect_url(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    proxy: str | None = None,
) -> InspectionReport:
    """Probe ``url`` and gather everything the inspector shows. Never raises -
    an unreachable server comes back as a report with ``reachable=False``."""
    parts = urlsplit(url)
    request_headers = {"Range": "bytes=0-0", **(headers or {})}
    try:
        with net.build_client(proxy=proxy, follow_redirects=True, timeout=_TIMEOUT) as client:
            start = time.perf_counter()
            with client.stream("GET", url, headers=request_headers) as response:
                response_ms = int((time.perf_counter() - start) * 1000)
                response.read() if response.status_code == 206 else None
                final = str(response.url)
                length = response.headers.get("content-length")
                report_headers = tuple(response.headers.multi_items())
                cookies = tuple(response.headers.get_list("set-cookie"))
                chain = tuple((r.status_code, str(r.url)) for r in response.history)
                status = response.status_code
                cdn = detect_cdn(response.headers)
                server = response.headers.get("server", "")
                mime = (response.headers.get("content-type") or "").split(";")[0].strip()
    except httpx.HTTPError as exc:
        return InspectionReport(url=url, final_url=url, reachable=False, error=str(exc))

    final_host = urlsplit(final).hostname or parts.hostname or ""
    ips, reverse = _resolve(final_host)
    tls = None
    if urlsplit(final).scheme == "https" and final_host:
        tls = tls_info(final_host, urlsplit(final).port or 443)

    return InspectionReport(
        url=url,
        final_url=final,
        status=status,
        reachable=True,
        ip_addresses=ips,
        reverse_dns=reverse,
        cdn=cdn,
        server=server,
        mime_type=mime,
        content_length=int(length) if length and length.isdigit() else None,
        response_ms=response_ms,
        headers=report_headers,
        cookies=cookies,
        redirect_chain=chain,
        tls=tls,
    )
