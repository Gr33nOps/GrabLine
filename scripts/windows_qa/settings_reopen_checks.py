"""Every editable setting through embedded Save, reopened widgets and Cancel."""

from __future__ import annotations

import gc
import json
import os
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
from common import ROOT, case, save
from PySide6.QtCore import QCoreApplication, QEvent, Qt, QTime, QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTimeEdit,
)

from app.core import launcher
from app.core.manager import DownloadManager
from app.core.settings import Settings
from app.db.database import Database
from app.tests.qt_app import ensure_app
from app.ui import threads
from app.ui.main_window import MainWindow
from app.ui.settings_dialog import SettingsDialog

app = ensure_app()
root = ROOT / "reopen"
root.mkdir(parents=True, exist_ok=True)
os.environ["LOCALAPPDATA"] = str(root / "profile")
db = Database(root / "settings.db")
s = Settings(db)
s.download_dir = root
s.setup_seen = s.host_registered = True
s.check_updates = False
s.torrent_watch_dir = root / "watch"
db.create_queue("QA alternate queue")
(root / "completion.cmd").write_text("@exit /b 0\n")
manager = DownloadManager(db, settings=s, max_concurrent=0)
w = MainWindow(manager, s)
w.show()
w._switch_view("settings")
v = w._pages.currentWidget()
d = v._dialog
inventory = json.loads((ROOT / "editable-control-inventory.json").read_text())
states = {"autostart": False}
timer = QTimer()
timer.timeout.connect(
    lambda: [
        x.reject() for x in app.topLevelWidgets() if isinstance(x, QMessageBox) and x.isVisible()
    ]
)
timer.start(20)


def control(dialog, field):
    if field.startswith("shortcut."):
        return dialog._shortcuts_page._edits[field[9:]]
    if "[" in field:
        name, index = field[:-1].split("[")
        group = getattr(dialog, name)
        return list(group.values())[int(index)] if isinstance(group, dict) else group[int(index)]
    return getattr(dialog, field)


def value(c):
    if isinstance(c, QCheckBox):
        return c.isChecked()
    if isinstance(c, QComboBox):
        return c.currentIndex()
    if isinstance(c, QTimeEdit):
        return c.time().toString("HH:mm")
    if isinstance(c, QLineEdit):
        return c.text()
    if isinstance(c, QPlainTextEdit):
        return c.toPlainText()
    if hasattr(c, "keySequence"):
        return c.keySequence().toString()
    return c.value()


def set_value(c, x):
    if isinstance(c, QCheckBox):
        if c.isChecked() != x:
            c.setFocus()
            QTest.keyClick(c, Qt.Key.Key_Space)
    elif isinstance(c, QComboBox):
        c.setCurrentIndex(x)
    elif isinstance(c, QTimeEdit):
        c.setTime(QTime.fromString(x, "HH:mm"))
    elif isinstance(c, QLineEdit):
        c.setText(x)
    elif isinstance(c, QPlainTextEdit):
        c.setPlainText(x)
    elif hasattr(c, "setKeySequence"):
        c.setKeySequence(QKeySequence(x))
    else:
        c.setValue(x)


def alternate(c, field):
    if isinstance(c, QCheckBox):
        return not c.isChecked()
    if isinstance(c, QComboBox):
        return (c.currentIndex() + 1) % c.count()
    if isinstance(c, QTimeEdit):
        return "12:34" if value(c) != "12:34" else "00:00"
    if isinstance(c, QPlainTextEdit):
        return {
            "passwords_edit": "qa-only\nsecond",
            "favorites_edit": str(root),
            "rss_edit": "http://127.0.0.1/feed | fixture",
            "trackers_default_edit": "http://127.0.0.1/announce",
            "rename_edit": "old -> new",
            "host_limits_edit": "example.test = 64",
        }[field]
    if isinstance(c, QLineEdit):
        return {
            "proxy_edit": "http://127.0.0.1:3128",
            "bypass_edit": "localhost, 127.0.0.1",
            "scan_ext_edit": ".exe,.zip",
            "default_tags_edit": "qa, settings",
            "search_url_edit": "https://example.test/?q={query}",
            "ua_edit": "Settings-QA/1",
            "virustotal_edit": "synthetic-key",
            "safebrowsing_edit": "synthetic-key",
            "script_edit": str(root / "completion.cmd"),
        }.get(field, str(root / (field + ".txt")))
    if hasattr(c, "keySequence"):
        return "Ctrl+Alt+Z"
    return c.minimum() if c.value() != c.minimum() else min(c.maximum(), c.minimum() + 1)


try:
    with (
        patch.object(launcher, "autostart_enabled", lambda: states["autostart"]),
        patch.object(launcher, "set_autostart", lambda x: states.update(autostart=x)),
    ):
        for row in inventory:
            field = row["field"]
            c = control(d, field)
            original = value(c)
            alt = alternate(c, field)
            if alt == original and isinstance(c, (QLineEdit, QPlainTextEdit)):
                alt = str(alt) + "-alternate"
            with case(
                "SETTING-REOPEN-" + field,
                "Qt Save + fresh SQLite/dialog reload; OS autostart provider isolated",
            ) as e:
                if row["section"] in v._titles:
                    v._list.setCurrentRow(v._titles.index(row["section"]))
                    app.processEvents()
                set_value(c, alt)
                buttons = [b for b in v.findChildren(QPushButton) if b.text() == "Save changes"]
                assert len(buttons) == 1
                QTest.mouseClick(buttons[0], Qt.MouseButton.LeftButton)
                app.processEvents()
                assert v._saved.text() == "Saved"
                freshdb = Database(root / "settings.db")
                fresh = SettingsDialog(Settings(freshdb))
                try:
                    assert value(control(fresh, field)) == value(c), (
                        field,
                        value(c),
                        value(control(fresh, field)),
                    )
                finally:
                    fresh.reject()
                    fresh.deleteLater()
                    freshdb.close()
                before = db.all_settings()
                cancel = SettingsDialog(s)
                set_value(control(cancel, field), original)
                cancel.reject()
                cancel.deleteLater()
                assert db.all_settings() == before
                e.update(
                    section=row["section"],
                    changed=original != alt,
                    cancel_preserved_store=True,
                    reopened_value_verified=True,
                )
            set_value(c, original)
            assert d.apply()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            gc.collect()
        save("reopen-controls.json", inventory)
finally:
    timer.stop()
    w.shutdown()
    manager.shutdown()
    threads.shutdown()
    db.close()
