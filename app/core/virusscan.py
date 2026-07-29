"""Best-effort malware scan before extraction, using a scanner already on the
machine - Windows Defender (MpCmdRun.exe) or ClamAV (clamdscan/clamscan).
GrabLine is not an antivirus; when no scanner is installed and the setting is
on, extraction stops with a message saying so rather than pretending to scan.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from app.core import proc
from app.core.errors import DownloadError

#: Defender exits 2 when threats were found; ClamAV exits 1.
_INFECTED_CODES = {"Windows Defender": 2, "ClamAV": 1}


@dataclass(frozen=True)
class ScanResult:
    clean: bool
    scanner: str
    detail: str = ""


def find_scanner(pref: str = "auto") -> tuple[str, list[str]] | None:
    """(display name, command prefix) of the first scanner found, or None.
    ``pref`` pins one scanner ("defender" / "clamav"); "auto" takes the first."""
    if pref == "clamav":
        return _find_clamav()
    if pref == "defender":
        return _find_defender()
    return _find_defender() or _find_clamav()


def _find_defender() -> tuple[str, list[str]] | None:
    if sys.platform == "win32":
        for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMW6432")):
            if not base:
                continue
            exe = Path(base) / "Windows Defender" / "MpCmdRun.exe"
            if exe.is_file():
                return (
                    "Windows Defender",
                    [str(exe), "-Scan", "-ScanType", "3", "-DisableRemediation", "-File"],
                )
    return None


def _find_clamav() -> tuple[str, list[str]] | None:
    if tool := shutil.which("clamdscan"):
        # --fdpass lets the daemon read files it has no permission for.
        return ("ClamAV", [tool, "--no-summary", "--fdpass", "--"])
    if tool := shutil.which("clamscan"):
        return ("ClamAV", [tool, "--no-summary", "--"])
    return None


def scan(path: Path, pref: str = "auto") -> ScanResult:
    """Scan one file. Raises DownloadError when no scanner is installed or the
    scan itself fails - never silently passes a file it could not check."""
    found = find_scanner(pref)
    if found is None:
        raise DownloadError(
            "no virus scanner was found - install ClamAV (or use Windows "
            "Defender on Windows), or turn off scanning in Settings."
        )
    name, prefix = found
    try:
        result = subprocess.run(  # arg list only, never a shell string (S1)
            [*prefix, str(path)],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=600,
            **proc.hidden(),
        )
    except subprocess.TimeoutExpired as exc:
        raise DownloadError(f"the {name} scan timed out") from exc
    if result.returncode == 0:
        return ScanResult(clean=True, scanner=name)
    if result.returncode == _INFECTED_CODES[name]:
        lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
        return ScanResult(clean=False, scanner=name, detail=lines[-1] if lines else "")
    tail = (result.stderr or result.stdout or "").strip().splitlines()[-1:] or ["unknown error"]
    raise DownloadError(f"the {name} scan could not run ({tail[0]})")
