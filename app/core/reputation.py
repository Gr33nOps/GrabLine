"""Optional online reputation checks - VirusTotal and Google Safe Browsing.

Both are opt-in and use the user's own API key. Privacy is the point:

* VirusTotal is queried by the file's SHA-256 hash only - the file contents
  are never uploaded. A hash reveals nothing about the file unless someone
  already has an identical copy.
* Safe Browsing is queried by URL. That is more revealing, so it is off by
  default, needs the user's own Google key, and is clearly labelled.

Neither is used unless the user configures a key. Results are advisory: they
never block or delete anything - the UI shows them and the user decides.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

_VT_URL = "https://www.virustotal.com/api/v3/files/{}"
_SB_URL = "https://safebrowsing.googleapis.com/v4/threatMatches:find?key={}"
_TIMEOUT = httpx.Timeout(15.0, connect=10.0)


@dataclass(frozen=True)
class VirusTotalResult:
    malicious: int
    suspicious: int
    total: int  # engines that returned a verdict
    known: bool  # False = VirusTotal has never seen this hash
    permalink: str = ""

    @property
    def flagged(self) -> bool:
        return self.malicious > 0 or self.suspicious > 0


def virustotal_lookup(
    sha256: str, api_key: str, *, proxy: str | None = None
) -> VirusTotalResult | None:
    """Look a file up on VirusTotal by hash. None on any error / no key.

    Sends only the hash. A 404 means the file is unknown to VirusTotal (not an
    error) - reported as ``known=False``."""
    if not api_key or not sha256:
        return None
    try:
        response = httpx.get(
            _VT_URL.format(sha256),
            headers={"x-apikey": api_key},
            timeout=_TIMEOUT,
            proxy=proxy or None,
        )
    except httpx.HTTPError as exc:
        log.info("VirusTotal lookup failed: %s", exc)
        return None
    if response.status_code == 404:
        return VirusTotalResult(malicious=0, suspicious=0, total=0, known=False)
    if response.status_code != 200:
        log.info("VirusTotal returned HTTP %s", response.status_code)
        return None
    try:
        stats = response.json()["data"]["attributes"]["last_analysis_stats"]
    except (KeyError, ValueError):
        return None
    malicious = int(stats.get("malicious", 0))
    suspicious = int(stats.get("suspicious", 0))
    total = sum(int(v) for v in stats.values())
    return VirusTotalResult(
        malicious=malicious,
        suspicious=suspicious,
        total=total,
        known=True,
        permalink=f"https://www.virustotal.com/gui/file/{sha256}",
    )


def safebrowsing_check(url: str, api_key: str, *, proxy: str | None = None) -> str | None:
    """The threat type if Google Safe Browsing flags ``url`` (e.g.
    "MALWARE", "SOCIAL_ENGINEERING"), else None. Errors return None."""
    if not api_key or not url:
        return None
    body = {
        "client": {"clientId": "grabline", "clientVersion": "1"},
        "threatInfo": {
            "threatTypes": [
                "MALWARE",
                "SOCIAL_ENGINEERING",
                "UNWANTED_SOFTWARE",
                "POTENTIALLY_HARMFUL_APPLICATION",
            ],
            "platformTypes": ["ANY_PLATFORM"],
            "threatEntryTypes": ["URL"],
            "threatEntries": [{"url": url}],
        },
    }
    try:
        response = httpx.post(
            _SB_URL.format(api_key), json=body, timeout=_TIMEOUT, proxy=proxy or None
        )
    except httpx.HTTPError as exc:
        log.info("Safe Browsing lookup failed: %s", exc)
        return None
    if response.status_code != 200:
        return None
    try:
        matches = response.json().get("matches") or []
    except ValueError:
        return None
    if matches:
        return str(matches[0].get("threatType", "THREAT"))
    return None
