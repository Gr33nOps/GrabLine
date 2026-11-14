"""Genuine Chrome context-menu handoffs to the native Qt collection pickers."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

from browser_api import database, manifest, python
from command import command
from common import case, digest, require_gui_result


def gui(op, **options):
    args = [python, "scripts/windows_qa/ui_driver.py", op]
    for key, value in options.items():
        args += ["--" + key, str(value)]
    result = subprocess.run(args, capture_output=True, text=True, timeout=25)
    require_gui_result(result)
    return json.loads(result.stdout)


def menu(label, selection=False):
    command("front")
    if selection:
        command(
            "evaluate",
            js="""(()=>{const range=document.createRange();
            range.selectNodeContents(document.querySelector('#selection'));
            const s=getSelection();s.removeAllRanges();s.addRange(range);return s.toString()})()""",
        )
        gui("key", text="+{F10}")
    else:
        command("hover", selector="h1")
        gui("context", label=manifest["title"], control="Text")
    gui("click", label="GrabLine Connect", control="MenuItem")
    # Chromium exposes '&' as a menu mnemonic rather than literal UIA text.
    gui("click", label=label.replace("&", ""), control="MenuItem")
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        rows = gui("inspect", target="app")
        if any(r["text"].startswith(manifest["title"] + ": ") for r in rows):
            return rows
        time.sleep(0.1)
    raise TimeoutError("Native collection picker absent")


def completed(suffix):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        with sqlite3.connect(database) as db:
            db.row_factory = sqlite3.Row
            rows = list(db.execute("select * from jobs where url like ?", ("%" + suffix,)))
        if rows and all(r["status"] in {"completed", "failed", "cancelled"} for r in rows):
            assert all(r["status"] == "completed" for r in rows), [dict(r) for r in rows]
            return [dict(r) for r in rows]
        time.sleep(0.2)
    raise TimeoutError(suffix)


gallery_tag = str(time.time_ns())
command(
    "evaluate",
    js="("
    + """async tag=>{
    for(const name of ['blue','green']){
        const image=document.querySelector('#'+name);
        image.src='/'+name+'.png?case=gallery-'+tag;
        if(image.parentElement.tagName==='A')image.parentElement.href=image.src;
    }
    [...document.links].filter(a=>a.textContent.includes('Duplicate blue')).forEach(
        a=>a.href=document.querySelector('#blue').src);
    await Promise.all(['blue','green'].map(n=>document.querySelector('#'+n).decode()));
}"""
    + ")("
    + json.dumps(gallery_tag)
    + ")",
)

with case("BROWSER-CONTEXT-GALLERY-CANCEL", "genuine Chrome context menu + native Qt GUI"):
    before = sqlite3.connect(database).execute("select count(*) from jobs").fetchone()[0]
    menu("Download all images with GrabLine")
    gui("click", target="app", label="Cancel")
    assert sqlite3.connect(database).execute("select count(*) from jobs").fetchone()[0] == before

with case(
    "BROWSER-CONTEXT-GALLERY", "genuine Chrome context menu + native Qt GUI + transfer"
) as evidence:
    menu("Download all images with GrabLine")
    none = gui("click", target="app", label="Select none", name="gallery-none")
    all_rows = gui("click", target="app", label="Select all", name="gallery-all")
    assert any(r["text"] == "0 selected" for r in none)
    assert any(r["text"] == "2 selected" for r in all_rows)
    gui("shot", target="app", name="gallery-native")
    gui("click", target="app", label="Download 2")
    outputs = []
    for name in ("blue", "green"):
        rows = completed("/" + name + ".png?case=gallery-" + gallery_tag)
        assert len(rows) == 1, rows
        row = rows[0]
        target = Path(row["dest_dir"]) / row["filename"]
        actual = digest(target)
        assert actual == manifest["resources"]["/" + name + ".png"]["sha256"]
        outputs.append({"job_id": row["id"], "sha256": actual})
    evidence.update(
        outputs=outputs,
        screenshot="gallery-native.png",
        filtered_small_image=True,
        duplicate_blue_collapsed=True,
    )

with case(
    "BROWSER-CONTEXT-LINKS-CANCEL", "genuine Chrome context menu + native Qt GUI"
) as evidence:
    before = sqlite3.connect(database).execute("select count(*) from jobs").fetchone()[0]
    rows = menu("Download all links with GrabLine")
    names = [r["text"] for r in rows if r["type"] == "ListItem"]
    assert names.count("blue.png") == 1 and "file.bin" in names, names
    gui("click", target="app", label="All visible")
    rows = gui("click", target="app", label="None")
    assert any(r["text"] == "0 selected" for r in rows)
    gui("click", target="app", label="Cancel")
    assert sqlite3.connect(database).execute("select count(*) from jobs").fetchone()[0] == before
    evidence["links"] = names

with case(
    "BROWSER-CONTEXT-SELECTED-TEXT",
    "genuine Chrome selection context menu + native Qt GUI + transfer",
) as evidence:
    tag = str(time.time_ns())
    text = manifest["base"] + "/file.bin?case=selection-a-" + tag + "\n"
    text += manifest["base"] + "/video.mp4?case=selection-b-" + tag
    command("evaluate", js="document.querySelector('#selection').textContent=" + json.dumps(text))
    rows = menu("Download selected links & media with GrabLine", selection=True)
    assert any("2 links" in r["text"] for r in rows), rows
    gui("click", target="app", label="All visible")
    gui("shot", target="app", name="selection-native")
    gui("click", target="app", label="Download 2")
    file_rows = completed("selection-a-" + tag)
    media_rows = completed("selection-b-" + tag)
    assert len(file_rows) == len(media_rows) == 1
    file = Path(file_rows[0]["dest_dir"]) / file_rows[0]["filename"]
    assert digest(file) == manifest["resources"]["/file.bin"]["sha256"]
    media = Path(media_rows[0]["dest_dir"]) / media_rows[0]["filename"]
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type",
            "-of",
            "json",
            str(media),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    assert probe.returncode == 0, probe.stderr
    properties = json.loads(probe.stdout)
    assert float(properties["format"]["duration"]) >= 3.8
    evidence.update(
        job_ids=[file_rows[0]["id"], media_rows[0]["id"]],
        file_sha256=digest(file),
        media_sha256=digest(media),
        media=properties,
        screenshot="selection-native.png",
    )
