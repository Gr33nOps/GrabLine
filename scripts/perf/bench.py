"""Grabline performance baseline - the table every optimization is judged against.

Run it the same way before and after a change:

    python scripts/perf/bench.py all           # everything
    python scripts/perf/bench.py startup idle  # just those

Every number is a real measurement of this machine, so compare runs from the
same session; absolute values move between machines, the deltas are the point.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
OUT = Path(os.environ.get("PERF_OUT", "/tmp/grabline-perf"))
OUT.mkdir(parents=True, exist_ok=True)


def _fmt(value: float, unit: str) -> str:
    return f"{value:.1f} {unit}"


# ----------------------------------------------------------------- startup


def measure_startup(runs: int = 5) -> dict:
    """Cold start to an interactive window, measured inside the process: the
    app prints the elapsed time at first paint and exits."""
    probe = OUT / "startup_probe.py"
    probe.write_text(
        "import os, sys, time\n"
        "T0 = time.monotonic()\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "os.environ.setdefault('GRABLINE_PERF_PROBE', '1')\n"
        "from PySide6.QtCore import QTimer\n"
        "from PySide6.QtWidgets import QApplication\n"
        "import app.__main__ as m\n"
        "T_IMPORT = time.monotonic()\n"
        "def report():\n"
        "    print(json.dumps({'import': T_IMPORT - T0, 'total': time.monotonic() - T0}))\n"
        "    QApplication.instance().quit()\n"
        "import json\n"
        "QTimer.singleShot(0, report)\n"
        "sys.exit(m.main())\n"
    )
    imports, totals = [], []
    for _ in range(runs):
        result = subprocess.run(
            [sys.executable, str(probe)],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=120,
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                data = json.loads(line)
                imports.append(data["import"] * 1000)
                totals.append(data["total"] * 1000)
    if not totals:
        return {"error": "no startup samples"}
    return {
        "import_ms_median": statistics.median(imports),
        "total_ms_median": statistics.median(totals),
        "total_ms_min": min(totals),
        "runs": len(totals),
    }


def measure_import_cost() -> dict:
    """What the import graph costs, and which heavy modules load eagerly."""
    code = (
        "import sys, time, json\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "t0 = time.monotonic()\n"
        "import app.__main__\n"
        "elapsed = time.monotonic() - t0\n"
        "heavy = [m for m in ('yt_dlp','libtorrent','boto3','paramiko','psutil','PIL',"
        "'httpx','botocore') if m in sys.modules]\n"
        "print(json.dumps({'ms': elapsed*1000, 'modules': len(sys.modules), 'heavy': heavy}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT), timeout=120
    )
    for line in result.stdout.splitlines():
        if line.strip().startswith("{"):
            return json.loads(line)
    return {"error": result.stderr[-400:]}


# -------------------------------------------------------------------- cost


def _sample_cpu(pid: int, seconds: float) -> dict:
    import psutil

    proc = psutil.Process(pid)
    proc.cpu_percent(None)
    ctx0 = proc.num_ctx_switches()
    time.sleep(seconds)
    cpu = proc.cpu_percent(None)
    ctx1 = proc.num_ctx_switches()
    return {
        "cpu_percent": cpu,
        "wakeups_per_s": ((ctx1.voluntary - ctx0.voluntary) + (ctx1.involuntary - ctx0.involuntary))
        / seconds,
        "rss_mb": proc.memory_info().rss / (1024 * 1024),
    }


def measure_idle(seconds: float = 6.0) -> dict:
    """CPU while the app sits there: window open, on the Dashboard, and hidden
    to the tray. This is the number a laptop's fan responds to."""
    script = OUT / "idle_probe.py"
    script.write_text(
        "import os, sys, time\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from PySide6.QtCore import QTimer\n"
        "from PySide6.QtWidgets import QApplication\n"
        "import app.__main__ as m\n"
        "import threading\n"
        "def drive():\n"
        "    time.sleep(1.5)\n"
        "    print('READY', flush=True)\n"
        "    while True:\n"
        "        line = sys.stdin.readline().strip()\n"
        "        if line == 'dashboard':\n"
        "            QTimer.singleShot(0, lambda: _switch('dashboard'))\n"
        "        elif line == 'hide':\n"
        "            QTimer.singleShot(0, _hide)\n"
        "        elif line == 'quit':\n"
        "            QTimer.singleShot(0, QApplication.instance().quit); return\n"
        "        print('OK', flush=True)\n"
        "def _window():\n"
        "    for w in QApplication.topLevelWidgets():\n"
        "        if w.__class__.__name__ == 'MainWindow':\n"
        "            return w\n"
        "def _switch(name):\n"
        "    w = _window()\n"
        "    if w is not None: w._switch_view(name)\n"
        "def _hide():\n"
        "    w = _window()\n"
        "    if w is not None: w.hide()\n"
        "threading.Thread(target=drive, daemon=True).start()\n"
        "sys.exit(m.main())\n"
    )
    env = {**os.environ, "GRABLINE_PERF_PROBE": "1"}
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        cwd=str(ROOT),
        env=env,
    )
    assert proc.stdout is not None and proc.stdin is not None
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if line.strip() == "READY":
            break
    else:
        proc.kill()
        return {"error": "app never became ready"}
    # Let the background extractor warm-up finish first: it enumerates 1700+
    # yt-dlp extractors and would otherwise be counted as idle cost.
    time.sleep(8.0)
    results = {"downloads_page": _sample_cpu(proc.pid, seconds)}
    for command, label in (("dashboard", "dashboard_page"), ("hide", "hidden_to_tray")):
        proc.stdin.write(command + "\n")
        proc.stdin.flush()
        proc.stdout.readline()
        time.sleep(1.0)
        results[label] = _sample_cpu(proc.pid, seconds)
    proc.stdin.write("quit\n")
    proc.stdin.flush()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    return results


