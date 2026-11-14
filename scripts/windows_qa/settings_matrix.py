"""Additional settings persistence and render coverage in an offscreen Qt app."""

from __future__ import annotations

import os
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from common import ROOT, case
from PySide6.QtCore import Qt, QTime, QTimer
from PySide6.QtWidgets import QMessageBox

from app.core import i18n, launcher
from app.core.settings import Settings
from app.db.database import Database
from app.tests.qt_app import ensure_app
from app.ui import theme, threads
from app.ui.settings_dialog import SettingsDialog

app = ensure_app()
profile = ROOT / "settings-matrix"
profile.mkdir(exist_ok=True)
db = Database(profile / "settings.db")
settings = Settings(db)


def dismiss_messages():
    for widget in app.topLevelWidgets():
        if isinstance(widget, QMessageBox) and widget.isVisible():
            widget.reject()


timer = QTimer()
timer.timeout.connect(dismiss_messages)
timer.start(30)
with patch.object(launcher, "set_autostart", lambda _enabled: None):
    dialog = SettingsDialog(settings)
    dialog.show()
    app.processEvents()
    texts = [
        ("folder_edit", "download_dir", str(profile / "downloads")),
        ("sound_file_edit", "sound_file", str(profile / "sound.wav")),
        ("script_edit", "script_on_complete", ""),
        ("proxy_edit", "proxy", "http://127.0.0.1:3128"),
        ("cookies_edit", "cookies_file", str(profile / "cookies.txt")),
        ("torrent_dir_edit", "torrent_dir", str(profile / "torrents")),
        ("search_url_edit", "torrent_search_url", "https://example.test/?q={query}"),
        ("watch_dir_edit", "torrent_watch_dir", str(profile / "watch")),
        ("watch_dest_edit", "torrent_watch_dest", str(profile / "destination")),
        ("default_tags_edit", "default_tags", "qa, disposable"),
        ("ua_edit", "user_agent", "GrabLine-Windows-QA/1"),
        ("virustotal_edit", "virustotal_key", "qa-not-a-real-key"),
        ("safebrowsing_edit", "safebrowsing_key", "qa-not-a-real-key"),
        ("scan_ext_edit", "scan_extensions", ".exe,.zip"),
        ("ffmpeg_override_edit", "ffmpeg_path", str(profile / "ffmpeg.exe")),
    ]
    for field, prop, value in texts:
        with case("QT-SETTING-" + prop.upper(), "Qt widget integration") as evidence:
            control = getattr(dialog, field)
            before = control.text()
            for text in (before, value):
                control.setText(text)
                assert dialog.apply()
                actual = getattr(Settings(db), prop)
                assert str(actual or "") == text, (prop, actual, text)
            evidence["alternate_persisted"] = True
    dialog.proxy_edit.clear()
    plain = [
        ("passwords_edit", "archive_passwords", "qa-only\nsecond", ("qa-only", "second")),
        ("favorites_edit", "favorite_folders", str(profile), [str(profile)]),
        ("rss_edit", "rss_feeds", "http://127.0.0.1/feed | qa", ["http://127.0.0.1/feed | qa"]),
        (
            "trackers_default_edit",
            "torrent_trackers",
            "http://127.0.0.1/announce",
            ["http://127.0.0.1/announce"],
        ),
        ("rename_edit", "rename_rules", "old -> new", [("old", "new")]),
        ("host_limits_edit", "host_limits", "EXAMPLE.TEST = 64\ninvalid", {"example.test": 64}),
    ]
    for field, prop, value, expected in plain:
        with case("QT-SETTING-" + prop.upper(), "Qt widget integration"):
            getattr(dialog, field).setPlainText(value)
            assert dialog.apply()
            actual = getattr(Settings(db), prop)
            assert actual == expected or list(actual) == expected, (prop, actual)
    with case("QT-SETTING-PROXY_BYPASS", "Qt widget integration"):
        dialog.bypass_edit.setText("localhost,127.0.0.1")
        assert dialog.apply()
        assert list(Settings(db).proxy_bypass) == ["localhost", "127.0.0.1"]
    combos = {
        "browser_combo": "session_browser",
        "new_dl_combo": "auto_start_downloads",
        "default_quality_combo": "video_default_quality",
        "bitrate_combo": "audio_bitrate",
        "encryption_combo": "torrent_encryption",
        "after_combo": "after_queue_action",
        "theme_combo": "theme",
        "accent_combo": "accent_color",
        "density_combo": "ui_density",
        "scanner_combo": "scanner_pref",
        "log_combo": "log_level",
        "default_queue_combo": "default_queue_id",
    }
    for field, prop in combos.items():
        with case("QT-SETTING-" + prop.upper(), "Qt widget integration") as evidence:
            widget = getattr(dialog, field)
            options = []
            for index in range(widget.count()):
                widget.setCurrentIndex(index)
                assert dialog.apply()
                expected = (
                    widget.currentText()
                    if field == "default_quality_combo"
                    else widget.currentData()
                )
                if prop == "default_queue_id":
                    expected = int(expected or 0)
                actual = getattr(Settings(db), prop)
                assert actual == expected, (prop, actual, expected)
                options.append(expected)
            evidence["options"] = options
    numeric = {
        "retry_spin": "auto_retry_max",
        "torrent_port_spin": "torrent_port",
        "ratio_spin": "torrent_ratio_limit",
        "upload_spin": "torrent_upload_kbps",
        "rss_interval_spin": "rss_interval_minutes",
        "watch_interval_spin": "torrent_watch_interval_seconds",
        "throttle_limit_spin": "auto_throttle_kbps",
        "throttle_threshold_spin": "auto_throttle_threshold_kbps",
        "toast_spin": "toast_seconds",
    }
    for field, prop in numeric.items():
        with case("QT-SETTING-" + prop.upper(), "Qt widget integration") as evidence:
            widget = getattr(dialog, field)
            for value in (
                widget.minimum(),
                widget.maximum(),
                widget.minimum() - 1,
                widget.maximum() + 1,
            ):
                widget.setValue(value)
                assert dialog.apply()
                assert getattr(Settings(db), prop) == widget.value()
            evidence.update(minimum=widget.minimum(), maximum=widget.maximum(), clamps_invalid=True)
    for field, prop in {
        "full_from": "speed_full_from",
        "full_to": "speed_full_to",
        "download_start": "download_start",
        "download_stop": "download_stop",
        "quiet_from_edit": "quiet_from",
        "quiet_to_edit": "quiet_to",
    }.items():
        with case("QT-SETTING-" + prop.upper(), "Qt widget integration"):
            for hour, minute in ((0, 0), (23, 59), (12, 34)):
                getattr(dialog, field).setTime(QTime(hour, minute))
                assert dialog.apply()
                assert getattr(Settings(db), prop) == f"{hour:02}:{minute:02}"
    with case("QT-SETTING-DAYS-COLUMNS-QUIET", "Qt widget integration"):
        for index, check in enumerate(dialog.day_checks):
            check.setChecked(index % 2 == 0)
        for check in dialog.column_checks.values():
            check.setChecked(False)
        dialog.quiet_check.setChecked(True)
        assert dialog.apply()
        assert list(Settings(db).download_days) == [0, 2, 4, 6]
        assert set(Settings(db).hidden_columns) == set(dialog.column_checks)
        assert Settings(db).quiet_enabled
    with case("QT-SETTING-PROXY-INVALID", "Qt widget integration"):
        previous = settings.proxy
        dialog.proxy_edit.setText("not-a-proxy")
        assert dialog.apply() is False
        assert settings.proxy == previous
        dialog.proxy_edit.clear()
    with case("QT-SETTING-WATCH-FOLDER-PAIR", "Qt widget integration"):
        dialog.watch_check.setChecked(True)
        dialog.watch_dir_edit.clear()
        assert dialog.apply()
        assert not settings.torrent_watch_enabled
        dialog.watch_dir_edit.setText(str(profile / "watch"))
        assert dialog.apply() and settings.torrent_watch_enabled
    dialog.reject()
    for language, _, _ in i18n.available_languages():
        with case("QT-LANGUAGE-" + language.upper(), "Qt render integration") as evidence:
            i18n.set_language(language)
            settings.language = language
            app.setLayoutDirection(
                Qt.LayoutDirection.RightToLeft if i18n.is_rtl() else Qt.LayoutDirection.LeftToRight
            )
            widget = SettingsDialog(settings)
            widget.resize(1100, 800)
            widget.show()
            app.processEvents()
            assert widget.tabs.count() == 19
            assert widget.layoutDirection() == app.layoutDirection()
            evidence["screenshot"] = str(profile / f"language-{language}.png")
            assert widget.grab().save(evidence["screenshot"])
            widget.reject()
    i18n.set_language("en")
    app.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
    for mode in theme.THEMES:
        with case("QT-THEME-" + mode.upper(), "Qt render integration") as evidence:
            theme.apply_theme(app, mode)
            widget = SettingsDialog(settings)
            widget.show()
            app.processEvents()
            assert mode == "system" or theme.is_dark() == (mode == "dark")
            evidence["screenshot"] = str(profile / f"theme-{mode}.png")
            assert widget.grab().save(evidence["screenshot"])
            widget.reject()
timer.stop()
db.close()
threads.shutdown()
