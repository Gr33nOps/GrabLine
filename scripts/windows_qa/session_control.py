"""Stop only app processes proven to belong to this disposable QA profile."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psutil
from common import ROOT, save


def start_app():
    source = Path(__file__).resolve().parents[2]
    environment = {
        **os.environ,
        "LOCALAPPDATA": str(ROOT / "browser-app"),
        "PYTHONPATH": str(ROOT / "bootstrap"),
        "GRABLINE_QA_SOURCE": str(source),
        "GRABLINE_QA_ROOT": str(ROOT),
    }
    environment.pop("QT_QPA_PLATFORM", None)
    environment.pop("QT_OPENGL", None)
    return subprocess.Popen(
        [os.environ.get("GRABLINE_QA_PYTHON", sys.executable), "-m", "app", "--minimized"],
        cwd=source,
        env=environment,
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdout=(ROOT / "app-restarted-stdout.log").open("w"),
        stderr=(ROOT / "app-restarted-stderr.log").open("w"),
    )


def stop_app():
    owned = []
    for process in psutil.process_iter(["pid", "cmdline"]):
        try:
            args = process.info["cmdline"] or []
            if "-m" not in args or args[args.index("-m") + 1 : args.index("-m") + 2] != ["app"]:
                continue
            environment = process.environ()
            if environment.get("LOCALAPPDATA") != str(ROOT / "browser-app"):
                continue
            source = environment.get("GRABLINE_QA_SOURCE")
            assert source, "App data matches, but source identity is unverified"
            owned.append(process)
        except (psutil.Error, OSError):
            continue
    save("app-stop-identity.json", [{"pid": p.pid, "args": p.cmdline()} for p in owned])
    # The Windows venv executable is a launcher; stop its real child too.
    for process in sorted(owned, key=lambda p: len(p.parents()), reverse=True):
        process.terminate()
    _, remaining = psutil.wait_procs(owned, timeout=10)
    assert not remaining
    return [p.pid for p in owned]


if __name__ == "__main__":
    print(stop_app())
