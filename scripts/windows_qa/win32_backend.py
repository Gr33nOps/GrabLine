"""Scoped Win32 window fallback; deliberately avoids Qt UIA tree walks."""
# ruff: noqa: E402 -- DPI awareness and optional GUI module path precede imports.

from __future__ import annotations

import ctypes
import os
import site
import time

ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
if os.environ.get("GRABLINE_QA_GUI_MODULES"):
    site.addsitedir(os.environ["GRABLINE_QA_GUI_MODULES"])
import psutil
import win32con as win32con
import win32gui
import win32process
from common import ROOT, Blocked
from PIL import ImageGrab
from pywinauto import Desktop
from pywinauto.keyboard import send_keys


def owned_pids():
    result = set()
    for p in psutil.process_iter(["cmdline"]):
        try:
            if "app" in (p.info["cmdline"] or []) and p.environ().get("LOCALAPPDATA") == str(
                ROOT / "browser-app"
            ):
                result.add(p.pid)
        except psutil.Error:
            pass
    return result


def windows():
    pids = owned_pids()
    result = []
    security = []

    def visit(h, _):
        if not win32gui.IsWindowVisible(h):
            return
        title = win32gui.GetWindowText(h)
        if title == "Windows Security Alert":
            security.append(h)
        if win32process.GetWindowThreadProcessId(h)[1] in pids:
            result.append({"handle": h, "title": title, "rect": win32gui.GetWindowRect(h)})

    win32gui.EnumWindows(visit, None)
    if security:
        raise Blocked("Windows Security Alert requires user dismissal; no input sent")
    return result


def focus(row):
    assert row in windows()
    Desktop(backend="win32").window(handle=row["handle"]).set_focus()
    assert win32gui.GetForegroundWindow() == row["handle"]


def capture_and_cancel(row, name):
    focus(row)
    ImageGrab.grab(bbox=tuple(row["rect"])).save(ROOT / name)
    assert win32gui.GetForegroundWindow() == row["handle"]
    send_keys("{ESC}", vk_packet=False)
    end = time.monotonic() + 5
    while win32gui.IsWindowVisible(row["handle"]) and time.monotonic() < end:
        time.sleep(0.1)
    assert not win32gui.IsWindowVisible(row["handle"])
    assert owned_pids()
