"""A real app download and a uniquely identifiable physical Windows toast."""

import argparse
import ctypes
import json
import os
import site
import struct
import subprocess
import sys
import time

import psutil
from common import ROOT, Blocked, case, digest

site.addsitedir(r"D:\Projects\app dev\GrabLine\.dev-pc\qa\followup\gui-tools")
from pywinauto import Desktop

from app.core.models import JobStatus
from app.core.settings import Settings
from app.db.database import Database
from app.tests.media_server import MediaServer, payload, sha256

parser = argparse.ArgumentParser()
parser.add_argument("--foreground", action="store_true")
args = parser.parse_args()
case_name = "WINDOWS-NATIVE-NOTIFICATION"
if args.foreground:
    case_name += "-FOREGROUND"
profile = ROOT / ("notification-foreground" if args.foreground else "notification-profile")
db = Database(profile / "Grabline/grabline.db")
s = Settings(db)
s.setup_seen = s.host_registered = True
s.check_updates = s.confirm_downloads = s.confirm_video_quality = False
s.notify_on_complete = True
s.sound_on_complete = s.quiet_enabled = False
s.toast_seconds = 10
s.language = "en"
s.download_dir = ROOT / "notification-downloads"
s.log_to_file = True
s.connections = 1
s.ffmpeg_path = r"C:\Users\GREEN\AppData\Local\Grabline\bin\ffmpeg.exe"
env = {**os.environ, "LOCALAPPDATA": str(profile)}
env.pop("QT_QPA_PLATFORM", None)
server = MediaServer()
server.start()
process = subprocess.Popen(
    [sys.executable, "-m", "app", "--minimized"],
    env=env,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    with case(
        case_name,
        "real app/native host/HTTP transfer + Windows toast UIA observation",
    ) as e:

        def native(message):
            body = json.dumps(message).encode()
            result = subprocess.run(
                [sys.executable, "-m", "app.native_host"],
                input=struct.pack("<I", len(body)) + body,
                capture_output=True,
                env=env,
                timeout=15,
            )
            assert result.returncode == 0 and len(result.stdout) >= 4
            size = struct.unpack("<I", result.stdout[:4])[0]
            return json.loads(result.stdout[4 : 4 + size])

        deadline = time.monotonic() + 25
        while not native({"type": "ping"}).get("appRunning"):
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.1)
        if args.foreground:
            native({"type": "focus"})
            windows = []
            ready_deadline = time.monotonic() + 12
            while time.monotonic() < ready_deadline:
                pids = []
                for candidate in psutil.process_iter(["pid", "cmdline"]):
                    try:
                        if "app" in (candidate.info["cmdline"] or []) and candidate.environ().get(
                            "LOCALAPPDATA"
                        ) == str(profile):
                            pids.append(candidate.pid)
                    except psutil.Error:
                        pass
                windows = []
                for owned_pid in pids:
                    windows.extend(
                        Desktop(backend="win32").windows(
                            process=owned_pid, title="GrabLine", visible_only=False
                        )
                    )
                if len(windows) == 1:
                    break
                time.sleep(0.1)
            if len(windows) != 1:
                raise Blocked("Cannot identify one owned QA app window after native focus request")
            try:
                windows[0].restore()
                windows[0].set_focus()
            except Exception as exc:
                raise Blocked("Owned QA window could not be activated") from exc
            state = ctypes.c_int()
            ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
            e["foreground_shell_notification_state"] = state.value
        filename = "grabline-qa-notification-" + str(time.time_ns()) + ".bin"
        data = payload(1024 * 1024, 47)
        url = server.add("/" + filename, data, delay_per_chunk=0.2)
        reply = native({"type": "download", "url": url, "onlyIfRunning": True})
        assert reply["type"] == "queued", reply
        deadline = time.monotonic() + 30
        observed = False
        toast_windows = 0
        while time.monotonic() < deadline:
            job = db.latest_job_for_url(url)
            if job and job.status is JobStatus.FAILED:
                raise AssertionError(job.error)
            for window in Desktop(backend="uia").windows(title="New notification"):
                toast_windows += 1
                # Never retain text from unrelated notifications.
                observed |= any(
                    filename in c.window_text() for c in window.descendants(control_type="Text")
                )
            if observed and job and job.status is JobStatus.COMPLETED:
                break
            time.sleep(0.1)
        assert job and job.status is JobStatus.COMPLETED
        assert digest(job.dest_path) == sha256(data)
        e.update(
            job_id=job.id,
            sha256=digest(job.dest_path),
            toast_windows=toast_windows,
            uniquely_matching_toast=observed,
        )
        if not observed:
            raise Blocked(
                "Download completed, but the available Windows UIA observer did not "
                "expose a uniquely matching toast. Physical delivery remains "
                "unverified."
            )
finally:
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=15)
    db.close()
    server.stop()
