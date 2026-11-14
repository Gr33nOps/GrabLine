"""Qt desktop event paths, real watch/RSS imports, clipboard and sound API."""

from __future__ import annotations

import math
import os
import struct
import time
import wave
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
from common import ROOT, case, digest
from PySide6.QtCore import QMimeData, QPointF, Qt, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QDropEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from app.core import alerts, launcher
from app.core.manager import DownloadManager
from app.core.models import JobKind, JobStatus
from app.core.settings import Settings
from app.db.database import Database
from app.engines.torrent import create_torrent_file
from app.tests.media_server import MediaServer, payload
from app.tests.qt_app import ensure_app
from app.ui import threads
from app.ui.main_window import MainWindow
from app.ui.settings_dialog import SettingsDialog
from app.ui.tray import GrabLineTray

root = ROOT / "desktop-watch" / str(time.time_ns())
root.mkdir(exist_ok=True, parents=True)
os.environ["LOCALAPPDATA"] = str(root / "profile")
app = ensure_app()
db = Database(root / "data.db")
settings = Settings(db)
settings.download_dir = root / "downloads"
settings.setup_seen = settings.host_registered = True
settings.confirm_downloads = settings.confirm_video_quality = settings.check_updates = False
settings.auto_retry = False
manager = DownloadManager(db, settings=settings, max_concurrent=0)
window = MainWindow(manager, settings)
window.show()
app.processEvents()
server = MediaServer()
server.start()


