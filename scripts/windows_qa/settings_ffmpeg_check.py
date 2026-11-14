"""Execute the genuine FFmpeg settings installer in a disposable app profile."""

import os
import subprocess
import time
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
from common import ROOT, case, digest, record
from PySide6.QtWidgets import QMessageBox

from app.core.ffmpeg import managed_binary
from app.core.settings import Settings
from app.db.database import Database
from app.tests.qt_app import ensure_app
from app.ui import threads
from app.ui.settings_dialog import SettingsDialog

app = ensure_app()
root = ROOT / "settings-ffmpeg"
root.mkdir(exist_ok=True)
os.environ["LOCALAPPDATA"] = str(root / "profile")
db = Database(root / "settings.db")
d = SettingsDialog(Settings(db))
messages = []
with (
    patch.object(
        QMessageBox, "information", side_effect=lambda *a: messages.append(("info", a[-1]))
    ),
    patch.object(
        QMessageBox, "warning", side_effect=lambda *a: messages.append(("warning", a[-1]))
    ),
):
    try:
        with case(
            "SETTING-ACTION-FFMPEG-INSTALL",
            "real Qt button + pinned HTTPS archive + executable validation",
        ) as e:
            d.ffmpeg_button.click()
            first = d._installer
            assert first is not None
            d.ffmpeg_button.click()
            assert d._installer is first  # single-flight protection
            deadline = time.monotonic() + 180
            while first.isRunning() and time.monotonic() < deadline:
                app.processEvents()
                first.wait(20)
            assert not first.isRunning(), "Installer exceeded 180 seconds"
            app.processEvents()
            assert messages and messages[-1][0] == "info", messages
            assert "ffmpeg" not in d._in_flight
            binary = managed_binary("ffmpeg")
            probe = managed_binary("ffprobe")
            for tool in (binary, probe):
                result = subprocess.run(
                    [str(tool), "-version"], capture_output=True, text=True, timeout=15
                )
                assert result.returncode == 0, result.stderr
            assert d.ffmpeg_button.text() == "Reinstall"
            assert str(binary) in d.ffmpeg_status.text()
            e.update(ffmpeg_sha256=digest(binary), ffprobe_sha256=digest(probe), single_flight=True)
        # Replace a previous unexecuted placeholder with a precise pointer.
        record(
            "SETTING-EXTERNAL-FFMPEG-INSTALL",
            "NOT APPLICABLE",
            "superseded prerequisite placeholder",
            "Actual installer executed as SETTING-ACTION-FFMPEG-INSTALL; see that result.",
        )
    finally:
        threads.shutdown(timeout_ms=200_000)
        d.reject()
        db.close()
