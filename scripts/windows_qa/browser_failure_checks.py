"""Real app cancellation/failure and Chromium's retained-download recovery."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

from browser_api import database, python
from command import command
from common import case, digest, require_gui_result

from app.tests.media_server import MediaServer, payload, sha256


def gui(op, **options):
    args = [python, "scripts/windows_qa/ui_driver.py", op]
    for key, value in options.items():
        args += ["--" + key, str(value)]
    r = subprocess.run(args, capture_output=True, text=True, timeout=25)
    require_gui_result(r)


def latest(url):
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "select * from jobs where url=? order by id desc limit 1", (url,)
        ).fetchone()
    return dict(row) if row else None


for mode in os.environ.get("GRABLINE_QA_RECOVERY_MODES", "cancel,failure").split(","):
    with case(
        "BROWSER-RECOVERY-" + mode.upper(),
        "real Chrome downloads + native app + real HTTP failure/recovery",
    ) as evidence:
        server = MediaServer()
        server.start()
        data = payload(16 * 1024**2, 178)
        name = "qa-recovery-" + mode + "-" + str(time.time_ns()) + ".bin"
        url = server.add(
            "/file.bin",
            data,
            delay_per_chunk=0.3,
            content_disposition='attachment; filename="' + name + '"',
            fail_status=403 if mode == "failure" else None,
            fail_from=3,
            fail_until=100000 if mode == "failure" else 0,
        )
        try:
            gui("popup", label="Open GrabLine", control="Button")
            gui("click", target="app", label="Downloads", control="CheckBox")
            command("front")
            command("evaluate", js="document.querySelector('#slow').href=" + json.dumps(url))
            command("click", selector="#slow")
            deadline = time.monotonic() + 20
            job = None
            while time.monotonic() < deadline:
                job = latest(url)
                if job and job["status"] in (
                    {"downloading", "failed"} if mode == "failure" else {"downloading"}
                ):
                    break
                time.sleep(0.05)
            assert job and job["status"] in {"downloading", "failed"}, job
            if mode == "cancel":
                gui("lastrow", target="app")
                gui("click", target="app", label=job["filename"], control="DataItem")
                gui("click", target="app", label="Cancel")
            deadline = time.monotonic() + 25
            expected = "cancelled" if mode == "cancel" else "failed"
            while time.monotonic() < deadline:
                job = latest(url)
                if job["status"] == expected:
                    break
                time.sleep(0.05)
            assert job["status"] == expected, job
            evidence.update(
                app_job_id=job["id"], app_terminal_status=job["status"], app_error=job["error"]
            )
            # Restore the real fixture before Chromium resumes its retained item.
            resource = server.resources["/file.bin"]
            resource.fail_until = resource.cut_until = 0
            resource.delay_per_chunk = 0.01
            deadline = time.monotonic() + 100
            browser = None
            observations = []
            while time.monotonic() < deadline:
                items = command(
                    "worker",
                    js="async()=>await chrome.downloads.search({url:" + json.dumps(url) + "})",
                )
                if items:
                    assert len(items) == 1, items
                    browser = items[0]
                    observations.append(
                        {
                            k: browser.get(k)
                            for k in ("id", "paused", "state", "bytesReceived", "error")
                        }
                    )
                    if browser["state"] == "complete":
                        break
                time.sleep(0.2)
            assert browser and browser["state"] == "complete", browser
            assert digest(Path(browser["filename"])) == sha256(data)
            with sqlite3.connect(database) as db:
                assert (
                    db.execute("select count(*) from jobs where url=?", (url,)).fetchone()[0] == 1
                )
            evidence.update(
                browser_id=browser["id"],
                browser_states=observations,
                sha256=sha256(data),
                browser_bytes=browser["bytesReceived"],
            )
        finally:
            server.stop()
