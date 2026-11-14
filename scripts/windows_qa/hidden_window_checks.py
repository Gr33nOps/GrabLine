"""Real background transfers and completion actions while the Qt window is hidden."""

from __future__ import annotations

import io
import os
import time
import zipfile

os.environ["QT_QPA_PLATFORM"] = "offscreen"
from common import ROOT, case, digest
from PySide6.QtTest import QTest

from app.core.manager import DownloadManager
from app.core.models import JobStatus
from app.core.settings import Settings
from app.db.database import Database
from app.tests.media_server import MediaServer, payload, sha256
from app.tests.qt_app import ensure_app
from app.ui import threads
from app.ui.main_window import MainWindow

app = ensure_app()
root = ROOT / "hidden-transfers" / str(time.time_ns())
root.mkdir(parents=True)
os.environ["LOCALAPPDATA"] = str(root / "profile")
server = MediaServer()
server.start()

for failure in (False, True):
    with case(
        "DESKTOP-HIDDEN-" + ("FAILURE" if failure else "COMPLETION-EXTRACTION"),
        "real hidden Qt window + actual HTTP + completion actions",
    ) as evidence:
        directory = root / ("failed" if failure else "completed")
        directory.mkdir()
        content = payload(65536, 67)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("payload.bin", content)
        data = buffer.getvalue()
        url = server.add(
            "/failed.zip" if failure else "/completed.zip",
            data,
            delay_per_chunk=0.2,
            fail_status=403 if failure else None,
            fail_until=10000 if failure else 0,
        )
        db = Database(directory / "data.db")
        s = Settings(db)
        s.download_dir = directory
        s.auto_retry = False
        s.auto_extract = s.extract_to_subfolder = s.delete_archive_after_extract = True
        manager = DownloadManager(db, settings=s, max_concurrent=0)
        window = MainWindow(manager, s)
        signals = []
        window.job_completed.connect(lambda *_, signals=signals: signals.append("completed"))
        window.job_failed.connect(lambda *_, signals=signals: signals.append("failed"))
        try:
            job = db.create_job(url, str(directory), "archive.zip")
            window.show()
            window.refresh()
            window.hide()
            manager._max_concurrent_override = 1
            manager.reload_settings()
            deadline = time.monotonic() + 30
            extracted = directory / "archive/payload.bin"
            while time.monotonic() < deadline:
                app.processEvents()
                if failure and "failed" in signals:
                    break
                if (
                    not failure
                    and "completed" in signals
                    and extracted.exists()
                    and not job.dest_path.exists()
                ):
                    break
                QTest.qWait(30)
            assert not window.isVisible()
            assert signals == ["failed" if failure else "completed"], signals
            if failure:
                assert db.get_job(job.id).status is JobStatus.FAILED
            else:
                assert digest(extracted) == sha256(content)
                assert not job.dest_path.exists()
                evidence.update(
                    extracted_sha256=digest(extracted), archive_deleted_after_success=True
                )
            evidence.update(job_id=job.id, signals=signals, hidden=True)
        finally:
            window.shutdown()
            manager.shutdown()
            db.close()

server.stop()
threads.shutdown()
