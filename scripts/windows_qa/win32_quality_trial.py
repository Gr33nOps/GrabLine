"""Observe real quality dialogs through Win32, without UI Automation tree walks."""
# ruff: noqa: E402 -- DPI and optional module path must precede GUI imports.

from __future__ import annotations

import ctypes
import json
import os
import site
import time

ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
if os.environ.get("GRABLINE_QA_GUI_MODULES"):
    site.addsitedir(os.environ["GRABLINE_QA_GUI_MODULES"])

import psutil
import win32gui
import win32process
from command import command
from common import ROOT, Blocked, case
from PIL import ImageGrab
from pywinauto.keyboard import send_keys


def owned_pids():
    result = set()
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            args = p.info["cmdline"] or []
            if "app" in args and p.environ().get("LOCALAPPDATA") == str(ROOT / "browser-app"):
                result.add(p.pid)
        except psutil.Error:
            pass
    return result


def windows():
    pids = owned_pids()
    result = []

    def visit(handle, _):
        if win32process.GetWindowThreadProcessId(handle)[1] in pids and win32gui.IsWindowVisible(
            handle
        ):
            result.append(
                {
                    "handle": handle,
                    "title": win32gui.GetWindowText(handle),
                    "rect": win32gui.GetWindowRect(handle),
                }
            )

    win32gui.EnumWindows(visit, None)
    return result


with case(
    "SITE-LIVE-YOUTUBE-WIN32", "live real Chrome + native quality dialog through Win32 GUI"
) as evidence:
    security_dialogs = []

    def security_window(handle, _):
        if (
            win32gui.IsWindowVisible(handle)
            and win32gui.GetWindowText(handle) == "Windows Security Alert"
        ):
            security_dialogs.append(handle)

    win32gui.EnumWindows(security_window, None)
    if security_dialogs:
        raise Blocked("Windows Security Alert requires user dismissal; no native input was sent")
    url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    command("goto", url=url, timeout=40)
    command("front")
    command("hover", selector="#movie_player")
    deadline = time.monotonic() + 3
    buttons = []
    while time.monotonic() < deadline:
        buttons = [
            b
            for b in command("overlay_state")
            if b["display"] != "none" and b["width"] > 0 and b["document"].startswith(url)
        ]
        if buttons:
            break
        time.sleep(0.1)
    assert buttons
    b = buttons[0]
    command("mouse_click", x=b["x"] + b["width"] / 2, y=b["y"] + b["height"] / 2)
    deadline = time.monotonic() + 35
    dialog = None
    observations = []
    while time.monotonic() < deadline:
        observed = windows()
        observations.append(observed)
        matches = [w for w in observed if "Choose quality" in w["title"]]
        if matches:
            assert len(matches) == 1
            dialog = matches[0]
            break
        assert owned_pids(), "Actual application child process exited"
        time.sleep(0.1)
    evidence["observations"] = observations
    assert dialog, observations[-5:]
    win32gui.SetForegroundWindow(dialog["handle"])
    assert win32process.GetWindowThreadProcessId(win32gui.GetForegroundWindow())[1] in owned_pids()
    ImageGrab.grab(bbox=tuple(dialog["rect"])).save(ROOT / "live-youtube-win32-quality.png")
    send_keys("{ESC}", vk_packet=False)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and win32gui.IsWindowVisible(dialog["handle"]):
        time.sleep(0.1)
    assert not win32gui.IsWindowVisible(dialog["handle"])
    assert owned_pids()
    evidence.update(
        url=url,
        screenshot="live-youtube-win32-quality.png",
        cancellation_verified=True,
        UIA_used=False,
    )

manifest = json.loads((ROOT / "browser-manifest.json").read_text())
command("goto", url=manifest["page"])
