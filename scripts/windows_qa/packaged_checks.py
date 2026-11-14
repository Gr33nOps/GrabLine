"""Sequential Windows build and isolated packaged GUI/native-host smoke."""

from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

import psutil
from common import ROOT, case, digest, save

from app.core.models import JobStatus
from app.core.settings import Settings
from app.db.database import Database

source = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser()
parser.add_argument("--skip-build", action="store_true")
args = parser.parse_args()
environment = dict(os.environ)
environment["PATH"] = os.pathsep.join(
    [os.environ["SYSTEMROOT"] + r"\System32", os.environ["SYSTEMROOT"]]
)
if not args.skip_build:
    with case("PACKAGE-BUILD", "Windows packaging") as evidence:
        command = [
            sys.executable,
            "-m",
            "PyInstaller",
            "packaging/grabline.spec",
            "--noconfirm",
            "--clean",
            "--workpath",
            str(ROOT / "final-build"),
            "--distpath",
            str(ROOT / "final-dist"),
        ]
        with (ROOT / "final-build.log").open("w", encoding="utf-8") as log:
            result = subprocess.run(
                command,
                cwd=source,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=300,
            )
        assert result.returncode == 0
        executable = ROOT / "final-dist/grabline/grabline.exe"
        host_executable = executable.with_name("grabline-host.exe")
        assert executable.is_file() and host_executable.is_file()
        evidence.update(exe_sha256=digest(executable), host_sha256=digest(host_executable))

else:
    verified = json.loads((ROOT / "packaged-files.json").read_text())
    for relative, expected in verified.items():
        assert digest(ROOT / "final-dist" / relative) == expected

with case("PACKAGE-NATIVE-TRANSFER", "packaged Qt GUI + real native host + HTTP") as evidence:
    executable = ROOT / "final-dist/grabline/grabline.exe"
    host_executable = executable.with_name("grabline-host.exe")
    manifest = json.loads((ROOT / "browser-manifest.json").read_text(encoding="utf-8"))
    run_tag = str(time.time_ns())
    profile = ROOT / "packaged-profile" / run_tag
    db = Database(profile / "Grabline/grabline.db")
    settings = Settings(db)
    settings.setup_seen = settings.host_registered = True
    settings.check_updates = settings.confirm_downloads = settings.confirm_video_quality = False
    settings.download_dir = profile / "downloads"
    db.close()
    environment = {
        **os.environ,
        "LOCALAPPDATA": str(profile),
        "QT_QPA_PLATFORM": "offscreen",
        "QT_OPENGL": "software",
    }
    for key in ("PYTHONPATH", "GRABLINE_QA_SOURCE"):
        environment.pop(key, None)
    process = subprocess.Popen(
        [str(executable), "--minimized"], env=environment, creationflags=subprocess.CREATE_NO_WINDOW
    )

    def host(message):
        payload = json.dumps(message).encode()
        response = subprocess.run(
            [str(host_executable)],
            input=struct.pack("<I", len(payload)) + payload,
            capture_output=True,
            env=environment,
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        assert response.returncode == 0 and len(response.stdout) >= 4, response.stderr
        length = struct.unpack("<I", response.stdout[:4])[0]
        return json.loads(response.stdout[4 : 4 + length])

    try:
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            assert process.poll() is None
            ping = host({"type": "ping"})
            if ping.get("appRunning"):
                break
            time.sleep(0.2)
        assert ping.get("appRunning"), ping
        url = manifest["base"] + "/file.bin?case=packaged-final-" + run_tag
        reply = host({"type": "download", "url": url, "onlyIfRunning": True})
        assert reply["type"] == "queued", reply
        assert reply["previousJobId"] == 0, "Fresh-profile test matched an old job"
        db = Database(profile / "Grabline/grabline.db")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            job = db.latest_job_for_url(url)
            if job and job.status in {JobStatus.COMPLETED, JobStatus.FAILED}:
                break
            time.sleep(0.1)
        assert job and job.status is JobStatus.COMPLETED, job
        assert digest(job.dest_path) == manifest["resources"]["/file.bin"]["sha256"]
        evidence.update(
            job_id=job.id,
            sha256=digest(job.dest_path),
            ping=ping,
            handoff=reply,
            exe_sha256=digest(executable),
            offscreen=True,
            registration_preserved=True,
            fresh_profile=True,
        )
        db.close()
    finally:
        if process.poll() is None:
            children = psutil.Process(process.pid).children(recursive=True)
            process.terminate()
            process.wait(timeout=15)
            for child in children:
                if child.is_running():
                    child.terminate()
        save(
            "packaged-files.json",
            {
                str(p.relative_to(ROOT / "final-dist")): digest(p)
                for p in (executable, host_executable)
            },
        )
