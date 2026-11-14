"""Native minimize/close/reopen and taskbar accessibility preflight."""

from __future__ import annotations

import time

from command import command
from common import case, record
from win32_backend import Desktop, focus, owned_pids, win32con, win32gui, windows


def reopen():
    reply = command(
        "worker",
        js="async()=>await chrome.runtime.sendNativeMessage('dev.grabline.host',{type:'focus'})",
    )
    assert reply["type"] != "error", reply
    end = time.monotonic() + 5
    while time.monotonic() < end:
        rows = [w for w in windows() if w["title"] == "GrabLine"]
        if rows and not win32gui.IsIconic(rows[0]["handle"]):
            return rows[0]
        time.sleep(0.1)
    raise TimeoutError("Native host focus did not restore GrabLine")


with case(
    "DESKTOP-NATIVE-MINIMIZE-CLOSE-REOPEN", "native Win32 window actions + real native host focus"
) as e:
    row = reopen()
    focus(row)
    h = row["handle"]
    win32gui.ShowWindow(h, win32con.SW_MINIMIZE)
    assert win32gui.IsIconic(h) or not win32gui.IsWindowVisible(h)
    reopen()
    win32gui.PostMessage(h, win32con.WM_CLOSE, 0, 0)
    end = time.monotonic() + 5
    while time.monotonic() < end and win32gui.IsWindowVisible(h):
        time.sleep(0.1)
    assert not win32gui.IsWindowVisible(h) and owned_pids()
    reopen()
    e.update(window_handle=h, close_kept_process_alive=True, reopen_via="real native host focus")
h = win32gui.FindWindow("Shell_TrayWnd", None)
names = []
if h:
    w = Desktop(backend="uia").window(handle=h)
    names = [
        e.window_text()
        for e in w.descendants(control_type="Button")
        if "GrabLine" in e.window_text()
    ]
record(
    "DESKTOP-PHYSICAL-TRAY",
    "BLOCKED",
    "physical Windows shell icon interaction",
    "A stable unique notification-area icon was not established through available "
    "automation. An intermittent taskbar button does not prove tray-icon ownership. "
    "Qt tray action/state paths passed separately; no shell target was guessed.",
    taskbar_handle=h,
    matching_icons=names,
)
