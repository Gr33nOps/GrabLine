"""Real Qt widgets and isolated SQLite; separate from native-desktop coverage."""

from __future__ import annotations

import json
import os
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from common import ROOT, case, save
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QCheckBox, QDialogButtonBox, QPushButton, QSpinBox

from app.core import launcher
from app.core.settings import Settings
from app.db.database import Database
from app.engines.smart import MediaInfo, PlaylistEntry, PlaylistInfo, QualityOption
from app.tests.qt_app import ensure_app
from app.ui import threads
from app.ui.gallery_panel import GalleryPanel
from app.ui.link_panel import LinkPanel
from app.ui.playlist_panel import PlaylistPanel
from app.ui.quality_panel import QualityPanel
from app.ui.settings_dialog import SettingsDialog

app = ensure_app()
manifest = json.loads((ROOT / "browser-manifest.json").read_text())
base = manifest["base"]
profile = ROOT / "qt-profile"
profile.mkdir(exist_ok=True)
os.environ["LOCALAPPDATA"] = str(profile)


def button(widget, text):
    values = [b for b in widget.findChildren(QPushButton) if b.text() == text]
    assert len(values) == 1, (text, [b.text() for b in widget.findChildren(QPushButton)])
    QTest.mouseClick(values[0], Qt.MouseButton.LeftButton)
    app.processEvents()


with case("QT-GALLERY-SELECTION", "Qt widget integration") as evidence:
    urls = [base + "/blue.png", base + "/green.png"]
    panel = GalleryPanel(urls)
    panel.show()
    app.processEvents()
    assert panel.selected_urls() == urls
    button(panel, "Select none")
    assert panel.selected_urls() == []
    button(panel, "Select all")
    assert panel.selected_urls() == urls
    panel.grid.item(1).setCheckState(Qt.CheckState.Unchecked)
    assert panel.selected_urls() == [urls[0]]
    ok = panel.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok)
    QTest.mouseClick(ok, Qt.MouseButton.LeftButton)
    assert panel.result() == panel.DialogCode.Accepted
    panel._thumbs.wait(5000)
    evidence["selected_urls"] = panel.selected_urls()

with case("QT-LINK-FILTER-SELECTION", "Qt widget integration"):
    urls = [base + "/blue.png", base + "/audio.mp3", base + "/video.mp4", base + "/report.pdf"]
    panel = LinkPanel(urls)
    panel.show()
    app.processEvents()
    button(panel, "None")
    assert panel.selected_urls() == []
    button(panel, "Images")
    assert panel.selected_urls() == [urls[0]]
    button(panel, "None")
    panel.filter_box.setText("report")
    button(panel, "All visible")
    assert panel.selected_urls() == [urls[3]]
    panel.reject()
    assert panel.result() == panel.DialogCode.Rejected

with case("QT-PLAYLIST-CAP-SELECTION", "Qt widget integration"):
    info = PlaylistInfo(
        base,
        "Fixture playlist",
        None,
        tuple(PlaylistEntry(base + f"/video.mp4?item={i}", f"Clip {i}", 4, i) for i in range(1, 6)),
    )
    panel = PlaylistPanel(info, preselect_cap=2)
    panel.show()
    app.processEvents()
    assert [entry.index for entry in panel.selected_entries()] == [1, 2]
    button(panel, "Select none")
    assert panel.selected_entries() == []
    button(panel, "Select all")
    assert len(panel.selected_entries()) == 5
    panel.reject()

with case("QT-QUALITY-SUBTITLES-TRIM-EXTRAS", "Qt widget integration") as evidence:
    options = (
        QualityOption("Best", "video", "b", height=1080),
        QualityOption("MP3", "audio", "b", audio_format="mp3"),
    )
    info = MediaInfo(
        base + "/video.mp4", "fixture", "QA clip", None, 4, None, options, ("en",), ("fr",)
    )
    panel = QualityPanel(info)
    panel.show()
    app.processEvents()
    panel.options_list.setCurrentRow(1)
    assert panel.selected_option().audio_format == "mp3"
    panel.subtitle_combo.setCurrentIndex(1)
    panel.embed_subtitles.setChecked(True)
    assert panel.subtitles_config()["embed"] is True
    panel.save_thumbnail.setChecked(True)
    panel.save_metadata.setChecked(True)
    panel.sponsorblock.setCurrentIndex(2)
    extras = panel.extras_config()
    assert extras == {
        "save_thumbnail": True,
        "save_metadata": True,
        "chapters": True,
        "sponsorblock": "remove",
    }
    panel.trim_start.setText("0:03")
    panel.trim_end.setText("0:02")
    button(panel, "Download")
    assert panel.result() != panel.DialogCode.Accepted
    panel.trim_start.setText("0:01")
    panel.trim_end.setText("0:03")
    assert panel.trim_range() == (1, 3)
    button(panel, "Download")
    assert panel.result() == panel.DialogCode.Accepted
    evidence["extras"] = extras

