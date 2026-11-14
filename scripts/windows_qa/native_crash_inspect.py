"""Read only the explicitly owned QA dialog through the requested UIA fallback."""

import json
import site
import time

import psutil
from common import ROOT, Blocked, case, save

site.addsitedir(r"D:\Projects\app dev\GrabLine\.dev-pc\qa\followup\gui-tools")
from pywinauto import Desktop

with case(
    "CRASH-NATIVE-UIA-READS", "actual Windows Qt dialog + scoped UI Automation fallback"
) as e:
    heartbeat = json.loads((ROOT / "native-crash/heartbeat.json").read_text())
    process = psutil.Process(heartbeat["pid"])
    assert process.environ().get("LOCALAPPDATA") == str(ROOT / "native-crash/profile")
    if not heartbeat.get("window_id"):
        raise Blocked("Test process did not publish a native window identifier")
    window = Desktop(backend="uia").window(handle=heartbeat["window_id"])
    assert window.process_id() == heartbeat["pid"]
    counts = []
    for _index in range(30):
        assert process.is_running(), "Target process exited during inspection"
        controls = window.descendants()
        assert controls
        counts.append(len(controls))
        save("native-crash/uia-counts.json", counts)
        time.sleep(0.1)
    e.update(
        reads=len(counts),
        minimum_controls=min(counts),
        maximum_controls=max(counts),
        process_alive=True,
        target_pid=heartbeat["pid"],
    )
