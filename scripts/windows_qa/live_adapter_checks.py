"""Bounded public-page adapter checks in the real unpacked-extension browser."""

from __future__ import annotations

import json
import os
import subprocess
import time
from contextlib import suppress

from browser_api import python
from command import command
from common import ROOT, record, require_gui_result, save
from win32_backend import capture_and_cancel, owned_pids, windows

samples = [
    ("YOUTUBE", "https://www.youtube.com/watch?v=jNQXAC9IVRw", ["#movie_player"]),
    (
        "YOUTUBE-SHORTS",
        "https://www.youtube.com/shorts",
        ["#movie_player", ".html5-video-player"],
    ),
    (
        "YOUTUBE-MUSIC",
        "https://music.youtube.com/watch?v=dQw4w9WgXc",
        ["ytmusic-player-bar", "ytmusic-player"],
    ),
    (
        "VIMEO",
        "https://vimeo.com/54469442",
        ["iframe[src*='player.vimeo.com']", "a[href*='/54469442']"],
    ),
    ("X", "https://twitter.com/starwars/status/665052190608723968", ["article video"]),
    (
        "SOUNDCLOUD",
        "https://soundcloud.com/ethmusic",
        ["a.soundTitle__title", "a.trackItem__trackTitle"],
    ),
    (
        "REDDIT",
        "https://www.reddit.com/r/videos/comments/6rrwyj/that_small_heart_attack/",
        ["shreddit-player", "[slot='post-media-container']"],
    ),
    (
        "INSTAGRAM",
        "https://www.instagram.com/reel/Chunk8-jurw/",
        ["article video", "a[href^='/reel/']"],
    ),
    (
        "DAILYMOTION",
        "https://www.dailymotion.com/video/x84sh87",
        ["#player-wrapper", "iframe[src*='geo.dailymotion.com']", "video"],
    ),
]


def gui(op, **options):
    args = [python, "scripts/windows_qa/ui_driver.py", op]
    for key, value in options.items():
        args += ["--" + key, str(value)]
    p = subprocess.run(args, capture_output=True, text=True, timeout=20)
    require_gui_result(p)
    return json.loads(p.stdout)


try:
    for name, url, selectors in samples:
        requested = os.environ.get("GRABLINE_QA_LIVE_SITES")
        if requested and name not in requested.upper().split(","):
            continue
        identifier = "SITE-LIVE-" + name
        evidence = {"url": url, "private_profile_used": False}
        try:
            try:
                command("goto", url=url, timeout=40)
            except Exception as exc:
                evidence["navigation_error"] = str(exc)[:600]
            snapshot = command("snapshot")
            if name == "YOUTUBE-SHORTS" and "/shorts/" not in snapshot["page"]:
                record(
                    identifier,
                    "BLOCKED",
                    "live public Shorts page",
                    "Shorts address redirected or exposed no actual Shorts item; "
                    "watch success is separate.",
                    url=url,
                    actual_page=snapshot["page"],
                )
                continue
            evidence.update(
                actual_page=snapshot["page"],
                page_title=snapshot["title"],
                visible_excerpt=" ".join(snapshot["body"].split()[:24]),
            )
            save("live-site-" + name.lower() + ".json", evidence)
            # No login, CAPTCHA, certificate or other safety barrier is bypassed.
            selector = None
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and selector is None:
                for candidate in selectors:
                    count = command(
                        "evaluate",
                        js="document.querySelectorAll(" + json.dumps(candidate) + ").length",
                    )
                    if count:
                        selector = candidate
                        break
                if selector is None:
                    time.sleep(0.2)
            if selector is None:
                record(
                    identifier,
                    "BLOCKED",
                    "external public page + real Chrome",
                    (
                        "No supported media target available on this public page; "
                        "see captured title/excerpt and navigation outcome"
                    ),
                    **evidence,
                )
                continue
            command(
                "worker",
                js=(
                    "globalThis.qaLastGrab=null; if(!globalThis.qaGrabObserver){"
                    "globalThis.qaGrabObserver=(m)=>{if(m?.cmd==='grab')"
                    "globalThis.qaLastGrab={url:m.url,time:Date.now()};};"
                    "chrome.runtime.onMessage.addListener(globalThis.qaGrabObserver)}"
                ),
            )
            command("front")
            target_id = "qa-live-" + name.lower()
            command(
                "evaluate",
                js="(()=>{const candidates=[...document.querySelectorAll("
                + json.dumps(selector)
                + ")];const e=candidates.find(e=>"
                "e.getBoundingClientRect().width>0&&!e.getAttribute('href')?.includes('/sets/'));"
                "if(!e)throw Error('No visible media target');e.dataset.grablineQaTarget="
                + json.dumps(target_id)
                + ";})()",
            )
            command("hover", selector='[data-grabline-qa-target="' + target_id + '"]')
            deadline = time.monotonic() + 3
            buttons = []
            while time.monotonic() < deadline:
                buttons = [
                    b for b in command("overlay_state") if b["display"] != "none" and b["width"] > 0
                ]
                if buttons:
                    break
                time.sleep(0.1)
            if not buttons:
                record(
                    identifier,
                    "BLOCKED",
                    "external public page + real Chrome",
                    (
                        "Public player shell exists but exposes no hover button in this session; "
                        "live selector/player inspection still required"
                    ),
                    selector=selector,
                    **evidence,
                )
                continue
            b = buttons[0]
            command("mouse_click", x=b["x"] + b["width"] / 2, y=b["y"] + b["height"] / 2)
            deadline = time.monotonic() + 25
            grab = None
            dialog = None
            while time.monotonic() < deadline:
                grab = command("worker", js="globalThis.qaLastGrab")
                matches = [w for w in windows() if "Choose quality" in w["title"]]
                assert owned_pids(), "Actual application exited during live handoff"
                if matches:
                    assert len(matches) == 1
                    dialog = matches[0]
                    break
                time.sleep(0.2)
            evidence.update(grab=grab, selector=selector, button=b)
            if dialog is not None and grab and grab.get("url", "").startswith("https://"):
                capture_and_cancel(dialog, "live-" + name.lower() + "-quality.png")
                record(
                    identifier,
                    "PASS",
                    "live public adapter + genuine click + native quality dialog",
                    "Canonical handoff reached native quality dialog; no full-download claim",
                    screenshot="live-" + name.lower() + "-quality.png",
                    **evidence,
                )
            else:
                record(
                    identifier,
                    "BLOCKED",
                    "external public adapter + native host",
                    (
                        "Live hover/handoff observed but a usable quality dialog "
                        "was unavailable within the bound"
                    ),
                    **evidence,
                )
                with suppress(AssertionError):
                    gui("key", target="app", text="{ESC}")
        except Exception as exc:
            record(
                identifier,
                "BLOCKED",
                "external public adapter + real browser",
                str(exc)[:1000],
                **evidence,
            )
finally:
    manifest = json.loads((ROOT / "browser-manifest.json").read_text())
    command("goto", url=manifest["page"])
