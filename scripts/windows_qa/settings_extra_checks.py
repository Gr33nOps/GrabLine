"""Language/quiet effects and every editable shortcut's live binding/reset."""

from __future__ import annotations

import os
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
from common import ROOT, case
from PySide6.QtCore import Qt, QTime, QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from app.core import i18n, launcher
from app.core.manager import DownloadManager
from app.core.settings import Settings
from app.db.database import Database
from app.tests.qt_app import ensure_app
from app.ui.main_window import MainWindow
from app.ui.settings_dialog import SettingsDialog
from app.ui.shortcuts import normalize

app = ensure_app()
root = ROOT / "settings-extras"
root.mkdir(exist_ok=True)
os.environ["LOCALAPPDATA"] = str(root / "profile")
db = Database(root / "settings.db")
s = Settings(db)
s.download_dir = root
s.check_updates = False
manager = DownloadManager(db, settings=s, max_concurrent=0)
window = MainWindow(manager, s)
window.show()
timer = QTimer()


def dismiss_restart():
    widget = QApplication.activeModalWidget()
    if isinstance(widget, QMessageBox):
        widget.reject()  # The real "Later" outcome; no restart.


timer.timeout.connect(dismiss_restart)
timer.start(30)
try:
    with patch.object(launcher, "set_autostart"):
        d = SettingsDialog(s)
        d.show()
        app.processEvents()
        with case(
            "QT-SETTING-LANGUAGE",
            "real Qt combo/apply + preference persistence; rendering separately verified",
        ) as e:
            options = []
            for index in range(d.language_combo.count()):
                d.language_combo.setCurrentIndex(index)
                assert d.apply()
                language = d.language_combo.currentData()
                assert Settings(db).language == language
                options.append(language)
            assert len(options) == len(i18n.available_languages())
            e["options"] = options
            d.language_combo.setCurrentIndex(d.language_combo.findData("en"))
            assert d.apply()
        with case("QT-SETTING-QUIET_ENABLED", "real Qt checkbox + wall-clock quiet-hours effect"):
            d.quiet_from_edit.setTime(QTime.currentTime().addSecs(-60))
            d.quiet_to_edit.setTime(QTime.currentTime().addSecs(60))
            for enabled in (True, False):
                d.quiet_check.setChecked(enabled)
                assert d.apply()
                assert Settings(db).quiet_enabled is enabled
                assert s.in_quiet_hours() is enabled
        page = d._shortcuts_page
        resets = [b for b in page.findChildren(QPushButton) if b.text() == "Reset"]
        assert len(resets) == len(page._edits)
        for index, ((identifier, edit), reset) in enumerate(
            zip(page._edits.items(), resets, strict=True)
        ):
            with case(
                "QT-SHORTCUT-" + identifier.upper().replace(".", "-"),
                "real Qt shortcut editor + persisted and live binding + reset",
            ) as e:
                default = normalize(
                    edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
                )
                alternate = "Ctrl+Alt+" + ("Shift+" if index >= 26 else "") + chr(65 + index % 26)
                edit.setKeySequence(QKeySequence(alternate))
                assert d.apply()
                assert Settings(db).shortcuts[identifier] == normalize(alternate)
                window._shortcuts.reload()
                assert window._shortcuts.effective()[identifier] == normalize(alternate)
                QTest.mouseClick(reset, Qt.MouseButton.LeftButton)
                assert d.apply()
                assert identifier not in Settings(db).shortcuts
                window._shortcuts.reload()
                assert window._shortcuts.effective()[identifier] == default
                e.update(default=default, alternate=normalize(alternate))
        with case("QT-SHORTCUT-CONFLICT-UNBIND", "real Qt live conflict warning and unbinding"):
            ids = list(page._edits)
            page._edits[ids[0]].setKeySequence(QKeySequence("Ctrl+Alt+Z"))
            page._edits[ids[1]].setKeySequence(QKeySequence("Ctrl+Alt+Z"))
            assert page._warning.text()
            page._reset_all()
            page._edits[ids[0]].clear()
            assert d.apply()
            window._shortcuts.reload()
            assert window._shortcuts.effective()[ids[0]] == ""
            page._reset_all()
            assert d.apply()
        d.reject()
finally:
    timer.stop()
    window.shutdown()
    manager.shutdown()
    db.close()
