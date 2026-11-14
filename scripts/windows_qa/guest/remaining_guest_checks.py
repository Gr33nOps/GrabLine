# ruff: noqa: E402 -- Guest identity guard must run before app imports.
"""Execute only inside the test-owned Windows Sandbox; never on the host."""

from __future__ import annotations

import ctypes
import json
import os
import struct
import subprocess
import sys
import time
import winreg
from pathlib import Path
from unittest.mock import patch

identity = subprocess.check_output(
    ["powershell.exe", "-NoProfile", "-Command", "(Get-CimInstance Win32_ComputerSystem).Model"],
    text=True,
).strip()
assert os.environ.get("USERNAME") == "WDAGUtilityAccount" and identity == "Virtual Machine"
assert os.environ["COMPUTERNAME"].upper() != "DESKTOP-J5QDQCG"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["GRABLINE_QA_ROOT"] = r"C:\GrabLine-QA\Results"
os.environ["LOCALAPPDATA"] = r"C:\GrabLine-QA\Profile"
os.environ["QT_OPENGL"] = "software"
os.environ.pop("QT_QPA_PLATFORM", None)
from common import ROOT, Blocked, case, digest, record, save
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog, QPushButton

from app import __version__
from app.core import launcher
from app.core.manager import DownloadManager
from app.core.models import JobStatus
from app.core.settings import Settings
from app.db.database import Database
from app.tests.media_server import MediaServer, payload, sha256
from app.ui import threads
from app.ui.main_window import MainWindow
from app.ui.settings_dialog import SettingsDialog

phase = sys.argv[1] if len(sys.argv) > 1 else "functional"
save(
    "guest-identity.json",
    {
        "computer": os.environ["COMPUTERNAME"],
        "model": identity,
        "user": os.environ["USERNAME"],
        "phase": phase,
    },
)
app = QApplication([])
server = MediaServer()
server.start()
profile = Path(os.environ["LOCALAPPDATA"]) / "Grabline"
profile.mkdir(parents=True, exist_ok=True)
db = Database(profile / "grabline.db")
s = Settings(db)
s.setup_seen = True
s.check_updates = s.confirm_downloads = s.confirm_video_quality = False
s.download_dir = ROOT / "guest-downloads"
s.connections = s.max_concurrent = 1
s.ffmpeg_path = r"C:\GrabLine-QA\Tools\ffmpeg.exe"
helper_db = Database(profile / "helper.db")
helper_settings = Settings(helper_db)
helper_settings.download_dir = s.download_dir
helper_settings.check_updates = False
manager = DownloadManager(helper_db, settings=helper_settings, max_concurrent=0)
window = MainWindow(manager, helper_settings)
window.show()
process = None
exe = Path(r"C:\Build\dist\grabline\grabline.exe")
host = exe.with_name("grabline-host.exe")


