"""Real Chromium native-host absence, using only a disposable fault configuration."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

from common import ROOT, case
from playwright.sync_api import sync_playwright

source = Path(__file__).resolve().parents[2]
manifest = json.loads((ROOT / "browser-manifest.json").read_text())
out = ROOT / "missing-host"
out.mkdir(exist_ok=True)
extension = out / "extension"
shutil.copytree(source / "extension", extension, dirs_exist_ok=True)
background = extension / "background.js"
text = background.read_text(encoding="utf-8")
assert '"dev.grabline.host"' in text
background.write_text(
    text.replace('"dev.grabline.host"', '"dev.grabline.qa_missing_host"'), encoding="utf-8"
)
dbpath = ROOT / "browser-app/Grabline/grabline.db"
with case(
    "BROWSER-HOST-UNAVAILABLE", "real Chrome nativeMessaging failure + original browser download"
) as evidence:
    with sqlite3.connect(dbpath) as db:
        before = db.execute("select count(*) from jobs").fetchone()[0]
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(out / "profile"),
            executable_path=os.environ["GRABLINE_QA_CHROME"],
            headless=False,
            args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}"],
        )
        try:
            worker = (
                context.service_workers[0]
                if context.service_workers
                else context.wait_for_event("serviceworker")
            )
            page = context.pages[0]
            page.goto(manifest["page"])
            assert page.title() == manifest["title"]
            page.locator("#file").evaluate("a=>a.href='/file.bin?case=missing-host'")
            with page.expect_download(timeout=15000) as info:
                page.locator("#file").click()
            download = info.value
            file = out / "browser-file.bin"
            download.save_as(str(file))
            actual = hashlib.sha256(file.read_bytes()).hexdigest()
            assert actual == manifest["resources"]["/file.bin"]["sha256"]
            failure = worker.evaluate(
                "async()=>await chrome.storage.session.get('lastNativeError')"
            )
            assert failure.get("lastNativeError"), failure
            with sqlite3.connect(dbpath) as db:
                assert db.execute("select count(*) from jobs").fetchone()[0] == before
            evidence.update(
                sha256=actual,
                native_error=failure["lastNativeError"],
                fault_configuration=(
                    "Only HOST_NAME changed to an unregistered namespace "
                    "in a disposable extension copy"
                ),
                normal_registration_preserved=True,
                popup_as_tab_tested=False,
            )
        finally:
            context.close()
