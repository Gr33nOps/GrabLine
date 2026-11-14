"""Real toolbar, overlay, pause/resume and closed-app recovery checks."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

from browser_api import count_jobs, database, manifest, overlays, python
from command import command
from common import ROOT, case, digest, require_gui_result
from session_control import stop_app

from app.tests.media_server import MediaServer, payload


def gui(op, **options):
    args = [python, "scripts/windows_qa/ui_driver.py", op]
    for key, value in options.items():
        args += ["--" + key, str(value)]
    p = subprocess.run(args, capture_output=True, text=True, timeout=25)
    require_gui_result(p)
    return json.loads(p.stdout)


def job_for(tag, statuses=None, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with sqlite3.connect(database) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "select * from jobs where url like ? order by id desc limit 1", ("%" + tag,)
            ).fetchone()
        if row and (statuses is None or row["status"] in statuses):
            return dict(row)
        time.sleep(0.1)
    raise TimeoutError((tag, statuses, dict(row) if row else None))


def pill(expected):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        texts = command("shadow_text")
        if any(expected in text for text in texts):
            return [text for text in texts if expected in text]
        time.sleep(0.1)
    raise AssertionError((expected, texts))


with case("BROWSER-OPEN-SETTINGS", "actual Chrome toolbar popup + native GUI") as evidence:
    gui("popup", name="open-settings-toolbar")
    command(
        "toolbar", js="setTimeout(()=>document.querySelector('#open-settings').click(),100);true"
    )
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        rows = gui("inspect", target="app", name="open-settings-app")
        if any(r["text"] == "General" for r in rows):
            break
        time.sleep(0.1)
    assert any(r["text"] == "General" for r in rows), rows
    evidence["screenshot"] = "open-settings-app.png"
    gui("shot", target="app", name="open-settings-app")
    gui("key", target="app", text="^1")

with case(
    "BROWSER-OVERLAY-CLICK", "real hover + real browser click + native host + transfer"
) as evidence:
    command("front")
    command("evaluate", js="document.querySelector('#video').src='/video.mp4?case=overlay-final'")
    buttons = [b for b in overlays("#video") if b["document"] == manifest["page"]]
    assert len(buttons) == 1, buttons
    b = buttons[0]
    command("mouse_click", x=b["x"] + b["width"] / 2, y=b["y"] + b["height"] / 2)
    job = job_for("overlay-final", {"completed", "failed"}, 45)
    assert job["status"] == "completed", job
    path = Path(job["dest_dir"]) / job["filename"]
    assert digest(path) == manifest["resources"]["/video.mp4"]["sha256"]
    evidence.update(job_id=job["id"], sha256=digest(path), overlay=b)

with case(
    "BROWSER-PAUSE-RESUME-PROGRESS", "real Chrome takeover + native app controls + real transfer"
) as evidence:
    gui("popup")
    command(
        "toolbar",
        js=(
            "(()=>{const c=document.querySelector('#toggle-pill');"
            "if(!c.checked)c.click();return c.checked})()"
        ),
    )
    gui("key", text="{ESC}")
    gui("popup", label="Open GrabLine", control="Button")
    gui("click", target="app", label="Downloads", control="CheckBox")
    slow_server = MediaServer()
    slow_server.start()
    test_name = "qa-pause-" + str(time.time_ns()) + ".bin"
    slow_url = (
        slow_server.add(
            "/slow.bin",
            payload(16 * 1024**2, 81),
            delay_per_chunk=1,
            content_disposition='attachment; filename="' + test_name + '"',
        )
        + "?case=pause-final"
    )
    command("evaluate", js="document.querySelector('#slow').href=" + json.dumps(slow_url))
    command("click", selector="#slow")
    job = job_for("pause-final", {"downloading"})
    tracking_deadline = time.monotonic() + 3
    while time.monotonic() < tracking_deadline:
        evidence["tracking_before_pause"] = command("worker", js="[...tracked.entries()]")
        if evidence["tracking_before_pause"]:
            break
        time.sleep(0.1)
    gui("lastrow", target="app")
    gui("click", target="app", label=job["filename"], control="DataItem")
    gui("click", target="app", label="Pause")
    paused = job_for("pause-final", {"paused"})
    evidence["tracking_after_pause"] = command("worker", js="[...tracked.entries()]")
    evidence["native_paused_status"] = command(
        "worker",
        js="async()=>await chrome.runtime.sendNativeMessage('dev.grabline.host',"
        + "{type:'status',urls:["
        + json.dumps(slow_url)
        + "]})",
    )
    observed = pill("Paused")
    gui("click", target="app", label="Resume")
    job = job_for("pause-final", {"completed", "failed"}, 60)
    assert job["status"] == "completed", job
    path = Path(job["dest_dir"]) / job["filename"]
    assert digest(path) == manifest["resources"]["/slow.bin"]["sha256"]
    evidence.update(
        job_id=job["id"],
        paused_status=paused["status"],
        observed_paused=observed,
        completed=pill("Done"),
        sha256=digest(path),
    )
    slow_server.stop()

with case(
    "BROWSER-CLOSED-APP-FALLBACK", "real Chrome downloads API + unavailable desktop app"
) as evidence:
    before = count_jobs()
    stopped = stop_app()
    assert stopped, "No owned app processes found"
    try:
        command("front")
        command(
            "evaluate", js="document.querySelector('#file').href='/file.bin?case=app-closed-final'"
        )
        result = command("download", selector="#file", name="app-closed-final.bin")
        assert result["sha256"] == manifest["resources"]["/file.bin"]["sha256"]
        assert count_jobs() == before, "Closed app fallback created an unwanted duplicate job"
        evidence.update(result, stopped_test_pids=stopped)
    finally:
        source = Path(__file__).resolve().parents[2]
        env = {
            **os.environ,
            "LOCALAPPDATA": str(ROOT / "browser-app"),
            "PYTHONPATH": str(ROOT / "bootstrap"),
            "GRABLINE_QA_SOURCE": str(source),
            "GRABLINE_QA_ROOT": str(ROOT),
        }
        env.pop("QT_QPA_PLATFORM", None)
        env.pop("QT_OPENGL", None)
        process = subprocess.Popen(
            [python, "-m", "app", "--minimized"],
            cwd=source,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=(ROOT / "app-restarted-stdout.log").open("w"),
            stderr=(ROOT / "app-restarted-stderr.log").open("w"),
        )
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            reply = command(
                "worker",
                js=(
                    "async()=>await chrome.runtime.sendNativeMessage("
                    "'dev.grabline.host',{type:'ping'})"
                ),
            )
            if reply.get("appRunning"):
                break
            time.sleep(0.2)
        assert reply.get("appRunning"), reply
        evidence["restart_ping"] = reply
