"""Test-owned native Qt dialog with live metadata churn and concurrent imports."""

import argparse
import faulthandler
import os
import threading
import time

from common import ROOT, save
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from app.core.manager import DownloadManager
from app.core.settings import Settings
from app.db.database import Database
from app.engines.smart import MediaInfo, QualityOption
from app.ui import threads
from app.ui.main_window import MainWindow
from app.ui.quality_panel import QualityPanel

parser = argparse.ArgumentParser()
parser.add_argument("--seconds", type=int, default=100)
parser.add_argument("--all-threads", type=int, choices=(-1, 0, 1), default=-1)
args = parser.parse_args()
root = ROOT / "native-crash"
root.mkdir(exist_ok=True)
os.environ["LOCALAPPDATA"] = str(root / "profile")
os.environ.pop("QT_QPA_PLATFORM", None)
crash = (root / "crash.log").open("a")
if args.all_threads == -1:
    from app.__main__ import _enable_native_crash_log

    _enable_native_crash_log(crash)
else:
    faulthandler.enable(crash, all_threads=bool(args.all_threads))
app = QApplication([])
db = Database(root / "settings.db")
s = Settings(db)
s.download_dir = root
s.check_updates = False
manager = DownloadManager(db, settings=s, max_concurrent=0)
window = MainWindow(manager, s)


def media(index):
    return MediaInfo(
        "https://example.invalid/qa",
        "qa",
        "Synthetic QA media " + str(index),
        "GrabLine QA",
        5,
        None,
        (
            QualityOption("720p", "video", "best", height=720),
            QualityOption("MP3", "audio", "bestaudio", audio_format="mp3"),
        ),
    )


panel = QualityPanel(media(0), parent=window)
panel.setWindowTitle("GrabLine QA Crash Probe")
panel.show()


def warm():
    from yt_dlp.extractor import gen_extractor_classes

    list(gen_extractor_classes())


threading.Thread(target=warm, daemon=True).start()
started = time.monotonic()
updates = [0]


def tick():
    updates[0] += 1
    panel.apply_media(media(updates[0]))
    save(
        "native-crash/heartbeat.json",
        {
            "pid": os.getpid(),
            "updates": updates[0],
            "elapsed": time.monotonic() - started,
            "title": panel.windowTitle(),
            "platform": app.platformName(),
            "visible": panel.isVisible(),
            "window_id": int(panel.winId()),
        },
    )
    if time.monotonic() - started >= args.seconds:
        app.quit()


timer = QTimer()
timer.timeout.connect(tick)
timer.start(100)
try:
    app.exec()
finally:
    window.shutdown()
    manager.shutdown()
    threads.shutdown()
    db.close()
    save(
        "native-crash/completed.json",
        {"updates": updates[0], "elapsed": time.monotonic() - started, "normal_shutdown": True},
    )
    faulthandler.disable()
    crash.close()
