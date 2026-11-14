"""Controlled playlist metadata, real native Qt selection and actual media files."""

from __future__ import annotations

import json
import os
import subprocess
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"
from common import ROOT, case, digest
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialogButtonBox

from app.core.manager import DownloadManager
from app.core.models import JobKind, JobStatus
from app.core.resolver import Resolution
from app.core.settings import Settings
from app.db.database import Database
from app.engines.smart import PlaylistEntry, PlaylistInfo
from app.tests.qt_app import ensure_app
from app.ui import threads
from app.ui.main_window import MainWindow
from app.ui.playlist_panel import PlaylistPanel

app = ensure_app()
root = ROOT / "playlist-transfers" / str(time.time_ns())
root.mkdir(parents=True)
os.environ["LOCALAPPDATA"] = str(root / "profile")
manifest = json.loads((ROOT / "browser-manifest.json").read_text())
db = Database(root / "data.db")
s = Settings(db)
s.download_dir = root
s.min_free_mb = 0
s.ask_save_dir = False
s.playlist_batch_cap = 2
manager = DownloadManager(db, settings=s, max_concurrent=2)
w = MainWindow(manager, s)
w.show()
app.processEvents()
info = PlaylistInfo(
    manifest["base"] + "/playlist",
    "Controlled QA playlist",
    None,
    tuple(
        PlaylistEntry(
            manifest["base"] + "/video.mp4?playlist=" + str(root.name) + "&item=" + str(i),
            "Clip " + str(i),
            4,
            i,
        )
        for i in range(1, 6)
    ),
)
try:
    with case(
        "MEDIA-PLAYLIST-UI-CAP-CANCEL-DOWNLOAD",
        "controlled playlist metadata + real Qt dialogs + actual media transfers",
    ) as e:
        observed = []

        def choose(cancel):
            panel = app.activeModalWidget()
            assert isinstance(panel, PlaylistPanel)
            assert [x.index for x in panel.selected_entries()] == [1, 2]
            observed.append([x.index for x in panel.selected_entries()])
            panel.findChild(QDialogButtonBox).button(
                QDialogButtonBox.StandardButton.Cancel
                if cancel
                else QDialogButtonBox.StandardButton.Ok
            ).click()

        QTimer.singleShot(0, lambda: choose(True))
        w._on_resolved(Resolution(info.url, JobKind.SMART, playlist=info), None)
        assert not db.list_jobs()
        QTimer.singleShot(0, lambda: choose(False))
        w._on_resolved(Resolution(info.url, JobKind.SMART, playlist=info), None)
        end = time.monotonic() + 45
        while time.monotonic() < end:
            app.processEvents()
            jobs = db.list_jobs()
            if len(jobs) == 2 and all(
                j.status in [JobStatus.COMPLETED, JobStatus.FAILED] for j in jobs
            ):
                break
            QTest.qWait(30)
        assert len(jobs) == 2 and all(j.status is JobStatus.COMPLETED for j in jobs), [
            (j.id, j.status, j.error) for j in jobs
        ]
        outputs = []
        for j in jobs:
            p = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration:stream=codec_name",
                    "-of",
                    "json",
                    str(j.dest_path),
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert p.returncode == 0, p.stderr
            media = json.loads(p.stdout)
            assert float(media["format"]["duration"]) > 3.8
            outputs.append({"job_id": j.id, "sha256": digest(j.dest_path), "media": media})
        e.update(
            preselection_cap=2,
            total_entries=5,
            selection_observations=observed,
            cancel_added_zero_jobs=True,
            outputs=outputs,
            metadata_level="controlled PlaylistInfo; no live playlist discovery claim",
        )
finally:
    w.hide()
    manager.shutdown()
    threads.shutdown()
    db.close()
