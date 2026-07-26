"""The security wave: multi-algorithm checksums, the VirusTotal / Safe
Browsing clients (parsing, never uploading the file), and the advisory
SecurityReport - which flags risk but never blocks.
"""

from __future__ import annotations

import hashlib
import zlib
from pathlib import Path

import httpx
import pytest

from app.core import reputation, security, verify
from app.core.security import Risk

# ------------------------------------------------------------- checksums


def test_hash_all_algorithms(tmp_path: Path):
    payload = b"grabline security" * 100
    target = tmp_path / "f.bin"
    target.write_bytes(payload)
    digests = verify.hash_all(target)
    assert digests["md5"] == hashlib.md5(payload).hexdigest()
    assert digests["sha1"] == hashlib.sha1(payload).hexdigest()
    assert digests["sha256"] == hashlib.sha256(payload).hexdigest()
    assert digests["sha512"] == hashlib.sha512(payload).hexdigest()
    assert digests["crc32"] == f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}"


def test_guess_algorithm_by_length():
    assert verify.guess_algorithm("a" * 8) == "crc32"
    assert verify.guess_algorithm("a" * 32) == "md5"
    assert verify.guess_algorithm("a" * 40) == "sha1"
    assert verify.guess_algorithm("a" * 64) == "sha256"
    assert verify.guess_algorithm("a" * 128) == "sha512"


def test_verify_file_autodetects(tmp_path: Path):
    target = tmp_path / "f.bin"
    target.write_bytes(b"data")
    assert verify.verify_file(target, hashlib.sha512(b"data").hexdigest())
    assert verify.verify_file(target, f"{zlib.crc32(b'data') & 0xFFFFFFFF:08x}")
    assert not verify.verify_file(target, "deadbeef")


# ---------------------------------------------------- reputation clients


def _mock_get(monkeypatch, status: int, payload: dict[str, object]):
    def fake_get(url, **kwargs):
        return httpx.Response(status, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr("app.core.reputation.httpx.get", fake_get)


def test_virustotal_parses_stats(monkeypatch: pytest.MonkeyPatch):
    _mock_get(
        monkeypatch,
        200,
        {
            "data": {
                "attributes": {
                    "last_analysis_stats": {
                        "malicious": 3,
                        "suspicious": 1,
                        "harmless": 60,
                        "undetected": 6,
                    }
                }
            }
        },
    )
    result = reputation.virustotal_lookup("a" * 64, "key")
    assert result is not None
    assert result.malicious == 3 and result.suspicious == 1
    assert result.total == 70 and result.known and result.flagged


def test_virustotal_404_is_unknown_not_error(monkeypatch: pytest.MonkeyPatch):
    _mock_get(monkeypatch, 404, {})
    result = reputation.virustotal_lookup("a" * 64, "key")
    assert result is not None and not result.known and not result.flagged


def test_virustotal_needs_a_key():
    assert reputation.virustotal_lookup("a" * 64, "") is None


def test_safebrowsing_returns_threat(monkeypatch: pytest.MonkeyPatch):
    def fake_post(url, **kwargs):
        return httpx.Response(
            200,
            json={"matches": [{"threatType": "MALWARE"}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("app.core.reputation.httpx.post", fake_post)
    assert reputation.safebrowsing_check("http://evil.test/x", "key") == "MALWARE"


def test_safebrowsing_clean_is_none(monkeypatch: pytest.MonkeyPatch):
    def fake_post(url, **kwargs):
        return httpx.Response(200, json={}, request=httpx.Request("POST", url))

    monkeypatch.setattr("app.core.reputation.httpx.post", fake_post)
    assert reputation.safebrowsing_check("http://ok.test/x", "key") is None
    assert reputation.safebrowsing_check("http://ok.test/x", "") is None  # no key -> skip


# ------------------------------------------------- advisory security report


def test_report_is_ok_for_a_plain_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.core.security.virusscan.find_scanner", lambda pref="auto": None)
    target = tmp_path / "doc.pdf"
    target.write_bytes(b"%PDF-1.4 ...")
    report = security.check_file(target, url="https://example.com/doc.pdf")
    assert report.level is Risk.OK
    assert report.https is True
    assert set(report.checksums) == set(verify.ALGORITHMS)


def test_executable_over_http_raises_caution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.core.security.virusscan.find_scanner", lambda pref="auto": None)
    target = tmp_path / "setup.exe"
    target.write_bytes(b"MZ...")
    report = security.check_file(target, url="http://downloads.test/setup.exe")
    assert report.level is Risk.CAUTION  # executable + plain HTTP, but not blocked
    assert report.executable is True
    assert report.https is False
    assert any("executable" in f.lower() for f in report.findings)


def test_local_scan_detection_is_a_warning_not_a_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from app.core.virusscan import ScanResult

    monkeypatch.setattr(
        "app.core.security.virusscan.find_scanner", lambda pref="auto": ("ClamAV", ["x"])
    )
    monkeypatch.setattr(
        "app.core.security.virusscan.scan",
        lambda p, pref="auto": ScanResult(clean=False, scanner="ClamAV", detail="Eicar-Test"),
    )
    target = tmp_path / "sample.bin"
    target.write_bytes(b"x")
    report = security.check_file(target, url="https://example.com/sample.bin")
    assert report.level is Risk.WARNING
    assert report.scan_clean is False
    assert target.exists()  # never deleted - advisory only


def test_virustotal_flag_raises_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.core.reputation import VirusTotalResult

    monkeypatch.setattr("app.core.security.virusscan.find_scanner", lambda pref="auto": None)
    monkeypatch.setattr(
        "app.core.security.reputation.virustotal_lookup",
        lambda *a, **k: VirusTotalResult(malicious=5, suspicious=0, total=70, known=True),
    )
    target = tmp_path / "f.zip"
    target.write_bytes(b"PK")
    report = security.check_file(target, url="https://example.com/f.zip", virustotal_key="key")
    assert report.level is Risk.WARNING
    assert any("VirusTotal" in f for f in report.findings)


def test_check_url_advisory():
    insecure = security.check_url("http://x.test/f", enforce_https=True)
    assert insecure.insecure_http and insecure.has_warning
    secure = security.check_url("https://x.test/f", enforce_https=True)
    assert not secure.has_warning


# ------------------------------------------------------------- settings


def test_security_settings_roundtrip(db):
    from app.core.settings import Settings

    settings = Settings(db)
    assert settings.scan_downloads is False
    assert settings.enforce_https is False
    assert settings.virustotal_key == ""
    settings.scan_downloads = True
    settings.enforce_https = True
    settings.virustotal_key = "vt-key"
    settings.safebrowsing_key = "sb-key"
    fresh = Settings(db)
    assert fresh.scan_downloads and fresh.enforce_https
    assert fresh.virustotal_key == "vt-key" and fresh.safebrowsing_key == "sb-key"
