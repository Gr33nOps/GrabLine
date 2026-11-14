"""Explicitly scoped Windows GUI fallback for the disposable QA session."""
# ruff: noqa: E402 -- DPI and optional GUI module path must precede UIA imports.

from __future__ import annotations

import argparse
import ctypes
import json
import os
import site

# UIA rectangles are physical pixels; keep mouse injection in the same space
# after monitor scaling changes, without changing Windows display settings.
ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))

if os.environ.get("GRABLINE_QA_GUI_MODULES"):
    site.addsitedir(os.environ["GRABLINE_QA_GUI_MODULES"])
import contextlib

import psutil
import win32gui
import win32process
from common import ROOT, save
from pywinauto import Desktop, timings
from pywinauto.keyboard import send_keys

parser = argparse.ArgumentParser()
parser.add_argument(
    "op", choices=["inspect", "popup", "click", "type", "key", "context", "shot", "lastrow"]
)
parser.add_argument("--target", default="chrome", choices=["chrome", "app"])
parser.add_argument("--label")
parser.add_argument("--control", default="Button")
parser.add_argument("--text")
parser.add_argument("--name", default="ui-latest")
args = parser.parse_args()
security_dialogs = []


def check_security_dialog(handle, _):
    if (
        win32gui.IsWindowVisible(handle)
        and win32gui.GetWindowText(handle) == "Windows Security Alert"
    ):
        security_dialogs.append(handle)


win32gui.EnumWindows(check_security_dialog, None)
if security_dialogs:
    raise SystemExit(
        "SECURITY_DIALOG_BLOCKED: Windows permission dialog requires the user; "
        "no desktop input was sent."
    )
if args.target == "chrome":
    windows = [
        w
        for w in Desktop(backend="uia").windows()
        if w.window_text() == "GrabLine complete QA - Google Chrome for Testing"
    ]
else:
    windows = []
    for w in Desktop(backend="uia").windows():
        if w.window_text().removesuffix(" - GrabLine") in {
            "GrabLine",
            "Images on this page",
            "Links on this page",
            "Download Info",
            "Choose quality",
            "Cloud accounts",
            "Cloud account",
        }:
            try:
                if psutil.Process(w.element_info.process_id).environ().get("LOCALAPPDATA") == str(
                    ROOT / "browser-app"
                ):
                    windows.append(w)
            except (psutil.Error, OSError):
                pass
    modal_roots = [w for w in windows if w.window_text().removesuffix(" - GrabLine") != "GrabLine"]
    if modal_roots and args.op != "inspect":
        windows = modal_roots
    # The active modal is the intended target; require a unique match if supplied.
    if args.label and args.op == "inspect":
        nested = [child for w in windows for child in w.descendants(control_type="Window")]
        windows = [
            w for w in windows + nested if w.window_text().removesuffix(" - GrabLine") == args.label
        ]
assert len(windows) == 1, [(w.handle, w.window_text()) for w in windows]
window = windows[0]
if args.target == "app" and args.op != "inspect":
    modals = [child for child in window.descendants(control_type="Window") if child.is_visible()]
    assert len(modals) <= 1, [(w.handle, w.window_text()) for w in modals]
    if modals:
        window = modals[0]
pid = window.element_info.process_id
if win32process.GetWindowThreadProcessId(win32gui.GetForegroundWindow())[1] != pid:
    Desktop(backend="win32").window(handle=window.handle).set_focus()
assert win32process.GetWindowThreadProcessId(win32gui.GetForegroundWindow())[1] == pid


def match(label: str, control: str = "Button"):
    values = [e for e in window.descendants(control_type=control) if e.window_text() == label]
    assert len(values) == 1, [(e.window_text(), str(e.rectangle())) for e in values]
    return values[0]


if args.op == "popup":
    send_keys("{ESC}")
    direct = [
        e
        for e in window.descendants(control_type="Button")
        if e.window_text().startswith("GrabLine Connect\n")
    ]
    if not direct:
        match("Extensions").click_input()
        timings.wait_until(
            5,
            0.1,
            lambda: any(
                e.window_text() == "Pin GrabLine Connect"
                for e in window.descendants(control_type="Button")
            ),
        )
        match("Pin GrabLine Connect").click_input()
        send_keys("{ESC}")
        direct = [
            e
            for e in window.descendants(control_type="Button")
            if e.window_text().startswith("GrabLine Connect\n")
        ]
    assert len(direct) == 1
    direct[0].click_input()
    timings.wait_until(
        10,
        0.1,
        lambda: any(
            e.window_text() == "connected" for e in window.descendants(control_type="Text")
        ),
    )
    if args.label:
        element = match(args.label, args.control)
        if args.control == "CheckBox":
            previous = element.get_toggle_state()
            element.set_focus()
            send_keys("{SPACE}")
            timings.wait_until(
                3, 0.1, lambda: match(args.label, args.control).get_toggle_state() != previous
            )
        else:
            if args.control in {"Hyperlink", "ComboBox"}:
                with contextlib.suppress(Exception):
                    element.iface_scroll_item.ScrollIntoView()
                element = match(args.label, args.control)
            element.click_input()
        if args.text is not None:
            if args.control == "ComboBox":
                element.set_focus()
                send_keys(args.text + "{TAB}")
            else:
                send_keys("^a")
                escaped = "".join("{" + c + "}" if c in "()+^%~{}" else c for c in args.text)
                send_keys(escaped, with_spaces=True, pause=0.001)
                match("Send").click_input()
elif args.op in {"click", "context"}:
    element = match(args.label, args.control)
    with contextlib.suppress(Exception):
        element.iface_scroll_item.ScrollIntoView()
    rect = element.rectangle()
    bounds = window.rectangle()
    inside = (
        bounds.left <= rect.mid_point().x < bounds.right
        and bounds.top <= rect.mid_point().y < bounds.bottom
    )
    if not inside:
        # Chromium's owned menus can extend beyond the parent browser window.
        point_window = win32gui.WindowFromPoint((rect.mid_point().x, rect.mid_point().y))
        assert (
            args.control == "MenuItem"
            and win32process.GetWindowThreadProcessId(point_window)[1] == pid
        ), (args.label, rect, bounds)
    element.click_input(button="right" if args.op == "context" else "left")
elif args.op == "lastrow":
    table = [w for w in window.descendants(control_type="Table") if w.is_visible()]
    assert len(table) == 1
    table[0].set_focus()
    send_keys("^{END}")
elif args.op == "type":
    edit = match(args.label, args.control)
    edit.click_input()
    edit.type_keys(args.text, with_spaces=True, set_foreground=False)
elif args.op == "key":
    send_keys(args.text, vk_packet=False)
elif args.op == "shot":
    window.capture_as_image().save(str(ROOT / (args.name + ".png")))
rows = []
for e in window.descendants():
    row = {"type": e.element_info.control_type, "text": e.window_text(), "rect": str(e.rectangle())}
    if row["type"] == "CheckBox":
        row["checked"] = e.get_toggle_state()
    rows.append(row)
save(args.name + ".json", {"window": window.window_text(), "handle": window.handle, "rows": rows})
if (
    args.op == "popup"
    and args.label
    and win32process.GetWindowThreadProcessId(win32gui.GetForegroundWindow())[1] == pid
):
    send_keys("{ESC}")
print(json.dumps(rows, ensure_ascii=True))
