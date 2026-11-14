"""Inspect native dialogs during cold imports, retaining each child exit status."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from common import ROOT, case, record, save

parser = argparse.ArgumentParser()
parser.add_argument("--all-threads", type=int, choices=(-1, 0, 1), default=-1)
args = parser.parse_args()
source = Path(__file__).resolve().parents[2]
outcomes = []
with case(
    "CRASH-COLD-IMPORT-UIA-MODE-" + str(args.all_threads),
    "three real native Qt children + concurrent imports + UIA reads",
) as e:
    for trial in range(3):
        directory = ROOT / "crash-repeat" / ("mode-" + str(args.all_threads)) / str(trial)
        directory.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "GRABLINE_QA_ROOT": str(directory), "PYTHONPATH": str(source)}
        with (directory / "app.log").open("w", encoding="utf-8") as app_log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(source / "scripts/windows_qa/native_crash_session.py"),
                    "--seconds",
                    "18",
                    "--all-threads",
                    str(args.all_threads),
                ],
                env=env,
                stdout=app_log,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 15
                while not (directory / "native-crash/heartbeat.json").exists():
                    assert process.poll() is None, ("Child exited", process.returncode)
                    assert time.monotonic() < deadline, "Native dialog startup timed out"
                    time.sleep(0.1)
                with (directory / "inspection.log").open("w", encoding="utf-8") as inspector_log:
                    inspector = subprocess.run(
                        [
                            sys.executable,
                            str(source / "scripts/windows_qa/native_crash_inspect.py"),
                        ],
                        env=env,
                        stdout=inspector_log,
                        stderr=subprocess.STDOUT,
                        timeout=20,
                    )
                exit_code = process.wait(timeout=30)
                inspection = json.loads(
                    (directory / "cases/CRASH-NATIVE-UIA-READS.json").read_text()
                )
                outcomes.append(
                    {
                        "trial": trial,
                        "app_exit_code": exit_code,
                        "inspector_exit_code": inspector.returncode,
                        "inspection_status": inspection["status"],
                    }
                )
                save("native-crash/repetitions-mode-" + str(args.all_threads) + ".json", outcomes)
                assert exit_code == 0 and inspection["status"] == "PASS", outcomes[-1]
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
    e["outcomes"] = outcomes
if args.all_threads == -1 and len(outcomes) == 3 and all(x["app_exit_code"] == 0 for x in outcomes):
    record(
        "CRASH-HISTORICAL-UNRESOLVED",
        "PASS",
        "same-offset reproduction + production crash-handler correction + native rechecks",
        (
            "Reproduced Windows traceback-dumper crash mitigated by "
            "faulting-thread-only logging. Bounded checks do not certify absence of "
            "every native crash."
        ),
        original_fault_rva="0x287720",
        corrected_windows_all_threads=False,
    )