def wait(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        QTest.qWait(30)
    raise TimeoutError("Desktop readiness deadline: " + window.statusBar().currentMessage())


try:
    with case(
        "DESKTOP-KEYBOARD-NAVIGATION", "real Qt keyboard events + embedded views"
    ) as evidence:
        observed = []
        for key, page in (
            (Qt.Key.Key_2, "dashboard"),
            (Qt.Key.Key_3, "queue"),
            (Qt.Key.Key_4, "settings"),
            (Qt.Key.Key_1, "downloads"),
        ):
            window.activateWindow()
            app.processEvents()
            QTest.keyClick(window, key, Qt.KeyboardModifier.ControlModifier)
            wait(lambda page=page: window._current_page == page)
            observed.append(page)
        QTest.keyClick(window, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        assert window.search_box.hasFocus()
        evidence["pages"] = observed

    with case(
        "DESKTOP-CLIPBOARD-PASTE-DRAG-DROP", "real Qt clipboard/drop events + HTTP transfer"
    ) as evidence:
        data = payload(131072, 91)
        url = server.add("/clipboard.bin", data)
        previous_clipboard = app.clipboard().text()
        try:
            app.clipboard().setText(url)
            assert app.clipboard().text() == url
            window._paste_and_download()
            wait(lambda: bool(db.list_jobs()), 45)
            assert db.list_jobs()[-1].url == url
        finally:
            app.clipboard().setText(previous_clipboard)
        dropped = server.add("/drop.bin", data)
        mime = QMimeData()
        mime.setUrls([QUrl(dropped)])
        event = QDropEvent(
            QPointF(50, 50),
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        window.dropEvent(event)
        wait(lambda: len(db.list_jobs()) >= 2)
        assert any(j.url == dropped for j in db.list_jobs())
        manager._max_concurrent_override = 2
        manager.reload_settings()
        wait(lambda: all(j.status is JobStatus.COMPLETED for j in db.list_jobs()), 30)
        assert all(
            digest(j.dest_path) == __import__("hashlib").sha256(data).hexdigest()
            for j in db.list_jobs()
        )
        manager._max_concurrent_override = 0
        evidence["jobs"] = [{"id": j.id, "sha256": digest(j.dest_path)} for j in db.list_jobs()]

    with case(
        "DESKTOP-WATCH-RSS-UI",
        "real Qt settings + real filesystem/HTTP + application import controllers",
    ) as evidence:
        folder = root / "watch"
        folder.mkdir(exist_ok=True)
        original = root / "watch-payload.bin"
        original.write_bytes(payload(65536, 141))
        torrent = create_torrent_file(original)
        fixture = folder / "qa.torrent"
        fixture.write_bytes(torrent)
        remote = server.add(
            "/rss-payload.torrent", torrent, content_type="application/x-bittorrent"
        )
        feed = server.add(
            "/rss.xml",
            (
                "<rss><channel><item><guid>qa-rss-unique</guid>"
                '<title>QA selected release</title><enclosure url="'
                + remote
                + '"/></item><item><guid>qa-rss-excluded</guid>'
                '<title>Excluded release</title><enclosure url="'
                + remote
                + '?ignored=1"/></item></channel></rss>'
            ).encode(),
            content_type="application/rss+xml",
        )
        dialog = SettingsDialog(settings)
        dialog.watch_check.setChecked(True)
        dialog.watch_dir_edit.setText(str(folder))
        dialog.watch_dest_edit.setText(str(root / "watch-output"))
        dialog.watch_interval_spin.setValue(5)
        dialog.rss_edit.setPlainText(feed + " | QA selected")
        # Host login registration belongs in the disposable VM, not this widget case.
        with patch.object(launcher, "set_autostart"):
            assert dialog.apply()
        dialog.reject()
        before = len(db.list_jobs())
        window._poll_torrent_watch()
        wait(lambda: not window._watch_busy)
        window._poll_torrent_watch()
        wait(lambda: not window._watch_busy and len(db.list_jobs()) == before + 1)
        watched = db.list_jobs()[-1]
        assert watched.kind is JobKind.TORRENT and settings.torrent_watch_seen
        window._poll_torrent_watch()
        wait(lambda: not window._watch_busy)
        assert len(db.list_jobs()) == before + 1
        window._poll_rss()
        wait(lambda: "qa-rss-unique" in settings.rss_seen)
        assert len(db.list_jobs()) == before + 2
        window._poll_rss()
        QTest.qWait(200)
        app.processEvents()
        assert len(db.list_jobs()) == before + 2 and "qa-rss-excluded" not in settings.rss_seen
        assert Settings(db).rss_feeds == settings.rss_feeds and Settings(db).torrent_watch_seen
        evidence.update(
            watch_job_id=watched.id,
            rss_job_id=db.list_jobs()[-1].id,
            duplicate_imports=0,
            excluded_filtered=True,
            limitation=(
                "Import UI/controller verified; loopback swarm transfer is covered separately"
            ),
        )

    with case(
        "DESKTOP-TRAY-CLOSE-RESTORE", "real Qt tray actions/window state; offscreen shell"
    ) as evidence:
        tray = GrabLineTray(window)
        window.tray = tray
        window.close_to_tray = settings.close_to_tray = True
        window.show()
        close = QCloseEvent()
        window.closeEvent(close)
        assert not close.isAccepted() and not window.isVisible()
        tray._toggle_window()
        assert window.isVisible()
        tray._toggle_window()
        assert not window.isVisible()
        tray._toggle_window()
        evidence["limitation"] = (
            "Real Qt state/action path; physical taskbar tray interaction is a separate native case"
        )
        window.close_to_tray = False

    with case(
        "DESKTOP-ACTIVE-EXIT-CONFIRM", "real Qt confirmation dialog + active synthetic history"
    ) as evidence:
        active = db.create_job("http://example.invalid/held.bin", str(root), "held.bin")
        db.set_job_status(active.id, JobStatus.DOWNLOADING)
        window.refresh()
        settings.confirm_exit_active = True
        timer = QTimer()

        def answer():
            widget = QApplication.activeModalWidget()
            if isinstance(widget, QMessageBox):
                timer.stop()
                QTest.keyClick(widget, Qt.Key.Key_N, Qt.KeyboardModifier.AltModifier)

        timer.timeout.connect(answer)
        timer.start(30)
        event = QCloseEvent()
        window.closeEvent(event)
        timer.stop()
        assert not event.isAccepted() and window.isVisible()
        db.set_job_status(active.id, JobStatus.CANCELLED)
        evidence["declining_exit_preserved_window"] = True

    with case(
        "DESKTOP-SOUND-GENERATION-API",
        "generated WAV + Windows playback API; audibility not certified",
    ) as evidence:
        sound = root / "generated-completion.wav"
        with wave.open(str(sound), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(
                b"".join(
                    struct.pack("<h", int(1500 * math.sin(2 * math.pi * 440 * i / 16000)))
                    for i in range(3200)
                )
            )
        with wave.open(str(sound)) as wav:
            assert wav.getnframes() == 3200 and wav.getframerate() == 16000
        alerts._play(str(sound))
        evidence.update(
            sha256=digest(sound),
            duration_seconds=0.2,
            human_audibility="not confirmed",
            captured_loopback=False,
        )
finally:
    window.shutdown()
    manager.shutdown()
    server.stop()
    db.close()
    threads.shutdown()
