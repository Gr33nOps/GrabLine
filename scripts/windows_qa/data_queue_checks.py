"""Isolated real Qt dialogs, file operations and wall-clock queue scheduling."""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from common import ROOT, case, digest
from PySide6.QtCore import Qt, QTime, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTimeEdit,
)

from app.core.manager import DownloadManager
from app.core.models import JobStatus
from app.core.settings import Settings
from app.db.database import _HANDOFFS_MIGRATIONS, _JOBS_MIGRATIONS, Database
from app.tests.media_server import MediaServer, payload
from app.tests.qt_app import ensure_app
from app.ui import threads
from app.ui.main_window import MainWindow
from app.ui.queue_view import QueueView
from app.ui.settings_dialog import SettingsDialog

root = ROOT / "data-queues"
root.mkdir(exist_ok=True)
os.environ["LOCALAPPDATA"] = str(root / "profile")
app = ensure_app()
app.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs)
db = Database(root / "history.db")
s = Settings(db)
s.download_dir = root
s.confirm_downloads = s.confirm_video_quality = s.check_updates = False
s.setup_seen = s.host_registered = True
s.min_free_mb = 0
manager = DownloadManager(db, settings=s, max_concurrent=0)
window = MainWindow(manager, s)
window.show()
app.processEvents()


