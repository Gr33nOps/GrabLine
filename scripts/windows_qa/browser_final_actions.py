"""Remaining popup quick action and genuine all-links collection transfer."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import time
from pathlib import Path

import httpx
from browser_api import database, manifest, python
from command import command
from common import case, digest, require_gui_result

from app.core.settings import Settings
from app.db.database import Database


def gui(op, **options):
    args = [python, "scripts/windows_qa/ui_driver.py", op]
    for k, v in options.items():
        args += ["--" + k, str(v)]
    p = subprocess.run(args, capture_output=True, text=True, timeout=25)
    require_gui_result(p)
    return json.loads(p.stdout)


def jobs(fragment):
    with sqlite3.connect(database) as d:
        d.row_factory = sqlite3.Row
        return [
            dict(r)
            for r in d.execute("select * from jobs where url like ?", ("%" + fragment + "%",))
        ]


def done(fragment, timeout=80):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        rows = jobs(fragment)
        if rows and all(r["status"] in ["completed", "failed", "cancelled"] for r in rows):
            assert all(r["status"] == "completed" for r in rows), rows
            return rows
        time.sleep(0.1)
    raise TimeoutError(jobs(fragment))


d = Database(database)
s = Settings(d)
old = s.confirm_video_quality
s.confirm_video_quality = False
d.close()
try:
    with case(
        "BROWSER-POPUP-GRAB-TAB-DEDUP", "actual Chrome toolbar popup + real host/app + HTTP output"
    ) as e:
        tag = "grabtab-" + str(time.time_ns())
        command("goto", url=manifest["page"] + "?case=" + tag)
        command("front")
        gui("popup")
        command("toolbar", js="document.querySelector('#grab-tab').click();true")
        row = done(tag)[0]
        target = Path(row["dest_dir"]) / row["filename"]
        response = httpx.get(manifest["page"] + "?case=" + tag)
        response.raise_for_status()
        assert digest(target) == hashlib.sha256(response.content).hexdigest()
        assert b"GrabLine complete QA" in target.read_bytes()
        gui("key", text="{ESC}")
        gui("popup")
        command("toolbar", js="document.querySelector('#grab-tab').click();true")
        end = time.monotonic() + 3
        while time.monotonic() < end:
            assert len(jobs(tag)) == 1
            time.sleep(0.1)
        gui("key", text="{ESC}")
        e.update(
            job_id=row["id"],
            sha256=digest(target),
            content_type=response.headers["content-type"],
            repeat_deduplicated=True,
        )
    with case(
        "BROWSER-CONTEXT-LINKS-DOWNLOAD-ALL",
        "genuine Chrome context menu + actual native picker + HTTP hashes",
    ) as e:
        tag = "alllinks-" + str(time.time_ns())
        command("goto", url=manifest["page"])
        command(
            "evaluate",
            js="(()=>{document.querySelectorAll('a').forEach(a=>a.remove());"
            "const set=['file.bin','blue.png','green.png','blue.png'];"
            "for(const n of set){const a=document.createElement('a');a.href='/'+n+'?case="
            + tag
            + "';a.textContent=n;document.body.append(a)}})()",
        )
        command("front")
        command("hover", selector="h1")
        gui("context", label=manifest["title"], control="Text")
        gui("click", label="GrabLine Connect", control="MenuItem")
        gui("click", label="Download all links with GrabLine", control="MenuItem")
        end = time.monotonic() + 8
        while time.monotonic() < end:
            rows = gui("inspect", target="app")
            if any("3 links" in r["text"] for r in rows):
                break
            time.sleep(0.1)
        assert any("3 links" in r["text"] for r in rows), rows
        gui("click", target="app", label="All visible")
        gui("shot", target="app", name="alllinks-native")
        gui("click", target="app", label="Download 3")
        rows = done(tag)
        assert len(rows) == 3, rows
        evidence = []
        for row in rows:
            name = row["url"].split("/")[-1].split("?")[0]
            actual = digest(Path(row["dest_dir"]) / row["filename"])
            assert actual == manifest["resources"]["/" + name]["sha256"]
            evidence.append({"job_id": row["id"], "name": name, "sha256": actual})
        e.update(outputs=evidence, duplicate_collapsed=True, screenshot="alllinks-native.png")
finally:
    d = Database(database)
    Settings(d).confirm_video_quality = old
    d.close()
    command("goto", url=manifest["page"])
