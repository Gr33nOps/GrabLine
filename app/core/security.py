"""The security check: an advisory report about a finished download.

Design rule: this NEVER blocks or deletes. A flagged file stays exactly where
it is - GrabLine surfaces what it found and lets the user decide. Antivirus
false positives are common, so a detection is a heads-up, not a verdict.

A report gathers, best-effort and only what's configured:
  * a local virus scan (Windows Defender / ClamAV) if one is installed,
  * a VirusTotal hash lookup (opt-in, the user's key, hash-only),
  * whether the file is an executable / installer (extra caution),
  * whether it was fetched over HTTPS with a valid certificate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from urllib.parse import urlsplit

from app.core import reputation, verify, virusscan
from app.core.errors import DownloadError
from app.core.i18n import t

log = logging.getLogger(__name__)


class Risk(IntEnum):
    OK = 0
    CAUTION = 1
    WARNING = 2

    @property
    def label(self) -> str:
        return {
            Risk.OK: t("Looks OK"),
            Risk.CAUTION: t("Caution"),
            Risk.WARNING: t("Warning"),
        }[self]


#: File types that run code - worth an extra note even when nothing flags them.
_EXECUTABLE_SUFFIXES = {
    ".exe",
    ".msi",
    ".msix",
    ".bat",
    ".cmd",
    ".com",
    ".scr",
    ".ps1",
    ".vbs",
    ".js",
    ".jar",
    ".apk",
    ".dmg",
    ".pkg",
    ".deb",
    ".rpm",
    ".appimage",
    ".sh",
    ".run",
}


def is_executable(path: Path) -> bool:
    return path.suffix.lower() in _EXECUTABLE_SUFFIXES


@dataclass
class SecurityReport:
    path: str
    level: Risk = Risk.OK
    findings: list[str] = field(default_factory=list)
    checksums: dict[str, str] = field(default_factory=dict)
    scanner: str = ""
    scan_clean: bool | None = None  # None = not scanned
    scan_detail: str = ""
    virustotal: reputation.VirusTotalResult | None = None
    executable: bool = False
    https: bool | None = None  # None = unknown / not http(s)

    def _raise(self, level: Risk, finding: str) -> None:
        self.level = max(self.level, level)
        self.findings.append(finding)


def check_file(
    path: Path,
    *,
    url: str = "",
    virustotal_key: str = "",
    run_local_scan: bool = True,
    compute_checksums: bool = True,
    proxy: str | None = None,
    scanner_pref: str = "auto",
) -> SecurityReport:
    """Assemble an advisory report for a finished file. Never raises for a
    detection - a missing scanner or a network error just leaves that section
    blank."""
    report = SecurityReport(path=str(path))
    if not path.is_file():
        report._raise(Risk.WARNING, t("The file no longer exists."))
        return report

    if compute_checksums:
        report.checksums = verify.hash_all(path)

    report.executable = is_executable(path)
    if report.executable:
        report._raise(
            Risk.CAUTION,
            t("This is an executable or installer. Only run it if you trust the source."),
        )

    if url:
        scheme = urlsplit(url).scheme.lower()
        if scheme in ("http", "https"):
            report.https = scheme == "https"
            if not report.https:
                report._raise(
                    Risk.CAUTION,
                    t(
                        "Downloaded over unencrypted HTTP. It could have been "
                        "tampered with in transit."
                    ),
                )

    if run_local_scan and virusscan.find_scanner(scanner_pref) is not None:
        try:
            result = virusscan.scan(path, scanner_pref)
        except (DownloadError, OSError) as exc:
            # A scan that can't run (no scanner, subprocess error) must not sink
            # the rest of the advisory report - but it should leave a trace.
            log.debug("virus scan failed for %s: %s", path, exc)
            result = None
        if result is not None:
            report.scanner = result.scanner
            report.scan_clean = result.clean
            report.scan_detail = result.detail
            if not result.clean:
                detail = f" ({result.detail})" if result.detail else ""
                report._raise(
                    Risk.WARNING,
                    t(
                        "{scanner} flagged this file{detail}. Antivirus false "
                        "positives happen. Decide based on where it came from.",
                        scanner=result.scanner,
                        detail=detail,
                    ),
                )

    if virustotal_key:
        sha256 = report.checksums.get("sha256") or verify.hash_file(path, "sha256")
        vt = reputation.virustotal_lookup(sha256, virustotal_key, proxy=proxy)
        report.virustotal = vt
        if vt is not None and vt.flagged:
            report._raise(
                Risk.WARNING,
                t(
                    "VirusTotal: {malicious} of {total} engines flagged this file.",
                    malicious=vt.malicious,
                    total=vt.total,
                ),
            )
        elif vt is not None and vt.known and not vt.flagged:
            report.findings.append(t("VirusTotal: clean across {total} engines.", total=vt.total))

    if report.level is Risk.OK and not report.findings:
        report.findings.append(t("Nothing suspicious found."))
    return report


@dataclass(frozen=True)
class UrlAdvisory:
    insecure_http: bool = False
    threat: str = ""  # Safe Browsing threat type, if any

    @property
    def has_warning(self) -> bool:
        return self.insecure_http or bool(self.threat)


def check_url(
    url: str,
    *,
    enforce_https: bool = False,
    safebrowsing_key: str = "",
    proxy: str | None = None,
) -> UrlAdvisory:
    """A pre-download advisory for a URL: plain-HTTP (if the user asked to be
    warned) and a Safe Browsing match (if a key is configured). Advisory only -
    the caller warns and lets the user proceed."""
    scheme = urlsplit(url).scheme.lower()
    insecure = enforce_https and scheme == "http"
    threat = ""
    if safebrowsing_key and scheme in ("http", "https"):
        threat = reputation.safebrowsing_check(url, safebrowsing_key, proxy=proxy) or ""
    return UrlAdvisory(insecure_http=insecure, threat=threat)