def wait(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        QTest.qWait(30)
    raise TimeoutError("Observable condition did not arrive")


def click(widget, text):
    buttons = [
        b
        for b in widget.findChildren(QPushButton)
        if b.text().strip() == text or b.toolTip() == text
    ]
    assert len(buttons) == 1, (text, [b.text() for b in widget.findChildren(QPushButton)])
    QTest.mouseClick(buttons[0], Qt.MouseButton.LeftButton)
    app.processEvents()


def modal(action, handler):
    """Fill actual Qt dialogs; deadline closes a failed test, never a user dialog."""
    errors = []
    handled = []
    timer = QTimer()
    timer.setInterval(30)
    started = time.monotonic()

    def poll():
        widget = QApplication.activeModalWidget()
        if widget is None:
            return
        if time.monotonic() - started > 8:
            errors.append("dialog automation deadline")
            widget.reject()
            return
        if isinstance(widget, QMessageBox):
            widget.button(
                QMessageBox.StandardButton.Yes
                if widget.standardButtons() & QMessageBox.StandardButton.Yes
                else QMessageBox.StandardButton.Ok
            ).click()
            return
        if id(widget) in handled:
            return
        try:
            handler(widget)
            handled.append(id(widget))
        except Exception as exc:
            errors.append(str(exc))
            widget.reject()

    timer.timeout.connect(poll)
    timer.start()
    try:
        action()
    finally:
        timer.stop()
    assert not errors, errors
    assert handled, "Expected dialog was never shown"


def choose(path):
    def fill(dialog):
        assert isinstance(dialog, QFileDialog), type(dialog)
        edit = dialog.findChild(QLineEdit, "fileNameEdit")
        assert edit is not None
        edit.setText(str(path))
        assert Path(dialog.selectedFiles()[0]) == path, dialog.selectedFiles()
        dialog.accept()

    return fill


try:
    with case(
        "QT-DATA-RENAME-TAGS-SEARCH", "real Qt dialogs + real files + isolated SQLite"
    ) as evidence:
        target = root / "synthetic-original.bin"
        target.write_bytes(b"GrabLine synthetic data recovery fixture" * 128)
        original_sha = digest(target)
        j = db.create_job("http://example.invalid/synthetic.bin", str(root), target.name)
        db.set_job_status(j.id, JobStatus.COMPLETED)
        window.refresh()
        view = next(v for v in manager.snapshot() if v.id == j.id)

        def rename(dialog):
            assert isinstance(dialog, QInputDialog)
            dialog.setTextValue("synthetic-renamed.bin")
            dialog.accept()

        modal(lambda: window._rename_view(view), rename)
        renamed = root / "synthetic-renamed.bin"
        assert renamed.exists() and not target.exists() and digest(renamed) == original_sha
        view = next(v for v in manager.snapshot() if v.id == j.id)

        def tags(dialog):
            dialog.findChild(QLineEdit).setText("qa, recovery")
            dialog.findChild(QPlainTextEdit).setPlainText("Synthetic note, no personal history")
            dialog.accept()

        modal(lambda: window._edit_tags(view), tags)
        window.refresh()
        for query in ("synthetic-renamed", "recovery", "Synthetic note"):
            window.search_box.setText(query)
            assert any(not window.table.isRowHidden(i) for i in range(window.table.rowCount())), (
                query
            )
        window.search_box.setText("definitely-absent-marker")
        assert all(window.table.isRowHidden(i) for i in range(window.table.rowCount()))
        window.search_box.clear()
        evidence.update(job_id=j.id, sha256=original_sha, renamed=renamed.name)

    with case("QT-DATA-LIST-EXPORT-IMPORT", "real Qt file dialogs + isolated history") as evidence:
        exported = root / "history-export.json"
        exported.unlink(missing_ok=True)
        modal(window._export_list, choose(exported))
        text = exported.read_text(encoding="utf-8")
        assert "synthetic.bin" in text and "Cookie" not in text
        before = len(db.list_jobs())
        modal(window._import_list, choose(exported))
        assert len(db.list_jobs()) > before
        evidence.update(
            export_sha256=digest(exported), rows_before=before, rows_after=len(db.list_jobs())
        )

    with case(
        "QT-DATA-SETTINGS-BACKUP-RESET-RESTORE",
        "real Qt file/confirmation dialogs + isolated settings",
    ) as evidence:
        s.theme = "light"
        s.speed_limit_kbps = 157
        s.virustotal_key = "qa-disposable-never-used"
        d = SettingsDialog(s)
        exported = root / "settings-export.json"
        exported.unlink(missing_ok=True)
        modal(d._export_settings, choose(exported))
        raw = exported.read_text(encoding="utf-8")
        assert "qa-disposable-never-used" not in raw and '"157"' in raw
        count = len(db.list_jobs())
        # This reset confirmation has no input dialog; answer its real message box.
        timer = QTimer()

        def answer_reset():
            widget = QApplication.activeModalWidget()
            if isinstance(widget, QMessageBox):
                timer.stop()
                QTest.keyClick(widget, Qt.Key.Key_Y, Qt.KeyboardModifier.AltModifier)
                QTimer.singleShot(5000, lambda: widget.reject() if widget.isVisible() else None)

        timer.timeout.connect(answer_reset)
        timer.start(30)
        try:
            d._reset_settings()
        finally:
            timer.stop()
        assert s.speed_limit_kbps == 0 and len(db.list_jobs()) == count
        assert s.setup_seen and s.host_registered
        modal(d._import_settings, choose(exported))
        assert Settings(db).speed_limit_kbps == 157 and s.theme == "light"
        assert db.integrity_check() == "ok"
        db.vacuum()
        assert db.integrity_check() == "ok"
        evidence.update(settings_sha256=digest(exported), history_rows_preserved=count)
        d.reject()

    with case(
        "QT-DATA-SYNTHETIC-MIGRATION", "real SQLite migration on synthetic old-schema copy"
    ) as evidence:
        old = root / "old-schema.db"
        fresh = Database(old)
        sample = fresh.create_job(
            "http://example.invalid/migration.bin", str(root), "migration.bin"
        )
        fresh.close()
        with sqlite3.connect(old) as conn:
            for table, columns in (("jobs", _JOBS_MIGRATIONS), ("handoffs", _HANDOFFS_MIGRATIONS)):
                for column in columns:
                    conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        migrated = Database(old)
        assert migrated.get_job(sample.id).filename == "migration.bin"
        assert migrated.integrity_check() == "ok"
        migrated.close()
        evidence["sha256"] = digest(old)

    with case(
        "QT-QUEUE-UI-WALL-CLOCK", "real Qt queue editor + real HTTP + actual wall clock"
    ) as evidence:
        server = MediaServer()
        server.start()
        try:
            data = payload(512 * 1024, 134)
            url = server.add("/queue.bin", data, delay_per_chunk=0.08)
            q = manager.create_queue("Scheduled QA")
            qv = QueueView(manager)
            qv.show()
            qv._toggle_edit(q.id)
            editor = qv.findChildren(QLineEdit)[0].parentWidget()
            # The inline editor owns these real widgets and its Save signal persists them.
            qv.findChildren(QLineEdit)[0].setText("Scheduled QA edited")
            qv.findChildren(QSpinBox)[0].setValue(1)
            checks = {w.text(): w for w in qv.findChildren(QCheckBox)}
            checks["Only between"].setChecked(True)
            start = (datetime.now() + timedelta(minutes=2)).replace(second=0, microsecond=0)
            end = start + timedelta(minutes=2)
            times = qv.findChildren(QTimeEdit)
            times[0].setTime(QTime(start.hour, start.minute))
            times[1].setTime(QTime(end.hour, end.minute))
            click(qv, "Save")
            saved = db.get_queue(q.id)
            assert (
                saved.name == "Scheduled QA edited"
                and saved.max_concurrent == 1
                and saved.schedule_enabled
            )
            scheduled = manager.add_url(
                url, root / "scheduled", filename="queue.bin", queue_id=q.id
            )
            manager._max_concurrent_override = 2
            manager.reload_settings()
            QTest.qWait(1000)
            assert db.get_job(scheduled.id).status is JobStatus.QUEUED
            wait(lambda: db.get_job(scheduled.id).status is JobStatus.COMPLETED, 145)
            final = db.get_job(scheduled.id)
            assert digest(final.dest_path) == __import__("hashlib").sha256(data).hexdigest()
            assert datetime.now() >= start
            evidence.update(
                start=start.isoformat(),
                end=end.isoformat(),
                observed_completion=datetime.now().isoformat(),
                job_id=final.id,
                sha256=digest(final.dest_path),
            )
            qv.close()
        finally:
            server.stop()
finally:
    window.shutdown()
    manager.shutdown()
    db.close()
    threads.shutdown()
