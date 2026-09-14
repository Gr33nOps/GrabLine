"""The Phase 0 milestone: an 8-connection download is killed with SIGKILL
mid-transfer, the process is relaunched, the download resumes from its
checkpoints, and the final file's checksum matches the source exactly.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from app.db.database import Database
from app.tests.conftest import sha256_file, wait_for
from app.tests.media_server import MediaServer, payload, sha256

MB = 1024 * 1024
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL semantics are POSIX")
def test_kill9_resume_checksum_matches(server: MediaServer, tmp_path: Path):
    data = payload(8 * MB, 42)
    # Throttled so the download takes a couple of seconds: long enough to be
    # killed mid-flight, short enough to keep the suite fast.
    url = server.add("/movie.bin", data, chunk_size=32 * 1024, delay_per_chunk=0.05)
    dest = tmp_path / "out"
    dest.mkdir()
    db_path = tmp_path / "cli.db"
    command = [
        sys.executable,
        "-m",
        "app.cli",
        url,
        str(dest),
        "--db",
        str(db_path),
        "--connections",
        "8",
    ]
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}

    # --- run 1: start downloading, then kill -9 mid-transfer ---------------
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    watcher = Database(db_path)
    try:
        wait_for(
            lambda: sum(watcher.job_downloaded(j.id) for j in watcher.list_jobs()) > 1 * MB,
            timeout=60,
        )
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=30)
        persisted = sum(watcher.job_downloaded(j.id) for j in watcher.list_jobs())
    finally:
        if process.poll() is None:
            process.kill()
        if process.stdout is not None:
            process.stdout.close()
        watcher.close()

    assert not (dest / "movie.bin").exists(), "file must not exist before completion"
    assert (dest / "movie.bin.gl-part").exists(), "partial file should survive the crash"
    assert 0 < persisted < len(data), "checkpoints should hold partial progress"

    # --- run 2: relaunch, resume from checkpoints, finish ------------------
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    resumed = re.search(r"RESUMING job \d+ \((\d+) bytes already downloaded\)", completed.stdout)
    assert resumed is not None, f"expected a RESUMING line, got:\n{completed.stdout}"
    assert int(resumed.group(1)) > 0

    # --- the milestone assertion: checksum matches -------------------------
    assert sha256_file(dest / "movie.bin") == sha256(data)
    assert not (dest / "movie.bin.gl-part").exists()
