"""Actual toolbar actions, streamed progress and completed browser handoffs."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

from browser_api import database, manifest, overlays, python, stored
from command import command
from common import case, digest, require_gui_result


def native(op, **options):
    args = [python, "scripts/windows_qa/ui_driver.py", op]
    for name, value in options.items():
        args += ["--" + name, str(value)]
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    require_gui_result(result)


def toolbar(js):
    native("popup", name="toolbar-" + str(time.time_ns()))
    result = command("toolbar", js=js)
    native("key", text="{ESC}")
    return result["result"]


def wait_job(fragment, timeout=90):
    deadline = time.monotonic() + timeout
    observations = []
    while time.monotonic() < deadline:
        with sqlite3.connect(database) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "select * from jobs where url like ? order by id desc limit 1",
                ("%" + fragment + "%",),
            ).fetchone()
        if row:
            data = dict(row)
            observations.append({key: data[key] for key in ("id", "status", "downloaded")})
            if data["status"] in {"completed", "failed", "cancelled"}:
                assert data["status"] == "completed", data
                target = Path(data["dest_dir"]) / data["filename"]
                assert target.exists() and target.stat().st_size > 0
                return data, observations, target
        time.sleep(0.2)
    raise TimeoutError(observations)


with case("BROWSER-PREF-CORNERS", "actual toolbar popup via CDP + real browser") as evidence:
    positions = []
    for corner in ["top-right", "top-left", "bottom-right", "bottom-left"]:
        value = toolbar(
            "(() => {const s=document.querySelector('#button-corner');s.value="
            + json.dumps(corner)
            + ";s.dispatchEvent(new Event('change',{bubbles:true}));return s.value})()"
        )
        assert value == corner
        assert stored()["buttonCorner"] == corner
        buttons = [b for b in overlays("#video") if b.get("document") == manifest["page"]]
        assert len(buttons) == 1, buttons
        positions.append(buttons[0])
    assert positions[0]["x"] > positions[1]["x"] and positions[2]["x"] > positions[3]["x"]
    assert positions[0]["y"] < positions[2]["y"] and positions[1]["y"] < positions[3]["y"]
    evidence["positions"] = positions

with case("BROWSER-ACTUAL-TOOLBAR-MEDIA-RECENT", "actual toolbar popup") as evidence:
    data = toolbar(
        "({body:document.body.innerText,media:[...document.querySelectorAl"
        "l('#media-list li')].map(r=>({kind:r.querySelector('.kind').textC"
        "ontent,url:r.querySelector('.name').title})),recent:document.quer"
        "ySelector('#recent-list').innerText,names:[...document.querySelectorAll("
        "'#recent-list .name')].map(e=>e.textContent)})"
    )
    assert "connected" in data["body"]
    assert any(m["url"].endswith(".mp4") for m in data["media"])
    assert any(m["url"].endswith(".m3u8") for m in data["media"])
    with sqlite3.connect(database) as db:
        expected_names = [
            row[0] or row[1]
            for row in db.execute("select title,filename from jobs order by id desc limit 5")
        ]
    assert data["names"] == expected_names, (data["names"], expected_names)
    assert "Done" in data["recent"]
    evidence.update(data)

for suffix in (".mp4", ".m3u8"):
    with case(
        "BROWSER-POPUP-DOWNLOAD-" + suffix[1:].upper(), "actual toolbar popup + real transfer"
    ) as evidence:
        selected = toolbar(
            (
                "(() => {const row=[...document.querySelectorAll('#media-list "
                "li')].find(r=>r.querySelector('.name').title.endsWith("
            )
            + json.dumps(suffix)
            + (
                "));const url=row.querySelector('.name').title;row.querySelector('"
                "button').click();return url})()"
            )
        )
        job, observations, target = wait_job(selected)
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type",
                "-of",
                "json",
                str(target),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        assert probe.returncode == 0, probe.stderr
        media = json.loads(probe.stdout)
        assert float(media["format"]["duration"]) >= 3.8
        assert any(s["codec_type"] == "video" for s in media["streams"])
        evidence.update(job_id=job["id"], sha256=digest(target), media=media)

with case("BROWSER-PROGRESS-REAL", "actual toolbar + real browser + native host") as evidence:
    toolbar(
        "(() => {const c=document.querySelector('#toggle-pill');if(!c.chec"
        "ked)c.click();return c.checked})()"
    )
    assert stored()["pagePill"]
    command("evaluate", js="document.querySelector('#slow').href='/slow.bin?case=progress-real'")
    command("click", selector="#slow")
    seen = []
    deadline = time.monotonic() + 90
    job_id = None
    done = False
    while time.monotonic() < deadline:
        texts = command("shadow_text")
        seen += [text for text in texts if " · " in text and ("%" in text or "Done" in text)]
        with sqlite3.connect(database) as db:
            row = db.execute(
                "select id,status from jobs where url like '%progress-real' order "
                "by id desc limit 1"
            ).fetchone()
        if row:
            job_id = row[0]
        if row and row[1] == "completed" and any("Done ·" in text for text in seen):
            done = True
            break
        time.sleep(0.2)
    assert done and any("%" in text for text in seen), seen
    job, observations, target = wait_job("progress-real")
    assert digest(target) == manifest["resources"]["/slow.bin"]["sha256"]
    evidence.update(job_id=job_id, observed_text=sorted(set(seen)), sha256=digest(target))
    recent = toolbar("document.querySelector('#recent-list').innerText")
    assert (job["title"] or job["filename"]) in recent and "Done" in recent

with case("BROWSER-WORKER-RESTART", "real browser + native host") as evidence:
    command("front")
    before = stored()
    stopped = command("worker_stop")
    native("popup", name="worker-restarted-popup")
    assert command("worker", js="async()=>await chrome.storage.local.get(null)") == before
    ping = command(
        "worker",
        js="async()=>await chrome.runtime.sendNativeMessage('dev.grabline.host',{type:'ping'})",
    )
    assert ping["appRunning"]
    evidence.update(stopped_worker=stopped, ping=ping)
    native("key", text="{ESC}")
