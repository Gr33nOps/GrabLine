"""Real overlay scrolling, control overlap and document navigation."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from command import command
from common import ROOT, case, digest

manifest = json.loads((ROOT / "browser-manifest.json").read_text())


def visible():
    return [b for b in command("overlay_state") if b["display"] != "none" and b["width"] > 0]


def wait(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise TimeoutError("Overlay condition did not arrive")


command("goto", url=manifest["page"])
command("front")
command("viewport", size={"width": 1200, "height": 800})
with case("BROWSER-OVERLAY-SCROLL", "real Chrome DOM/mouse + actual extension") as e:
    command("hover", selector="#video")
    before = wait(visible)[0]
    command("evaluate", js="window.scrollBy(0,20)")
    after = wait(visible)[0]
    assert abs(after["y"] - before["y"] + 20) < 4, (before, after)
    command(
        "evaluate",
        js="(()=>{const space=document.createElement('div');space.style.height='2000px';"
        "document.body.append(space);window.scrollTo(0,document.body.scrollHeight)})()",
    )
    assert command(
        "evaluate", js="document.querySelector('#video').getBoundingClientRect().bottom<0"
    )
    wait(lambda: not visible())
    e.update(before=before, after=after, offscreen_button_hidden=True)
with case(
    "BROWSER-OVERLAY-OVERLAPPING-CONTROL", "real Chrome mouse + extension + native host + checksum"
) as e:
    command("goto", url=manifest["page"])
    tag = "overlap-" + str(time.time_ns())
    command(
        "evaluate",
        js="(()=>{const v=document.querySelector('#video');v.src='/video.mp4?case="
        + tag
        + "';const wrap=document.createElement('div');wrap.style='position:relative;width:320px';"
        "v.before(wrap);wrap.append(v);const control=document.createElement('button');"
        "control.id='qa-overlap-control';control.textContent='Player control';"
        "control.style='position:absolute;left:90px;top:60px;width:140px;height:45px;z-index:5';"
        "wrap.append(control)})()",
    )
    command("hover", selector="#qa-overlap-control")
    buttons = wait(visible)
    assert len(buttons) == 1, buttons
    b = buttons[0]
    command("mouse_click", x=b["x"] + b["width"] / 2, y=b["y"] + b["height"] / 2)
    row = None
    end = time.monotonic() + 45
    while time.monotonic() < end:
        with sqlite3.connect(ROOT / "browser-app/Grabline/grabline.db") as db:
            row = db.execute(
                "select id,status,dest_dir,filename,url from jobs "
                "where url like ? order by id desc limit 1",
                ("%" + tag,),
            ).fetchone()
        if row and row[1] in ["completed", "failed"]:
            break
        time.sleep(0.1)
    assert row and row[1] == "completed", row
    actual = digest(Path(row[2]) / row[3])
    assert actual == manifest["resources"]["/video.mp4"]["sha256"]
    e.update(
        job_id=row[0],
        url=row[4],
        sha256=actual,
        hover_target="covering player control; real underlying media chosen",
    )
with case("BROWSER-OVERLAY-NAVIGATION", "real Chrome document navigation + actual extension") as e:
    old = manifest["page"]
    new = manifest["base"] + "/frame.html"
    command("goto", url=new)
    assert not visible()
    command("front")
    command("hover", selector="video")
    buttons = wait(visible)
    assert all(b["document"] == new for b in buttons), buttons
    command("goto", url=old)
    assert not visible()
    e.update(new_document=new, buttons=buttons, old_buttons_did_not_leak=True)
command("goto", url=manifest["page"])