# ------------------------------------------------------------------ report


def render(results: dict) -> str:
    lines = ["", "=" * 74, "GRABLINE PERFORMANCE BASELINE", "=" * 74]
    startup = results.get("startup")
    if startup and "error" not in startup:
        lines += [
            "",
            f"STARTUP (to first paint, median of {startup['runs']})",
            f"  import app.__main__      {_fmt(startup['import_ms_median'], 'ms')}",
            f"  total to interactive     {_fmt(startup['total_ms_median'], 'ms')}"
            f"   (best {_fmt(startup['total_ms_min'], 'ms')})",
        ]
    imports = results.get("imports")
    if imports and "error" not in imports:
        lines += [
            f"  modules imported         {imports['modules']}",
            f"  heavy modules eager      {', '.join(imports['heavy']) or '(none)'}",
        ]
    idle = results.get("idle")
    if idle and "error" not in idle:
        lines += ["", "IDLE COST (zero downloads)"]
        lines += [f"  {'state':<24}{'CPU %':>8}{'wakeups/s':>12}{'RSS MB':>10}"]
        for label, data in idle.items():
            lines.append(
                f"  {label:<24}{data['cpu_percent']:>8.1f}"
                f"{data['wakeups_per_s']:>12.0f}{data['rss_mb']:>10.1f}"
            )
    lines += ["", "=" * 74, ""]
    return "\n".join(lines)


def main() -> int:
    wanted = set(sys.argv[1:]) or {"all"}
    everything = "all" in wanted
    results: dict = {}
    if everything or "startup" in wanted:
        print("measuring startup ...", flush=True)
        results["startup"] = measure_startup()
        results["imports"] = measure_import_cost()
    if everything or "idle" in wanted:
        print("measuring idle ...", flush=True)
        results["idle"] = measure_idle()
    print(render(results))
    label = os.environ.get("PERF_LABEL", "run")
    (OUT / f"{label}.json").write_text(json.dumps(results, indent=2))
    print(f"raw numbers -> {OUT / (label + '.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
