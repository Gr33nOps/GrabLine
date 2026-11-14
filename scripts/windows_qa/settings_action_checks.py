"""Settings actions on disposable data; file dialogs and OS dispatch are boundaries."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
from common import ROOT, case, record, save
from PySide6.QtCore import QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QMessageBox, QPushButton

from app.core.manager import DownloadManager
from app.core.settings import Settings
from app.db.database import Database
from app.tests.qt_app import ensure_app
from app.ui import threads
from app.ui.main_window import MainWindow

app = ensure_app()
root = ROOT / "settings-actions"
root.mkdir(exist_ok=True)
os.environ["LOCALAPPDATA"] = str(root / "profile")
db = Database(root / "settings.db")
s = Settings(db)
s.setup_seen = s.host_registered = True
s.check_updates = False
s.download_dir = root
manager = DownloadManager(db, settings=s, max_concurrent=0)
w = MainWindow(manager, s)
w.show()
w._switch_view("settings")
v = w._pages.currentWidget()
d = v._dialog
level = "offscreen real Qt action; disposable SQLite; file chooser/message boundaries"


def click(text):
    buttons = [b for b in v.findChildren(QPushButton) if b.text() == text]
    # Materialize every section before resolving controls.
    if not buttons:
        for index in range(v._list.count()):
            v._list.setCurrentRow(index)
        buttons = [b for b in v.findChildren(QPushButton) if b.text() == text]
    assert len(buttons) == 1, (text, len(buttons))
    buttons[0].click()
    app.processEvents()


try:
    with patch.object(QMessageBox, "information"), patch.object(QMessageBox, "warning"):
        for index, title in enumerate(v._titles):
            with case("SETTING-SECTION-" + str(index + 1), level) as e:
                v._list.setCurrentRow(index)
                app.processEvents()
                assert v._title.text() == title
                assert index not in v._pending_pages
                assert v._stack.currentWidget().isVisible()
                v._stack.currentWidget().grab().save(str(root / f"section-{index + 1}.png"))
                e.update(section=title, screenshot=f"settings-actions/section-{index + 1}.png")
        with case("SETTING-SEARCH-NAVIGATION", level):
            v._search.setText("advanced")
            assert sum(not v._list.item(i).isHidden() for i in range(v._list.count())) == 1
            v._search.setText("no-such-section-qa")
            assert all(v._list.item(i).isHidden() for i in range(v._list.count()))
            v._search.clear()
            assert all(not v._list.item(i).isHidden() for i in range(v._list.count()))

        # All settings-page action buttons, including the 38 individual resets.
        inventory = []
        for index, title in enumerate(v._titles):
            scroll = v._stack.widget(index)
            for button in scroll.findChildren(QPushButton):
                inventory.append({"section": title, "text": button.text()})
        save("settings-action-inventory.json", inventory)

        for method, field, folder in [
            ("_browse_folder", "folder_edit", True),
            ("_browse_torrent_folder", "torrent_dir_edit", True),
            ("_browse_watch_folder", "watch_dir_edit", True),
            ("_browse_watch_dest", "watch_dest_edit", True),
            ("_browse_cookies", "cookies_edit", False),
            ("_browse_sound", "sound_file_edit", False),
        ]:
            with case("SETTING-ACTION-" + method.upper(), level):
                chosen = str(root / field)
                api = "getExistingDirectory" if folder else "getOpenFileName"
                with patch.object(
                    QFileDialog, api, return_value=chosen if folder else (chosen, "")
                ):
                    getattr(d, method)()
                assert getattr(d, field).text() == chosen
                with patch.object(QFileDialog, api, return_value="" if folder else ("", "")):
                    getattr(d, method)()
                assert getattr(d, field).text() == chosen

        job = db.create_job("https://example.invalid/fixture", str(root), "fixture.bin")
        db.record_download("QA", "example.invalid", 4096)
        with case("SETTING-ACTION-STATS-EXPORT", level):
            output = root / "statistics.csv"
            with patch.object(QFileDialog, "getSaveFileName", return_value=(str(output), "CSV")):
                click("Export CSV…")
            rows = list(csv.reader(output.open(encoding="utf-8", newline="")))
            assert rows[0] == ["day", "category", "host", "bytes", "files"]
            assert any(row[1:] == ["QA", "example.invalid", "4096", "1"] for row in rows[1:])
        with case("SETTING-ACTION-STATS-CLEAR-CANCEL-CONFIRM", level):
            with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
                click("Clear statistics…")
            assert db.stats_rows()
            with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
                click("Clear statistics…")
            assert not db.stats_rows() and db.get_job(job.id)

        with case("SETTING-ACTION-SETTINGS-EXPORT-IMPORT", level):
            s.proxy = "http://qa-user:qa-password@127.0.0.1:3128"
            s.virustotal_key = "synthetic-vt-secret"
            s.speed_limit_kbps = 123
            output = root / "export.json"
            with patch.object(QFileDialog, "getSaveFileName", return_value=(str(output), "JSON")):
                click("Export settings…")
            blob = output.read_text(encoding="utf-8")
            assert all(x not in blob for x in ("qa-password", "qa-user", "synthetic-vt-secret"))
            s.speed_limit_kbps = 0
            with patch.object(QFileDialog, "getOpenFileName", return_value=(str(output), "JSON")):
                click("Import settings…")
            assert s.speed_limit_kbps == 123 and d.speed_spin.value() == 123
            s.proxy = None
            d.proxy_edit.clear()
        with case("SETTING-ACTION-IMPORT-INVALID-CANCEL", level):
            output = root / "invalid.json"
            output.write_text("[]", encoding="utf-8")
            before = db.all_settings()
            for chosen in (str(output), ""):
                with patch.object(QFileDialog, "getOpenFileName", return_value=(chosen, "JSON")):
                    click("Import settings…")
                assert db.all_settings() == before
        with case("SETTING-ACTION-RESET-CANCEL-CONFIRM", level):
            with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
                click("Reset all settings…")
            assert s.speed_limit_kbps == 123
            with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
                click("Reset all settings…")
            assert s.speed_limit_kbps == 0 and d.speed_spin.value() == 0
            assert db.get_job(job.id) and s.setup_seen and s.host_registered
            s.check_updates = False

        with case("SETTING-ACTION-DATABASE-MAINTENANCE", level):
            click("Compact database")
            click("Check database")
            assert db.integrity_check() == "ok" and db.get_job(job.id)
        with case("SETTING-ACTION-FORGET-WATCHED", level):
            s.torrent_watch_seen = ["qa.torrent"]
            click("Forget imported files")
            assert not s.torrent_watch_seen
        with case("SETTING-ACTION-FORGET-CERTIFICATES", level):
            s.trusted_certificates = {"qa.invalid:443": "a" * 64}
            d._refresh_trusted_certificates()
            assert d.forget_certs_button.isEnabled()
            click("Forget trusted certificates")
            assert not s.trusted_certificates and not d.forget_certs_button.isEnabled()
        with case("SETTING-ACTION-DIAGNOSTICS", level):
            prior = app.clipboard().text()
            try:
                click("Copy diagnostics")
                diagnostic = app.clipboard().text()
                assert (
                    "GrabLine" in diagnostic
                    and "Python:" in diagnostic
                    and "PySide6:" in diagnostic
                )
                assert "qa-password" not in diagnostic
                (root / "diagnostics.txt").write_text(diagnostic, encoding="utf-8")
            finally:
                app.clipboard().setText(prior)
        with case("SETTING-ACTION-ABOUT-LINK-DISPATCH", "Qt button + URL dispatch boundary") as e:
            seen = []
            with patch.object(
                QDesktopServices, "openUrl", side_effect=lambda u: seen.append(u.toString()) or True
            ):
                for text in (
                    "Project page",
                    "Changelog && releases",
                    "Report an issue",
                    "Buy me a coffee",
                ):
                    click(text)
            assert len(seen) == 4 and all(url.startswith("https://") for url in seen)
            assert seen[1].endswith("/releases") and seen[2].endswith("/issues")
            e["urls"] = seen
        with (
            case(
                "SETTING-ACTION-UPDATE-DISPATCH",
                "Qt button to real parent; network checker boundary",
            ),
            patch.object(w, "check_for_updates") as check,
        ):
            click("Check for updates")
            check.assert_called_once_with(quiet=False)
        for text, expected in (
            ("Setup wizard…", "SetupDialog"),
            ("Manage saved logins…", "CloudAccountsDialog"),
            ("View all shortcuts…", "ShortcutsDialog"),
        ):
            with case("SETTING-ACTION-OPEN-" + expected.upper(), level):
                observed = []

                def close_child(observed=observed):
                    modal = app.activeModalWidget()
                    if modal:
                        observed.append(type(modal).__name__)
                        modal.reject()

                timer = QTimer()
                timer.timeout.connect(close_child)
                timer.start(20)
                try:
                    click(text)
                    assert expected in observed, observed
                finally:
                    timer.stop()

        with case("SETTING-ACTION-RESET-ALL-SHORTCUTS", level):
            from PySide6.QtGui import QKeySequence

            from app.ui.shortcuts import DEFAULTS, normalize

            edits = d._shortcuts_page._edits
            edits[DEFAULTS[0].id].setKeySequence(QKeySequence("Ctrl+Alt+Z"))
            click("Reset all to defaults")
            for shortcut in DEFAULTS:
                assert normalize(edits[shortcut.id].keySequence().toString()) == normalize(
                    shortcut.default
                )

        with case("SETTING-LOGGING-FRESH-PROCESS", "fresh Python process + real log file"):
            s.log_level = "debug"
            s.log_to_file = True
            code = """import logging,sys
