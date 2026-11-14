"""Own one real Chromium + app session for serial native/DOM QA commands.

The startup path override selects the isolated source in the real native host;
it does not replace the host, messages, network requests, or browser APIs.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

from common import ROOT, save
from playwright.sync_api import sync_playwright

from app.core.settings import Settings
from app.db.database import Database

source = Path(__file__).resolve().parents[2]
manifest = json.loads((ROOT / "browser-manifest.json").read_text())
profile = ROOT / "browser-app"
profile.mkdir(exist_ok=True)
database = Database(profile / "Grabline/grabline.db")
settings = Settings(database)
settings.setup_seen = settings.host_registered = True
settings.check_updates = settings.confirm_downloads = settings.confirm_video_quality = False
settings.download_dir = profile / "downloads"
settings.min_free_mb = 0
database.close()
bootstrap = ROOT / "bootstrap"
bootstrap.mkdir(exist_ok=True)
(bootstrap / "sitecustomize.py").write_text(
    "import sys,os,json,pathlib\n"
    "p=os.environ.get('GRABLINE_QA_SOURCE')\n"
    "if p:\n"
    " sys.path.insert(0,p)\n"
    " root=pathlib.Path(os.environ['GRABLINE_QA_ROOT'])\n"
    " (root/('source-startup-'+str(os.getpid())+'.json')).write_text(j"
    "son.dumps({'pid':os.getpid(),'source':p}))\n"
)
environment = {
    **os.environ,
    "LOCALAPPDATA": str(profile),
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONPATH": str(bootstrap),
    "GRABLINE_QA_SOURCE": str(source),
    "GRABLINE_QA_ROOT": str(ROOT),
}
environment.pop("QT_QPA_PLATFORM", None)
environment.pop("QT_OPENGL", None)
application_python = os.environ.get("GRABLINE_QA_PYTHON", sys.executable)
application = subprocess.Popen(
    [application_python, "-m", "app", "--minimized"],
    cwd=source,
    env=environment,
    creationflags=subprocess.CREATE_NO_WINDOW,
    stdout=(ROOT / "app-stdout.log").open("w"),
    stderr=(ROOT / "app-stderr.log").open("w"),
)
events = {"worker_console": [], "page_errors": [], "console": []}
commands = ROOT / "commands"
commands.mkdir(exist_ok=True)
extension_revision = hashlib.sha256(
    b"".join(
        (source / "extension" / name).read_bytes()
        for name in ("background.js", "content/overlay.js", "popup/popup.js", "manifest.json")
    )
).hexdigest()[:12]
with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        str(ROOT / ("browser-profile-" + extension_revision)),
        executable_path=os.environ.get("GRABLINE_QA_CHROME"),
        headless=False,
        env=environment,
        args=[
            f"--disable-extensions-except={source / 'extension'}",
            f"--load-extension={source / 'extension'}",
        ],
    )
    worker = (
        context.service_workers[0]
        if context.service_workers
        else context.wait_for_event("serviceworker")
    )
    actual_handoff = worker.evaluate("handoffDownloadItem.toString()")
    background = (source / "extension/background.js").read_text(encoding="utf-8")
    expected_handoff = background.split("async function handoffDownloadItem", 1)[1]
    expected_handoff = (
        "async function handoffDownloadItem"
        + expected_handoff.split("const pendingTakeovers", 1)[0].strip()
    )
    assert actual_handoff.replace("\r\n", "\n") == expected_handoff, (
        "Cached extension source differs from checkout"
    )
    save(
        "extension-running-source.json",
        {
            "background_sha256": hashlib.sha256(
                (source / "extension/background.js").read_bytes()
            ).hexdigest(),
            "handoff_function": actual_handoff,
            "fresh_profile_revision": extension_revision,
        },
    )
    worker.on(
        "console", lambda m: events["worker_console"].append({"type": m.type, "text": m.text})
    )
    page = context.pages[0] if context.pages else context.new_page()
    page.on("pageerror", lambda e: events["page_errors"].append(str(e)))
    page.on("console", lambda m: events["console"].append({"type": m.type, "text": m.text}))
    page.goto(manifest["page"])
    assert page.title() == manifest["title"]
    deadline = time.monotonic() + 20
    while True:
        ping = worker.evaluate(
            "async()=>await chrome.runtime.sendNativeMessage('dev.grabline.host',{type:'ping'})"
        )
        if ping.get("appRunning") or time.monotonic() >= deadline:
            break
        page.wait_for_timeout(100)
    assert ping.get("appRunning"), ping
    save(
        "browser-session.json",
        {
            "ready": True,
            "app_launcher_pid": application.pid,
            "source": str(source),
            "worker_url": worker.url,
            "page": page.url,
            "ping": ping,
            "data_dir": str(profile),
        },
    )
    popup = None
    running = True
    try:
        while running:
            for command_file in sorted(commands.glob("*.json")):
                if command_file.name.endswith(".result.json"):
                    continue
                result_path = command_file.with_name(command_file.stem + ".result.json")
                if result_path.exists():
                    continue
                command = json.loads(command_file.read_text())
                try:
                    op = command["op"]
                    value = None
                    if op == "evaluate":
                        value = page.evaluate(command["js"])
                    elif op == "worker":
                        if context.service_workers:
                            worker = context.service_workers[0]
                        value = worker.evaluate(command["js"])
                    elif op == "worker_stop":
                        cdp = context.new_cdp_session(page)
                        versions = []
                        cdp.on(
                            "ServiceWorker.workerVersionUpdated",
                            lambda event, versions=versions: versions.extend(event["versions"]),
                        )
                        cdp.send("ServiceWorker.enable")
                        deadline = time.monotonic() + 3
                        while not versions and time.monotonic() < deadline:
                            page.wait_for_timeout(50)
                        candidates = [
                            v
                            for v in versions
                            if v.get("scriptURL") == worker.url
                            and v.get("runningStatus") == "running"
                        ]
                        assert candidates, versions
                        value = candidates[-1]
                        cdp.send("ServiceWorker.stopWorker", {"versionId": value["versionId"]})
                        cdp.detach()
                    elif op == "targets":
                        cdp = context.browser.new_browser_cdp_session()
                        value = cdp.send("Target.getTargets")
                        cdp.detach()
                    elif op == "toolbar":
                        cdp = context.browser.new_browser_cdp_session()
                        targets = cdp.send("Target.getTargets")["targetInfos"]
                        candidates = [t for t in targets if t["url"].endswith("/popup/popup.html")]
                        assert len(candidates) == 1, candidates
                        target = candidates[0]
                        session_id = cdp.send(
                            "Target.attachToTarget",
                            {"targetId": target["targetId"], "flatten": False},
                        )["sessionId"]
                        replies = []
                        cdp.on(
                            "Target.receivedMessageFromTarget",
                            lambda event, replies=replies: replies.append(event),
                        )
                        cdp.send(
                            "Target.sendMessageToTarget",
                            {
                                "sessionId": session_id,
                                "message": json.dumps(
                                    {
                                        "id": 1,
                                        "method": "Runtime.evaluate",
                                        "params": {
                                            "expression": command["js"],
                                            "returnByValue": True,
                                            "awaitPromise": True,
                                        },
                                    }
                                ),
                            },
                        )
                        deadline = time.monotonic() + 5
                        reply = None
                        while time.monotonic() < deadline and reply is None:
                            for event in replies:
                                message = json.loads(event["message"])
                                if event["sessionId"] == session_id and message.get("id") == 1:
                                    reply = message
                            page.wait_for_timeout(50)
                        assert reply and "result" in reply, reply
                        assert "exceptionDetails" not in reply["result"], reply
                        value = {"target": target, "result": reply["result"]["result"].get("value")}
                        cdp.send("Target.detachFromTarget", {"sessionId": session_id})
                        cdp.detach()
                    elif op == "goto":
                        page.goto(command["url"])
                    elif op == "hover":
                        page.locator(command["selector"]).hover()
                    elif op == "click":
                        page.locator(command["selector"]).click()
                    elif op == "download":
                        with page.expect_download(timeout=15000) as download_info:
                            page.locator(command["selector"]).click()
                        download = download_info.value
                        target = ROOT / "browser-downloads" / command["name"]
                        target.parent.mkdir(exist_ok=True)
                        download.save_as(str(target))
                        value = {
                            "bytes": target.stat().st_size,
                            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                            "filename": download.suggested_filename,
                        }
                    elif op == "screenshot":
                        page.screenshot(path=str(ROOT / command["name"]), full_page=True)
                    elif op == "mouse_click":
                        page.mouse.click(command["x"], command["y"])
                    elif op == "viewport":
                        page.set_viewport_size(command["size"])
                    elif op in {"overlay_state", "shadow_text"}:
                        cdp = context.new_cdp_session(page)
                        cdp.send("DOM.enable")
                        tree = cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
                        value = []

                        def visit(node, op=op, value=value, cdp=cdp):
                            if op == "shadow_text" and node.get("nodeType") == 3:
                                value.append(node.get("nodeValue", ""))
                            attrs = node.get("attributes", [])
                            attributes = dict(zip(attrs[::2], attrs[1::2], strict=True))
                            if (
                                op == "overlay_state"
                                and attributes.get("title") == "Download with GrabLine"
                            ):
                                ref = cdp.send("DOM.resolveNode", {"nodeId": node["nodeId"]})
                                details = cdp.send(
                                    "Runtime.callFunctionOn",
                                    {
                                        "objectId": ref["object"]["objectId"],
                                        ("functionDeclaration"): (
                                            "function(){const r=this.getBoundingClientRect();let "
                                            "w=this.ownerDocument.defaultView,x=r.x,y=r.y,"
                                            "visible=r.bottom>0&&"
                                            "r.right>0&&r.top<w.innerHeight&&r.left<w.innerWidth;while(w.frame"
                                            "Element){const f=w.frameElement."
                                            "getBoundingClientRect();"
                                            "x+=f.x;y+"
                                            "=f.y;w=w.parent;visible=visible&&f.bottom>0&&f.right>0&&f.top<w.i"
                                            "nnerHeight&&f.left<w.innerWidth;}return "
                                            "{display:visible?getComputedStyle(this).display:'none',x,y,width:"
                                            "r.width,height:r.height,text:this.textContent,document:this.owner"
                                            "Document.URL}}"
                                        ),
                                        "returnByValue": True,
                                    },
                                )
                                value.append(details["result"]["value"])
                            for child in node.get("children", []) + node.get("shadowRoots", []):
                                visit(child)
                            if node.get("contentDocument"):
                                visit(node["contentDocument"])

                        visit(tree["root"])
                        cdp.detach()
                    elif op == "popup":
                        if popup is None or popup.is_closed():
                            popup = context.new_page()
                            popup.goto(worker.url.rsplit("/", 1)[0] + "/popup/popup.html")
                        if command.get("js"):
                            value = popup.evaluate(command["js"])
                        elif command.get("selector"):
                            popup.locator(command["selector"]).click()
                        else:
                            value = popup.locator("body").inner_text()
                    elif op == "front":
                        page.bring_to_front()
                    elif op == "snapshot":
                        value = {
                            "page": page.url,
                            "title": page.title(),
                            "body": page.locator("body").inner_text(),
                        }
                    elif op == "shutdown":
                        running = False
                    else:
                        raise ValueError(op)
                    result = {"ok": True, "value": value}
                except Exception as exc:
                    result = {"ok": False, "error": str(exc), "traceback": traceback.format_exc()}
                result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
                save("browser-events.json", events)
            page.wait_for_timeout(200)
    finally:
        save("browser-events.json", events)
        context.close()
        if application.poll() is None:
            application.terminate()
            application.wait(timeout=15)
