"""Runtime effects beyond field persistence, with explicit OS-service boundaries."""

from __future__ import annotations

import ast
import logging
import os
from pathlib import Path
from unittest.mock import Mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"
from common import ROOT, case, record
from PySide6.QtCore import QTime
from PySide6.QtWidgets import QSystemTrayIcon

from app.core.manager import DownloadManager
from app.core.settings import Settings
from app.db.database import Database
from app.engines.smart import QualityOption
from app.engines.torrent import TorrentSession
from app.tests.qt_app import ensure_app
from app.ui import theme, threads
from app.ui.main_window import MainWindow

app = ensure_app()
root = ROOT / "settings-effects"
root.mkdir(exist_ok=True)
os.environ["LOCALAPPDATA"] = str(root / "profile")
db = Database(root / "settings.db")
s = Settings(db)
s.download_dir = root
s.check_updates = False
s.auto_start_downloads = False
manager = DownloadManager(db, settings=s, max_concurrent=0)
w = MainWindow(manager, s)
w.resize(1600, 900)
w.show()

try:
    with case(
        "SETTING-EFFECT-LIVE-APPEARANCE-TIMERS", "real Qt main window + settings callback"
    ) as e:
        for density, height in (("compact", 26), ("comfortable", 34)):
            s.ui_density = density
            w._on_settings_applied()
            assert w.table.verticalHeader().defaultSectionSize() == height
        for index, name in enumerate(("size", "progress", "speed", "eta", "status")):
            s.hidden_columns = [name]
            w._on_settings_applied()
            app.processEvents()
            assert w.table.isColumnHidden(index + 2), name
        s.hidden_columns = []
        for mode in ("light", "dark", "system"):
            s.theme = mode
            w._on_settings_applied()
            assert theme.current() is not None
        s.rss_interval_minutes = 7
        s.torrent_watch_interval_seconds = 17
        s.dashboard_refresh_ms = 1700
        w._on_settings_applied()
        assert w._rss_timer.interval() == 420_000
        assert w._watch_timer.interval() == 17_000
        assert w._dashboard_view._timer.interval() == 1700
        e.update(row_heights=[26, 34], hidden_columns=5, updated_timers=3)
    with case("SETTING-EFFECT-RATE-AND-CONNECTIONS", "real manager live settings"):
        s.connections = 11
        s.speed_schedule_enabled = False
        s.auto_throttle = False
        s.speed_limit_kbps = 123
        manager.reload_settings()
        assert manager.connections == 11
        assert manager._effective_global_rate() == 123 * 1024
        s.speed_limit_kbps = 0
        manager.reload_settings()
        assert manager._effective_global_rate() == 0
    with case("SETTING-EFFECT-SMART-OPTIONS", "real job creation and persisted engine options"):
        s.video_hq_first = True
        s.audio_bitrate = "192"
        cookies = root / "cookies.txt"
        cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        s.cookies_file = str(cookies)
        option = QualityOption("best", "video", "bestvideo+bestaudio/best", None)
        job = manager.add_smart_entry("https://example.invalid/media", "settings media", option)
        assert job.options["hq_first"] is True
        assert job.options["audio_bitrate"] == "192"
        assert job.options["cookie_file"] == str(cookies)
    with case("SETTING-EFFECT-DEFAULT-TAGS-QUEUE-RENAME", "real job creation"):
        queue = db.create_queue("settings QA")
        s.default_queue_id = queue.id
        s.default_tags = "qa, alternate"
        s.rename_rules = [("before", "after")]
        job = manager.add_url("https://example.invalid/before.bin")
        assert job.filename == "after.bin" and job.queue_id == queue.id
        assert "qa" in job.options["tags"] and "alternate" in job.options["tags"]
    with case("SETTING-EFFECT-STATISTICS-ENABLE-DISABLE", "real database statistics"):
        db.update_job_total(job.id, 1234)
        db.clear_stats()
        s.stats_enabled = False
        manager._record_completion(job.id)
        assert not db.stats_rows()
        s.stats_enabled = True
        manager._record_completion(job.id)
        assert db.lifetime_bytes() == (1234, 1)
    with case(
        "SETTING-EFFECT-TORRENT-SESSION-OPTIONS",
        "real libtorrent configuration; no peer/NAT certification",
    ):
        session = TorrentSession()
        try:
            for enc, expected in (("prefer", 1), ("require", 0), ("off", 2)):
                s.torrent_port = 0
                s.torrent_dht = s.torrent_upnp = s.torrent_natpmp = False
                s.torrent_encryption = enc
                s.torrent_upload_kbps = 77
                session.configure(s)
                actual = session.session().get_settings()
                assert not actual["enable_dht"]
                assert not actual["enable_upnp"] and not actual["enable_natpmp"]
                assert actual["upload_rate_limit"] == 77 * 1024
                assert actual["out_enc_policy"] == expected
        finally:
            session.detach()

    # Compile the actual entry-point callback definitions. Only OS dispatch is
    # captured: this is unit coverage, never a claim that Windows showed a toast.
    source = Path(__file__).resolve().parents[2] / "app/__main__.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names = {"toast", "on_job_completed", "on_job_failed", "on_queue_drained"}
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(functions) == 4
    tray = Mock()
    alerts = Mock()
    scripts = Mock()
    power = Mock()
    application = Mock()
    env = {
        "settings": s,
        "tray": tray,
        "QSystemTrayIcon": QSystemTrayIcon,
        "t": lambda text, **kwargs: text.format(**kwargs),
        "alerts": alerts,
        "scripts": scripts,
        "power": power,
        "app": application,
        "log": logging.getLogger(__name__),
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), env)
    s.quiet_enabled = False
    s.sound_on_complete = False
    s.script_on_complete = ""
    for setting, callback, args in (
        ("notify_on_complete", "on_job_completed", ("qa", "qa.bin")),
        ("notify_on_failed", "on_job_failed", ("qa", "synthetic failure")),
        ("notify_queue_done", "on_queue_drained", ()),
    ):
        with case(
            "SETTING-EFFECT-" + setting.upper(),
            "unit: actual callback + captured OS toast dispatch",
        ):
            s.after_queue_action = "nothing"
            s.toast_seconds = 7
            for enabled in (False, True):
                setattr(s, setting, enabled)
                tray.reset_mock()
                env[callback](*args)
                assert tray.showMessage.call_count == int(enabled)
                if enabled:
                    assert tray.showMessage.call_args.args[-1] == 7000
    with case(
        "SETTING-EFFECT-SOUND-SCRIPT-QUIET",
        "unit: actual callbacks + captured sound/script boundaries",
    ):
        s.notify_on_complete = True
        s.sound_on_complete = True
        s.sound_file = str(root / "synthetic.wav")
        s.script_on_complete = str(root / "synthetic.py")
        s.quiet_enabled = False
        env["on_job_completed"]("qa", "qa.bin")
        alerts.play_complete_sound.assert_called_once_with(str(root / "synthetic.wav"))
        scripts.run_script.assert_called_once_with(str(root / "synthetic.py"), "qa.bin")
        alerts.reset_mock()
        tray.reset_mock()
        s.quiet_enabled = True
        s.quiet_from = QTime.currentTime().addSecs(-60).toString("HH:mm")
        s.quiet_to = QTime.currentTime().addSecs(60).toString("HH:mm")
        env["on_job_completed"]("qa", "qa.bin")
        alerts.play_complete_sound.assert_not_called()
        tray.showMessage.assert_not_called()
        assert scripts.run_script.call_count == 2
    with case(
        "SETTING-EFFECT-AFTER-QUEUE-DISPATCH",
        "unit: captured power-action dispatch; real actions require VM",
    ):
        for action in ("nothing", "quit", "sleep", "shutdown", "hibernate", "lock"):
            power.reset_mock()
            application.reset_mock()
            s.after_queue_action = action
            env["on_queue_drained"]()
            assert application.quit.call_count == int(action == "quit")
            assert len(power.mock_calls) == int(action not in ("nothing", "quit"))
            if action not in ("nothing", "quit"):
                getattr(power, action).assert_called_once_with()
    for name, detail in {
        "POWER": "Actual sleep/shutdown/hibernate/lock reserved for disposable VM.",
        "BATTERY": "Physical battery state and threshold require battery-equipped hardware.",
        "NAT": "Real router UPnP/NAT-PMP/DHT traversal requires dedicated network infrastructure.",
        "APIS": "Real VirusTotal/Safe Browsing calls require dedicated test keys.",
        "AUDIBILITY": "Captured/generated sound does not establish human audibility.",
    }.items():
        record("SETTING-PHYSICAL-" + name, "BLOCKED", "physical/external prerequisite", detail)
finally:
    w.shutdown()
    manager.shutdown()
    threads.shutdown()
    db.close()