def wait(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        QTest.qWait(30)
    raise TimeoutError("Guest readiness deadline: " + window.statusBar().currentMessage())


def native(message):
    body = json.dumps(message).encode()
    result = subprocess.run(
        [str(host)], input=struct.pack("<I", len(body)) + body, capture_output=True, timeout=20
    )
    assert result.returncode == 0 and len(result.stdout) >= 4
    size = struct.unpack("<I", result.stdout[:4])[0]
    return json.loads(result.stdout[4 : 4 + size])


def start_packaged():
    global process
    # Use the real installed executable when installation has completed.
    process = subprocess.Popen([str(exe), "--minimized"])
    wait(lambda: native({"type": "ping"}).get("appRunning"), 40)


def transfer(tag):
    data = payload(1024 * 1024, 901)
    url = server.add("/" + tag + ".bin", data, delay_per_chunk=0.3)
    reply = native({"type": "download", "url": url, "onlyIfRunning": True})
    assert reply["type"] == "queued", reply
    wait(
        lambda: (
            (j := db.latest_job_for_url(url)) is not None
            and j.status in (JobStatus.COMPLETED, JobStatus.FAILED)
        ),
        35,
    )
    job = db.latest_job_for_url(url)
    assert job.status is JobStatus.COMPLETED, job
    assert digest(job.dest_path) == sha256(data)
    return job


try:
    if phase == "functional":
        with case(
            "GUEST-REAL-AUTOSTART-REGISTRY", "native Qt Settings Save + actual guest HKCU"
        ) as e:
            dialog = SettingsDialog(s)
            warnings = []
            with patch.object(
                QMessageBox, "warning", side_effect=lambda *a: warnings.append(str(a[-1]))
            ):
                dialog.autostart_check.setChecked(True)
                assert dialog.apply()
                assert launcher.autostart_enabled(), warnings
                dialog.autostart_check.setChecked(False)
                assert dialog.apply()
                assert not launcher.autostart_enabled()
            dialog.show()
            app.processEvents()
            dialog.grab().save(str(ROOT / "guest-settings.png"))
            dialog.reject()
            e.update(enabled_and_removed=True, warnings=warnings)

        with case("GUEST-INSTALLER-BUILD", "genuine Inno compiler in disposable guest") as e:
            installer = ROOT / "innosetup-7.1.1-x64.exe"
            assert (
                digest(installer)
                == "8a5a6711c3766a6e5fb74f280ba0e3551e719ce94538d901fdbd1f8f9920b3f0"
            )
            result = subprocess.run(
                [
                    str(installer),
                    "/VERYSILENT",
                    "/SUPPRESSMSGBOXES",
                    "/NORESTART",
                    "/CURRENTUSER",
                    r"/DIR=C:\GrabLine-QA\Compiler",
                ],
                timeout=90,
            )
            assert result.returncode == 0, result.returncode
            compiler = Path(r"C:\GrabLine-QA\Compiler\ISCC.exe")
            with (ROOT / "inno-build.log").open("w", encoding="utf-8") as log:
                result = subprocess.run(
                    [
                        str(compiler),
                        f"/DAppVersion={__version__}",
                        "/O" + str(ROOT),
                        r"C:\Build\packaging\windows\grabline.iss",
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=180,
                )
            assert result.returncode == 0, (ROOT / "inno-build.log").read_text()[-2000:]
            setup = ROOT / f"Grabline-Setup-{__version__}.exe"
            assert setup.exists()
            e["sha256"] = digest(setup)
        with case("GUEST-INSTALL-SHORTCUTS-ASSOCIATIONS", "real per-user Inno install in guest"):
            setup = ROOT / f"Grabline-Setup-{__version__}.exe"
            result = subprocess.run(
                [
                    str(setup),
                    "/VERYSILENT",
                    "/SUPPRESSMSGBOXES",
                    "/NORESTART",
                    "/CURRENTUSER",
                    r"/DIR=C:\GrabLine-QA\Installed\GrabLine",
                    "/TASKS=desktopicon,startupicon,magnetassoc,torrentassoc",
                    "/LOG=" + str(ROOT / "install.log"),
                ],
                timeout=90,
            )
            assert result.returncode == 0, result.returncode
            exe = Path(r"C:\GrabLine-QA\Installed\GrabLine\grabline.exe")
            host = exe.with_name("grabline-host.exe")
            assert exe.exists() and host.exists()
            assert (Path(os.environ["USERPROFILE"]) / "Desktop/GrabLine.lnk").exists()
            assert launcher.autostart_enabled()
            for key_name in (
                r"Software\Classes\magnet\shell\open\command",
                r"Software\Classes\Grabline.Torrent\shell\open\command",
            ):
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name) as key:
                    assert str(exe).lower() in winreg.QueryValueEx(key, "")[0].lower()
        with case(
            "GUEST-NATIVE-HOST-REGISTRATION-TRANSFER",
            "installed GUI + real HKCU manifests/native host + HTTP",
        ) as e:
            s.host_registered = False
            start_packaged()
            for vendor in (
                r"Google\Chrome",
                "Chromium",
                r"Microsoft\Edge",
                r"BraveSoftware\Brave-Browser",
                "Vivaldi",
                "Mozilla",
            ):
                key_name = "Software\\" + vendor + r"\NativeMessagingHosts\dev.grabline.host"
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name) as key:
                    manifest = Path(winreg.QueryValueEx(key, "")[0])
                    assert manifest.exists(), manifest
                    assert Path(json.loads(manifest.read_text())["path"]).exists()
            job = transfer("installed-native")
            e.update(job_id=job.id, sha256=digest(job.dest_path), registered_browsers=6)

        with case(
            "GUEST-UPDATE-INSTALLER-HANDOFF",
            "production Qt download/hash/open flow + real benign executable",
        ) as e:
            csc = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
            if not csc.exists():
                raise Blocked("Guest C# compiler unavailable for inert installer fixture")
            marker = ROOT / "update-handoff.txt"
            marker.unlink(missing_ok=True)
            code = ROOT / "Fixture.cs"
            code.write_text(
                'class Fixture { static void Main() { System.IO.File.WriteAllText(@"'
                + str(marker)
                + '", "started"); }}',
                encoding="utf-8",
            )
            fixture = ROOT / "qa-update-fixture.exe"
            result = subprocess.run(
                [str(csc), "/nologo", "/target:winexe", "/out:" + str(fixture), str(code)],
                capture_output=True,
                timeout=30,
            )
            assert result.returncode == 0, result.stdout
            body = fixture.read_bytes()
            url = server.add("/qa-update.exe", body)
            window._download_and_run_installer(
                "qa-update-fixture.exe", url, len(body), sha256(body)
            )
            wait(marker.exists, 30)
            assert marker.read_text() == "started"
            e.update(sha256=sha256(body), real_process_started=True, fixture_is_inert=True)
        with case(
            "GUEST-UPDATE-BAD-HASH-NO-HANDOFF",
            "real Qt update download, rejection and no execution",
        ):
            marker.unlink(missing_ok=True)
            with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
                window._download_and_run_installer(
                    "bad-update-fixture.exe", url, len(body), "0" * 64
                )
                wait(lambda: not window._file_ops, 30)
            assert not marker.exists()
            assert not (s.download_dir / "bad-update-fixture.exe").exists()
        with case("GUEST-UPDATE-CANCEL-NO-HANDOFF", "real Qt Cancel button + interrupted transfer"):
            slow = server.add(
                "/slow-update.exe", payload(2 * 1024 * 1024, 401), delay_per_chunk=0.2
            )
            window._download_and_run_installer("cancel-update.exe", slow, 2 * 1024 * 1024, None)
            wait(
                lambda: any(
                    isinstance(x, QProgressDialog) and x.isVisible() for x in app.topLevelWidgets()
                )
            )
            progress = next(
                x for x in app.topLevelWidgets() if isinstance(x, QProgressDialog) and x.isVisible()
            )
            cancel = next(b for b in progress.findChildren(QPushButton) if b.text() == "Cancel")
            QTest.mouseClick(cancel, Qt.MouseButton.LeftButton)
            wait(lambda: not window._file_ops, 30)
            assert "cancel" in window.statusBar().currentMessage().lower()
            assert not (s.download_dir / "cancel-update.exe").exists() and not marker.exists()
        with case(
            "GUEST-QUEUE-QUIT", "installed app + real slow HTTP transfer + queue-finished quit"
        ) as e:
            s.after_queue_action = "quit"
            job = transfer("queue-quit")
            wait(lambda: process.poll() is not None, 20)
            assert process.returncode == 0
            e.update(job_id=job.id, sha256=digest(job.dest_path))
        with case(
            "GUEST-UNINSTALL-PRESERVES-DOWNLOADS", "real Inno uninstall + registry/file assertions"
        ):
            saved = {p: digest(p) for p in s.download_dir.glob("*.bin")}
            uninstaller = exe.with_name("unins000.exe")
            result = subprocess.run(
                [
                    str(uninstaller),
                    "/VERYSILENT",
                    "/SUPPRESSMSGBOXES",
                    "/NORESTART",
                    "/LOG=" + str(ROOT / "uninstall.log"),
                ],
                timeout=90,
            )
            assert result.returncode == 0, result.returncode
            assert not exe.exists() and not launcher.autostart_enabled()
            for path, expected in saved.items():
                assert digest(path) == expected
            for vendor in (
                r"Google\Chrome",
                "Chromium",
                r"Microsoft\Edge",
                r"BraveSoftware\Brave-Browser",
                "Vivaldi",
                "Mozilla",
            ):
                try:
                    winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER,
                        "Software\\" + vendor + r"\NativeMessagingHosts\dev.grabline.host",
                    )
                except FileNotFoundError:
                    pass
                else:
                    raise AssertionError("Dead native-host registration remains: " + vendor)
    elif phase in ("lock", "shutdown"):
        s.host_registered = True
        s.after_queue_action = phase
        start_packaged()
        job = transfer("queue-" + phase)
        save(
            "guest-power-" + phase + "-completion.json",
            {
                "job_id": job.id,
                "sha256": digest(job.dest_path),
                "time": time.time(),
                "identity_guard_passed": True,
            },
        )
        if phase == "lock":

            def desktop_inaccessible():
                handle = ctypes.windll.user32.OpenInputDesktop(0, False, 0x0001)
                if handle:
                    ctypes.windll.user32.CloseDesktop(handle)
                    return False
                return True

            with case(
                "GUEST-QUEUE-LOCK", "real packaged queue action + guest input-desktop access"
            ):
                wait(desktop_inaccessible, 15)
        else:
            record(
                "GUEST-QUEUE-SHUTDOWN-REQUEST",
                "PASS",
                "real guest completion before host observes VM exit",
                (
                    "Transfer completed; host must independently observe guest shutdown. This "
                    "alone is not full shutdown PASS."
                ),
            )
            deadline = time.monotonic() + 80
            while time.monotonic() < deadline:
                result = subprocess.run(
                    [
                        "powershell.exe",
                        "-NoProfile",
                        "-Command",
                        (
                            "Get-WinEvent -FilterHashtable @{LogName='System';Id=1074} "
                            "-MaxEvents 1 "
                            "| Select-Object TimeCreated,Message | ConvertTo-Json"
                        ),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0 and result.stdout.strip():
                    (ROOT / "guest-shutdown-event.json").write_text(result.stdout, encoding="utf-8")
                time.sleep(1)
    record(
        "GUEST-LOGIN-REBOOT",
        "BLOCKED",
        "persistent disposable Windows VM",
        (
            "Sandbox discards guest state across shutdown; login/reboot persistence "
            "needs a persistent VM."
        ),
    )
    for name in ("SLEEP", "HIBERNATE"):
        record(
            "GUEST-QUEUE-" + name,
            "NOT APPLICABLE",
            "actual guest powercfg /a",
            "Sandbox firmware reports this state unsupported; see power-states.txt.",
        )
finally:
    if process is not None and process.poll() is None:
        process.terminate()
        process.wait(timeout=15)
    window.shutdown()
    manager.shutdown()
    threads.shutdown()
    save(
        "guest-job-summary-" + phase + ".json",
        [
            {
                "id": j.id,
                "status": j.status.value,
                "bytes": j.downloaded,
                "size": j.total_size,
                "error": j.error,
            }
            for j in db.list_jobs()
        ],
    )
    db.close()
    helper_db.close()
    server.stop()
    save(
        "guest-finished-" + phase + ".json", {"finished": True, "phase": phase, "time": time.time()}
    )
