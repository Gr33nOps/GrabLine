"""Real toolbar preferences, overlay geometry and browser-only downloads."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time

from command import command
from common import ROOT, require_gui_result

manifest = json.loads((ROOT / "browser-manifest.json").read_text())
python = os.environ.get("GRABLINE_QA_PYTHON", sys.executable)
database = ROOT / "browser-app/Grabline/grabline.db"


def ui(label: str, control: str = "CheckBox", text: str | None = None):
    args = [
        python,
        "scripts/windows_qa/ui_driver.py",
        "popup",
        "--label",
        label,
        "--control",
        control,
        "--name",
        "ui-" + str(time.time_ns()),
    ]
    if text is not None:
        args += ["--text", text]
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    require_gui_result(result)


def stored():
    return command("worker", js="async()=>await chrome.storage.local.get(null)")


def set_pref(label: str, key: str, value: bool, default: bool):
    if stored().get(key, default) != value:
        ui(label)
    deadline = time.monotonic() + 4
    while stored().get(key, default) != value and time.monotonic() < deadline:
        time.sleep(0.1)
    assert stored().get(key, default) == value, stored()


def overlays(selector: str):
    command("front")
    command("hover", selector="h1")
    hide_deadline = time.monotonic() + 2
    while time.monotonic() < hide_deadline:
        visible = [r for r in command("overlay_state") if r["display"] != "none" and r["width"] > 0]
        if not visible:
            break
        time.sleep(0.1)
    assert not visible, "Previous overlay did not hide after leaving media"
    command("hover", selector=selector)
    deadline = time.monotonic() + 2
    result = []
    while time.monotonic() < deadline:
        result = [r for r in command("overlay_state") if r["display"] != "none" and r["width"] > 0]
        if result:
            return result
        time.sleep(0.1)
    return result


def count_jobs():
    with sqlite3.connect(database) as db:
        return db.execute("select count(*) from jobs").fetchone()[0]
