"""Genuine RAR extraction and real completion-triggered archive processing."""

from __future__ import annotations

import os
import time
import zipfile

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from common import ROOT, case, digest

from app.core import archive
from app.core.manager import DownloadManager
from app.core.models import JobStatus
from app.core.settings import Settings
from app.db.database import Database
from app.tests.qt_app import ensure_app
from app.ui import threads
from app.ui.main_window import MainWindow

app = ensure_app()
root = ROOT / "archive-expanded"
root.mkdir(exist_ok=True)
for name in ("rar3-subdirs.rar", "rar5-crc.rar"):
    with case("ARCHIVE-" + name.split("-")[0].upper() + "-REAL", "local integration") as evidence:
        bundle = ROOT / "rar-fixtures" / name
        original = digest(bundle)
        entries = archive.list_entries(bundle)
        files = [e for e in entries if not e.is_dir]
        assert files
        selected = next((e.name for e in files if not e.name.isascii()), files[0].name)
        selected_out = archive.extract(bundle, root / (name + "-selected"), members=[selected])
        found = [
            p.relative_to(selected_out).as_posix() for p in selected_out.rglob("*") if p.is_file()
        ]
        assert found == [selected.replace("\\", "/")], found
        out = archive.extract(bundle, root / (name + "-all"))
        assert len([p for p in out.rglob("*") if p.is_file()]) == len(files)
        assert digest(bundle) == original
        evidence.update(
            archive_sha256=original,
            selected=selected,
            outputs={str(p.relative_to(out)): digest(p) for p in out.rglob("*") if p.is_file()},
        )

db = Database(root / "jobs.db")
settings = Settings(db)
settings.check_updates = False
settings.auto_extract = True
settings.scan_before_extract = False
settings.extract_to_subfolder = True
settings.delete_archive_after_extract = True
manager = DownloadManager(db, settings=settings, max_concurrent=0)
window = MainWindow(manager, settings)
window.show()
app.processEvents()
try:
    for broken in (False, True):
        with case(
            "ARCHIVE-AUTO-" + ("FAILURE-PRESERVES" if broken else "DELETE-AFTER-SUCCESS"),
            "Qt completion integration",
        ) as evidence:
            bundle = root / ("broken.zip" if broken else "automatic.zip")
            if broken:
                bundle.write_bytes(b"not an archive")
            else:
                with zipfile.ZipFile(bundle, "w") as stream:
                    stream.writestr("qa.txt", "Automatic extraction verified")
            job = db.create_job("https://fixture.test/" + bundle.name, str(root), bundle.name)
            window.refresh()
            db.set_job_status(job.id, JobStatus.COMPLETED)
            window.refresh()
            deadline = time.monotonic() + 10
            while window._file_ops and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            assert not window._file_ops
            assert job.id in window._auto_extracted
            if broken:
                assert bundle.read_bytes() == b"not an archive"
                assert "Did not extract" in window.statusBar().currentMessage()
            else:
                assert not bundle.exists()
                assert (root / "automatic/qa.txt").read_text() == "Automatic extraction verified"
            evidence["job_id"] = job.id
finally:
    window.shutdown()
    manager.shutdown()
    window.close()
    db.close()
    threads.shutdown()
