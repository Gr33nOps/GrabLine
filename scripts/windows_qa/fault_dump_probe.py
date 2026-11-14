"""Bounded subprocess probe for handled Windows exceptions during code churn."""

from __future__ import annotations

import argparse
import ctypes
import faulthandler
import json
import os
import subprocess
import sys
import threading
import time
import types

from common import record, save

parser = argparse.ArgumentParser()
parser.add_argument("--worker", type=int, choices=(0, 1))
args = parser.parse_args()
if args.worker is not None:
    # All changes are process-local; this worker never opens a GUI or alters OS policy.
    stop = threading.Event()

    def template():
        return 1

    def churn():
        index = 0
        while not stop.is_set():
            index += 1
            code = template.__code__.replace(
                co_filename=f"/qa/generated-{index}.py", co_name=f"generated_{index}"
            )
            function = types.FunctionType(code, {})
            function()
            function = code = None

    thread = threading.Thread(target=churn)
    sys.setswitchinterval(0.00001)
    thread.start()
    count = 0
    with open(os.devnull, "w") as stream:
        faulthandler.enable(stream, all_threads=bool(args.worker))
        deadline = time.monotonic() + 5
        try:
            while time.monotonic() < deadline:
                try:
                    ctypes.windll.kernel32.RaiseException(0x8001010D, 0, 0, None)
                except OSError:
                    count += 1
        finally:
            faulthandler.disable()
            stop.set()
            thread.join(timeout=5)
    print(json.dumps({"handled_exceptions": count, "all_threads": bool(args.worker)}))
else:
    attempts = []
    for mode in (1, 0):
        for trial in range(3):
            p = subprocess.run(
                [sys.executable, __file__, "--worker", str(mode)],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            attempts.append(
                {
                    "all_threads": bool(mode),
                    "trial": trial,
                    "exit_code": p.returncode,
                    "stdout": p.stdout[-1000:],
                    "stderr": p.stderr[-1000:],
                }
            )
            save("crash-investigation/fault-probe-attempts.json", attempts)
    record(
        "CRASH-FAULT-DUMP-PROBE",
        "PASS" if all(a["exit_code"] == 0 for a in attempts) else "FAIL",
        "isolated Windows exception/code-churn subprocesses",
        "Diagnostic reproduction only; does not certify the application crash as fixed",
        attempts=attempts,
    )