# OS autostart is a VM case. Only that OS boundary is replaced here; widget
# signals, validation, apply(), Settings and database persistence remain real.
db = Database(profile / "settings.db")
settings = Settings(db)
with patch.object(launcher, "set_autostart", lambda _enabled: None):
    dialog = SettingsDialog(settings)
    dialog.show()
    app.processEvents()
    dialog.watch_dir_edit.setText(str(profile / "watch"))
    mapping = {
        "categories_check": "categories_enabled",
        "clipboard_check": "clipboard_watcher",
        "fair_speed_check": "fair_speed",
        "schedule_check": "speed_schedule_enabled",
        "download_schedule_check": "download_schedule_enabled",
        "updates_check": "check_updates",
        "retry_check": "auto_retry",
        "battery_check": "pause_on_battery",
        "network_check": "wait_for_network",
        "sound_check": "sound_on_complete",
        "auto_throttle_check": "auto_throttle",
        "notify_check": "notify_on_complete",
        "open_folder_check": "auto_open_folder",
        "extract_check": "auto_extract",
        "scan_check": "scan_before_extract",
        "scan_downloads_check": "scan_downloads",
        "enforce_https_check": "enforce_https",
        "insecure_ssl_check": "insecure_ssl",
        "hq_first_check": "video_hq_first",
        "confirm_video_quality_check": "confirm_video_quality",
        "start_min_check": "start_minimized",
        "tray_min_check": "minimize_to_tray",
        "tray_close_check": "close_to_tray",
        "confirm_exit_check": "confirm_exit_active",
        "confirm_downloads_check": "confirm_downloads",
        "ask_save_check": "ask_save_dir",
        "watch_check": "torrent_watch_enabled",
        "watch_subfolder_check": "torrent_watch_subfolders",
        "extract_subfolder_check": "extract_to_subfolder",
        "delete_archive_check": "delete_archive_after_extract",
        "notify_failed_check": "notify_on_failed",
        "notify_queue_check": "notify_queue_done",
        "stats_enabled_check": "stats_enabled",
        "logfile_check": "log_to_file",
        "dht_check": "torrent_dht",
        "upnp_check": "torrent_upnp",
        "natpmp_check": "torrent_natpmp",
        "seed_check": "torrent_seed",
        "sequential_check": "torrent_sequential",
    }
    for field, prop in mapping.items():
        with case("QT-SETTING-" + prop.upper(), "Qt widget integration"):
            control = getattr(dialog, field)
            for i in range(dialog.tabs.count()):
                if dialog.tabs.widget(i).isAncestorOf(control):
                    dialog.tabs.setCurrentIndex(i)
                    break
            for value in (False, True):
                control.setChecked(value)
                assert dialog.apply()
                assert getattr(Settings(db), prop) is value, (field, prop, value)
    for field, prop, value in [
        ("concurrent_spin", "max_concurrent", 4),
        ("connections_spin", "connections", 3),
        ("speed_spin", "speed_limit_kbps", 200),
        ("free_mb_spin", "min_free_mb", 20),
        ("playlist_cap_spin", "playlist_batch_cap", 3),
        ("seed_minutes_spin", "torrent_seed_minutes", 5),
        ("battery_pct_spin", "battery_min_percent", 15),
        ("retention_spin", "stats_retention_days", 7),
        ("refresh_spin", "dashboard_refresh_ms", 1000),
    ]:
        with case("QT-SETTING-" + prop.upper(), "Qt widget integration"):
            getattr(dialog, field).setValue(value)
            assert dialog.apply()
            assert getattr(Settings(db), prop) == value
    inventory = []
    for i in range(dialog.tabs.count()):
        page = dialog.tabs.widget(i)
        inventory.append(
            {
                "section": dialog.tabs.tabText(i),
                "checkboxes": [w.text() for w in page.findChildren(QCheckBox)],
                "spins": [
                    {"minimum": w.minimum(), "maximum": w.maximum(), "value": w.value()}
                    for w in page.findChildren(QSpinBox)
                ],
            }
        )
    save("settings-control-inventory.json", inventory)
    assert len(inventory) == 19
    dialog.reject()
db.close()
threads.shutdown()
