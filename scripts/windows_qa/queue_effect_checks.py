"""Actual queue editor/buttons with real sequential/dependent/parallel HTTP jobs."""

from __future__ import annotations

import os
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"
from common import ROOT, case, digest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QInputDialog,
    QLabel,
    QPushButton,
    QSpinBox,
)

from app.core.manager import DownloadManager
from app.core.models import JobStatus
from app.core.settings import Settings
from app.db.database import Database
from app.tests.media_server import MediaServer, payload, sha256
from app.tests.qt_app import ensure_app
from app.ui.queue_view import QueueView

app = ensure_app()
root = ROOT / "queue-effects" / str(time.time_ns())
root.mkdir(parents=True)
os.environ["LOCALAPPDATA"] = str(root / "profile")
db = Database(root / "data.db")
settings = Settings(db)
settings.download_dir = root
settings.auto_retry = False
manager = DownloadManager(db, settings=settings, max_concurrent=0)
view = QueueView(manager)
view.resize(1000, 900)
view.show()
app.processEvents()
server = MediaServer()
server.start()
data = payload(1024**2, 907)


def wait(predicate, timeout=25):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if predicate():
            return
        QTest.qWait(20)
    raise TimeoutError("Queue state did not arrive")


def click(widget, label):
    buttons = [
        b for b in widget.findChildren(QPushButton) if b.toolTip() == label or b.text() == label
    ]
    assert len(buttons) == 1, (label, [b.toolTip() for b in buttons])
    QTest.mouseClick(buttons[0], Qt.MouseButton.LeftButton)
    app.processEvents()


def new_queue(name, concurrent, dependency=None, paused=False):
    def fill():
        dialog = app.activeModalWidget()
        assert isinstance(dialog, QInputDialog)
        dialog.setTextValue(name)
        dialog.accept()

    QTimer.singleShot(0, fill)
    click(view, "New queue")
    view.findChildren(QSpinBox)[0].setValue(concurrent)
    checks = {w.text(): w for w in view.findChildren(QCheckBox)}
    checks["Paused"].setChecked(paused)
    if dependency is not None:
        combos = [w for w in view.findChildren(QComboBox) if w.findData(dependency) >= 0]
        assert len(combos) == 1
        combos[0].setCurrentIndex(combos[0].findData(dependency))
    click(view, "Save")
    return next(q for q in db.list_queues() if q.name == name)


def card(name):
    cards = [
        w
        for w in view.findChildren(QFrame)
        if w.property("card") == "true"
        and any(label.text() == name for label in w.findChildren(QLabel))
    ]
    assert len(cards) == 1
    return cards[0]


def job(name, queue):
    url = server.add("/" + name + ".bin", data, delay_per_chunk=0.1)
    return db.create_job(
        url, str(root), name + ".bin", queue_id=queue.id, options={"connections": 1}
    )


try:
    with case(
        "QUEUE-UI-SEQUENTIAL-DEPENDENCY-PARALLEL",
        "real Qt queue dialogs/editor/buttons + real HTTP scheduler",
    ) as e:
        a = new_queue("Sequential QA", 1, paused=True)
        b = new_queue("Dependent QA", 2, dependency=a.id)
        assert db.get_queue(a.id).paused and db.get_queue(b.id).depends_on == a.id
        aa = [job("a1", a), job("a2", a)]
        bb = [job("b1", b), job("b2", b)]
        manager._max_concurrent_override = 4
        manager.reload_settings()
        QTest.qWait(700)
        app.processEvents()
        assert all(db.get_job(j.id).status is JobStatus.QUEUED for j in aa + bb)
        click(card(a.name), "Resume queue")
        wait(lambda: db.get_job(aa[0].id).status is JobStatus.DOWNLOADING)
        assert db.get_job(aa[1].id).status is JobStatus.QUEUED
        assert all(db.get_job(j.id).status is JobStatus.QUEUED for j in bb)
        wait(lambda: db.get_job(aa[1].id).status is JobStatus.DOWNLOADING)
        assert db.get_job(aa[0].id).status is JobStatus.COMPLETED
        assert all(db.get_job(j.id).status is JobStatus.QUEUED for j in bb)
        wait(lambda: all(db.get_job(j.id).status is JobStatus.DOWNLOADING for j in bb))
        wait(lambda: all(db.get_job(j.id).status is JobStatus.COMPLETED for j in aa + bb))
        assert all(digest(j.dest_path) == sha256(data) for j in aa + bb)
        e.update(
            sequential_job_ids=[j.id for j in aa],
            simultaneously_observed_job_ids=[j.id for j in bb],
            sha256=sha256(data),
            paused_queue_held_jobs=True,
            dependency_blocked_until_all_a_completed=True,
        )
    with case("QUEUE-UI-PRIORITY-ORDER", "real Qt Move up button + actual job start order") as e:
        manager._max_concurrent_override = 0
        c = new_queue("Priority later", 1)
        d = new_queue("Priority first", 1)
        jc = job("priority-later", c)
        jd = job("priority-first", d)
        click(card(d.name), "Move up the running order")
        assert db.get_queue(d.id).position < db.get_queue(c.id).position
        manager._max_concurrent_override = 1
        manager.reload_settings()
        wait(lambda: db.get_job(jd.id).status is JobStatus.DOWNLOADING)
        assert db.get_job(jc.id).status is JobStatus.QUEUED
        wait(lambda: all(db.get_job(j.id).status is JobStatus.COMPLETED for j in [jc, jd]))
        assert digest(jc.dest_path) == digest(jd.dest_path) == sha256(data)
        e.update(first_job=jd.id, added_earlier_but_waited=jc.id, sha256=sha256(data))
finally:
    view.close()
    manager.shutdown()
    server.stop()
    db.close()
