"""Wall-clock speed window and per-job delayed/dependent starts through Qt dialogs."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
from common import ROOT, case, digest, save
from PySide6.QtCore import QDateTime, QTime, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDateTimeEdit, QDialogButtonBox, QInputDialog

from app.core import launcher
from app.core.manager import DownloadManager
from app.core.models import JobStatus
from app.core.settings import Settings
from app.db.database import Database
from app.tests.media_server import MediaServer, payload, sha256
from app.tests.qt_app import ensure_app
from app.ui.main_window import MainWindow
from app.ui.settings_dialog import SettingsDialog

app = ensure_app()
root = ROOT / "wall-clock" / str(time.time_ns())
root.mkdir(parents=True)
os.environ["LOCALAPPDATA"] = str(root / "profile")
db = Database(root / "data.db")
s = Settings(db)
s.setup_seen = s.host_registered = True
s.check_updates = s.confirm_downloads = s.confirm_video_quality = False
s.download_dir = root
s.min_free_mb = 0
manager = DownloadManager(db, settings=s, max_concurrent=0)
window = MainWindow(manager, s)
window.show()
app.processEvents()
server = MediaServer()
server.start()
when = (datetime.now() + timedelta(minutes=2)).replace(second=0, microsecond=0)
events = []
try:
    with case(
        "SCHEDULE-UI-SPEED-WINDOW-DELAY-DEPENDENCY",
        "real Qt settings/job dialogs + real clock + HTTP transfers",
    ) as e:
        with patch.object(launcher, "set_autostart", lambda _: None):
            d = SettingsDialog(s)
            d.speed_spin.setValue(128)
            d.schedule_check.setChecked(True)
            d.full_from.setTime(QTime(when.hour, when.minute))
            end = when + timedelta(minutes=2)
            d.full_to.setTime(QTime(end.hour, end.minute))
            d.apply()
            d.close()
        assert s.speed_limit_kbps == 128 and s.speed_schedule_enabled
        large = payload(64 * 1024**2, 623)
        small = payload(1024**2, 624)
        slow = db.create_job(
            server.add("/large.bin", large), str(root), "large.bin", options={"connections": 1}
        )
        a = db.create_job(
            server.add("/delayed.bin", small), str(root), "delayed.bin", options={"connections": 1}
        )
        b = db.create_job(
            server.add("/dependent.bin", small),
            str(root),
            "dependent.bin",
            options={"connections": 1},
        )
        window.refresh()
        views = {v.id: v for v in manager.snapshot()}

        def choose_time():
            d = app.activeModalWidget()
            edit = d.findChild(QDateTimeEdit)
            assert edit is not None
            edit.setDateTime(QDateTime(when))
            d.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()

        QTimer.singleShot(0, choose_time)
        window._pick_start_at(views[a.id])
        window.refresh()

        def choose_parent():
            d = app.activeModalWidget()
            assert isinstance(d, QInputDialog)
            d.setTextValue("delayed.bin")
            d.accept()

        QTimer.singleShot(0, choose_parent)
        window._pick_start_after(views[b.id])
        manager._max_concurrent_override = 3
        manager.reload_settings()
        deadline = time.monotonic() + 180
        last = None
        observed_before = False
        unlimited = False
        started_a = None
        started_b = None
        while time.monotonic() < deadline:
            app.processEvents()
            jobs = [db.get_job(j.id) for j in [slow, a, b]]
            states = [j.status.value for j in jobs]
            now = datetime.now()
            row = {
                "time": now.isoformat(),
                "states": states,
                "rate": manager.limiter.rate,
                "bytes": jobs[0].downloaded,
            }
            if states != last:
                events.append(row)
                last = states
            if now < when:
                assert jobs[1].status is JobStatus.QUEUED and jobs[2].status is JobStatus.QUEUED, (
                    row
                )
                if jobs[0].status is JobStatus.DOWNLOADING:
                    assert manager.limiter.rate == 128 * 1024
                    observed_before = True
            else:
                if manager.limiter.rate == 0:
                    unlimited = True
                if started_a is None and jobs[1].status is not JobStatus.QUEUED:
                    started_a = now.isoformat()
                if started_b is None and jobs[2].status is not JobStatus.QUEUED:
                    assert jobs[1].status is JobStatus.COMPLETED, row
                    started_b = now.isoformat()
            if all(j.status is JobStatus.COMPLETED for j in jobs):
                break
            assert not any(j.status is JobStatus.FAILED for j in jobs), [
                (j.id, j.error) for j in jobs
            ]
            QTest.qWait(30)
        assert all(db.get_job(j.id).status is JobStatus.COMPLETED for j in [slow, a, b]), events
        assert observed_before and unlimited and started_a and started_b
        assert digest(slow.dest_path) == sha256(large)
        assert digest(a.dest_path) == digest(b.dest_path) == sha256(small)
        e.update(
            scheduled_at=when.isoformat(),
            events=events,
            job_ids=[slow.id, a.id, b.id],
            large_sha256=sha256(large),
            small_sha256=sha256(small),
            started_a=started_a,
            started_b=started_b,
            host_clock_changed=False,
            autostart_boundary_stubbed=True,
        )
finally:
    save("wall-clock-events.json", events)
    window.hide()
    manager.shutdown()
    server.stop()
    db.close()