from pathlib import Path
from app.core.settings import Settings
from app.db.database import Database
from app.__main__ import _configure_logging
from app.core import paths
db=Database(Path(sys.argv[1]));s=Settings(db);_configure_logging(s)
assert logging.getLogger().level==logging.DEBUG
logging.getLogger('qa.settings').warning('settings-restart-marker')
logging.shutdown()
log_text=(paths.data_dir()/'grabline.log').read_text(encoding='utf-8')
assert 'settings-restart-marker' in log_text
db.close()
"""
            result = subprocess.run(
                [sys.executable, "-c", code, str(root / "settings.db")],
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert result.returncode == 0, result.stderr
            s.log_level = "error"
            s.log_to_file = False
            code = code[: code.index("assert logging.getLogger().level")] + (
                "assert logging.getLogger().level==logging.ERROR\n"
                "assert not any(isinstance(h,logging.FileHandler) "
                "for h in logging.getLogger().handlers)\ndb.close()"
            )
            result = subprocess.run(
                [sys.executable, "-c", code, str(root / "settings.db")],
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert result.returncode == 0, result.stderr

        with case("SETTING-DATABASE-FRESH-PROCESS", "cross-process persisted settings digest") as e:
            expected = hashlib.sha256(
                json.dumps(db.all_settings(), sort_keys=True).encode()
            ).hexdigest()
            code = (
                "from app.db.database import Database;from pathlib import Path;"
                "import sys,json,hashlib;d=Database(Path(sys.argv[1]));"
                "print(hashlib.sha256(json.dumps(d.all_settings(),sort_keys=True)"
                ".encode()).hexdigest());d.close()"
            )
            result = subprocess.run(
                [sys.executable, "-c", code, str(root / "settings.db")],
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert result.returncode == 0 and result.stdout.strip() == expected, result.stderr
            e["settings_digest"] = expected

        for identifier, reason in {
            "LOGIN": "Actual login registration/startup needs a provisioned disposable VM.",
            "PAIR-REGISTRATION": (
                "Native-host re-registration reserved for VM; "
                "preserves existing browser registration."
            ),
            "UPDATE-INSTALL": (
                "Update handoff/install requires VM; button dispatch tested separately."
            ),
        }.items():
            record(
                "SETTING-EXTERNAL-" + identifier, "BLOCKED", "OS/VM or dependency action", reason
            )
finally:
    w.shutdown()
    manager.shutdown()
    threads.shutdown()
    db.close()
