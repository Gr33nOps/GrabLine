from __future__ import annotations

from pathlib import Path

import pytest

from app.core.paths import default_download_dir
from app.core.settings import Settings
from app.db.database import Database


def test_defaults(db: Database, monkeypatch: pytest.MonkeyPatch):
    # No browser installed -> the session-browser default falls back to chrome;
    # when one is present it's detected instead (see test_session_browser_*).
    monkeypatch.setattr("app.core.browser_setup.detect_cookie_browser", lambda: None)
    settings = Settings(db)
    assert settings.download_dir == default_download_dir()
    assert settings.categories_enabled is True
    assert settings.clipboard_watcher is False  # opt-in: no offer on every copy
    assert settings.use_browser_session is False
    assert settings.session_browser == "chrome"
    assert settings.max_concurrent == 3
    assert settings.connections == 8
    assert settings.fair_speed is True
    assert settings.ffmpeg_path is None


def test_session_browser_defaults_to_detected(db: Database, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.core.browser_setup.detect_cookie_browser", lambda: "firefox")
    assert Settings(db).session_browser == "firefox"  # never explicitly set


def test_session_browser_honours_explicit_choice(db: Database, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.core.browser_setup.detect_cookie_browser", lambda: "firefox")
    settings = Settings(db)
    settings.session_browser = "edge"  # user overrides detection
    assert settings.session_browser == "edge"


def test_roundtrip(db: Database, tmp_path: Path):
    settings = Settings(db)
    settings.download_dir = tmp_path / "dl"
    settings.categories_enabled = False
    settings.clipboard_watcher = False
    settings.session_browser = "firefox"
    settings.max_concurrent = 5
    settings.connections = 4
    settings.ffmpeg_path = "/opt/ffmpeg"

    fresh = Settings(db)  # separate instance, same storage
    assert fresh.download_dir == tmp_path / "dl"
    assert fresh.categories_enabled is False
    assert fresh.clipboard_watcher is False
    assert fresh.use_browser_session is False  # deprecated: always off now
    assert fresh.session_browser == "firefox"
    assert fresh.max_concurrent == 5
    assert fresh.connections == 4
    assert fresh.ffmpeg_path == "/opt/ffmpeg"


def test_values_are_clamped(db: Database):
    settings = Settings(db)
    settings.max_concurrent = 99
    settings.connections = 0
    assert settings.max_concurrent == 10
    assert settings.connections == 1


def test_invalid_browser_rejected(db: Database):
    settings = Settings(db)
    with pytest.raises(ValueError):
        settings.session_browser = "netscape"


def test_corrupt_int_falls_back(db: Database):
    db.set_setting("max_concurrent", "banana")
    assert Settings(db).max_concurrent == 3


def test_schedule_retry_theme_defaults(db: Database):
    settings = Settings(db)
    assert settings.speed_schedule_enabled is False
    assert settings.speed_full_from == "00:00"
    assert settings.speed_full_to == "07:00"
    assert settings.auto_retry is True
    assert settings.auto_retry_max == 5
    assert settings.theme == "system"
    assert settings.fair_speed is True


def test_schedule_retry_theme_roundtrip(db: Database):
    settings = Settings(db)
    settings.speed_schedule_enabled = True
    settings.speed_full_from = "23:30"
    settings.speed_full_to = "06:15"
    settings.auto_retry = False
    settings.auto_retry_max = 250  # clamped to 99 (0 means retry forever)
    settings.theme = "dark"
    settings.fair_speed = False

    fresh = Settings(db)
    assert fresh.speed_schedule_enabled is True
    assert fresh.speed_full_from == "23:30"
    assert fresh.speed_full_to == "06:15"
    assert fresh.auto_retry is False
    assert fresh.auto_retry_max == 99
    assert fresh.theme == "dark"
    assert fresh.fair_speed is False


def test_bad_time_and_theme_fall_back(db: Database):
    db.set_setting("speed_full_from", "9999")
    db.set_setting("theme", "hologram")
    settings = Settings(db)
    assert settings.speed_full_from == "00:00"
    assert settings.theme == "system"
    with pytest.raises(ValueError):
        settings.theme = "hologram"


def test_archive_passwords_roundtrip_dedup_and_bad_json(db: Database):
    settings = Settings(db)
    assert settings.archive_passwords == ()
    assert settings.scan_before_extract is False

    settings.archive_passwords = ["hunter2", "  spaced  ", "", "hunter2"]
    settings.scan_before_extract = True

    fresh = Settings(db)
    assert fresh.archive_passwords == ("hunter2", "spaced")  # trimmed, deduped
    assert fresh.scan_before_extract is True

    db.set_setting("archive_passwords", "{not json")
    assert Settings(db).archive_passwords == ()


def test_favorite_folders_and_rename_rules_roundtrip(db: Database):
    settings = Settings(db)
    assert settings.favorite_folders == ()
    assert settings.rename_rules == ()

    settings.favorite_folders = ["/data/movies", "  ", "/data/movies", "/isos"]
    settings.rename_rules = [("[AD] ", ""), ("", "ignored"), ("old", "new")]

    fresh = Settings(db)
    assert fresh.favorite_folders == ("/data/movies", "/isos")
    assert fresh.rename_rules == (("[AD] ", ""), ("old", "new"))

    db.set_setting("rename_rules", "not json")
    assert Settings(db).rename_rules == ()


def test_reset_restores_defaults_but_keeps_machine_state(db: Database, tmp_path: Path):
    settings = Settings(db)
    settings.max_concurrent = 9
    settings.theme = "dark"
    settings.close_to_tray = False
    settings.download_dir = str(tmp_path / "elsewhere")
    settings.virustotal_key = "secret"
    # Not preferences: these record what already happened to this machine.
    settings.setup_seen = True
    settings.host_registered = True

    settings.reset()

    fresh = Settings(db)
    assert fresh.max_concurrent == 3
    assert fresh.theme == "system"
    assert fresh.close_to_tray is True
    assert fresh.download_dir == default_download_dir()
    assert fresh.virustotal_key == ""
    assert fresh.setup_seen is True  # the wizard does not come back
    assert fresh.host_registered is True  # nor does silent re-pairing


def test_reset_leaves_downloads_and_stats_alone(db: Database, tmp_path: Path):
    settings = Settings(db)
    settings.max_concurrent = 7
    job = db.create_job("https://example.com/a.bin", str(tmp_path), "a.bin")
    db.record_download("Programs", "example.com", 1024)

    settings.reset()

    assert Settings(db).max_concurrent == 3
    assert db.get_job(job.id) is not None
    assert db.stats_rows()


def test_export_redacts_proxy_credentials(db: Database):
    """A settings export must not carry the proxy password or the API keys
    (CWE-312 / CWE-522)."""
    from app.core.settings import sanitized_export

    settings = Settings(db)
    settings.proxy = "socks5://secretuser:secretpass@proxy.example:1080"
    settings.virustotal_key = "VT-KEY"
    settings.safebrowsing_key = "SB-KEY"

    payload = sanitized_export(db.all_settings())
    blob = str(payload)
    assert "secretpass" not in blob and "secretuser" not in blob
    assert payload["proxy"] == "socks5://proxy.example:1080"  # host kept, creds gone
    assert "virustotal_key" not in payload and "safebrowsing_key" not in payload
