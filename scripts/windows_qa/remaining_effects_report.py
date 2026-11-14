"""Assemble sanitized evidence for the remaining Windows integration checks."""

import argparse
import csv
import hashlib
import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from common import ROOT

parser = argparse.ArgumentParser()
parser.add_argument("--output", required=True, type=Path)
args = parser.parse_args()
out = args.output.resolve()
out.mkdir(parents=True, exist_ok=True)
source = Path(__file__).resolve().parents[2]
host = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in (ROOT / "cases").glob("*.json")}
guest = {
    p.stem: json.loads(p.read_text(encoding="utf-8"))
    for p in (ROOT / "sandbox/cases").glob("*.json")
}
suite = ET.parse(ROOT / "windows-tests.xml").getroot().find("testsuite")
assert suite is not None and int(suite.attrib["failures"]) == int(suite.attrib["errors"]) == 0
assert host["PACKAGE-BUILD"]["status"] == host["PACKAGE-NATIVE-TRANSFER"]["status"] == "PASS"
rows = []
groups = {
    "Native crash": ["CRASH-HISTORICAL-UNRESOLVED", "CRASH-COLD-IMPORT-UIA-MODE--1"],
    "Windows startup": ["GUEST-REAL-AUTOSTART-REGISTRY", "GUEST-LOGIN-REBOOT"],
    "Windows install": [
        "GUEST-INSTALLER-BUILD",
        "GUEST-INSTALL-SHORTCUTS-ASSOCIATIONS",
        "GUEST-UNINSTALL-PRESERVES-DOWNLOADS",
    ],
    "Browser registration": ["GUEST-NATIVE-HOST-REGISTRATION-TRANSFER"],
    "Updates": [
        "GUEST-UPDATE-INSTALLER-HANDOFF",
        "GUEST-UPDATE-BAD-HASH-NO-HANDOFF",
        "GUEST-UPDATE-CANCEL-NO-HANDOFF",
    ],
    "Queue power actions": [
        "GUEST-QUEUE-QUIT",
        "GUEST-QUEUE-LOCK",
        "GUEST-QUEUE-SHUTDOWN",
        "GUEST-QUEUE-SLEEP",
        "GUEST-QUEUE-HIBERNATE",
    ],
    "Battery hardware": ["HOST-PHYSICAL-BATTERY"],
    "Torrent networking": [
        "TORRENT-PUBLIC-DHT",
        "ROUTER-UPNP-DISCOVERY",
        "ROUTER-NATPMP-DISCOVERY",
        "ROUTER-EXTERNAL-INBOUND-MAPPING",
    ],
    "Live reputation services": [
        "EXTERNAL-VIRUSTOTAL-REAL-ERROR",
        "EXTERNAL-SAFEBROWSING-REAL-ERROR",
        "EXTERNAL-VIRUSTOTAL-VALID-KEY",
        "EXTERNAL-SAFEBROWSING-VALID-KEY",
    ],
    "Sound": ["WINDOWS-SOUND-LOOPBACK", "WINDOWS-HUMAN-AUDIBILITY"],
    "Visible notifications": [
        "WINDOWS-NATIVE-NOTIFICATION",
        "WINDOWS-NATIVE-NOTIFICATION-FOREGROUND",
    ],
    "Final package": ["PACKAGE-BUILD", "PACKAGE-NATIVE-TRANSFER"],
}
for area, identifiers in groups.items():
    for identifier in identifiers:
        result = host.get(identifier) or guest.get(identifier)
        assert result is not None, identifier
        location = "host-cases" if identifier in host else "guest-cases"
        rows.append(
            {
                "area": area,
                "id": identifier,
                "result": result["status"],
                "level": result["level"],
                "detail": result["detail"],
                "evidence": "evidence/" + location + "/" + identifier + ".json",
            }
        )
with (out / "coverage.csv").open("w", encoding="utf-8-sig", newline="") as file:
    writer = csv.DictWriter(file, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
(out / "coverage.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
for name, values in (("host-cases", host), ("guest-cases", guest)):
    dest = out / "evidence" / name
    dest.mkdir(parents=True, exist_ok=True)
    for identifier, result in values.items():
        (dest / (identifier + ".json")).write_text(json.dumps(result, indent=2), encoding="utf-8")
(out / "logs").mkdir(exist_ok=True)
for name in (
    "api-malformed-red.log",
    "api-malformed-green.log",
    "crash-config-red.log",
    "regressions-final.log",
    "native-crash-repeat.log",
    "native-crash-mode0.log",
    "native-crash-production.log",
    "network-live-v2.log",
    "audio-live.log",
    "notification-live.log",
    "notification-foreground-v2.log",
    "windows-suite.log",
    "extension-suite.log",
    "types.log",
    "dependencies.log",
    "lint-final.log",
    "format-final.log",
):
    if (ROOT.parent / name).exists():
        shutil.copy2(ROOT.parent / name, out / "logs" / name)
for folder in (ROOT / "sandbox", ROOT / "native-crash", ROOT / "crash-repeat"):
    for path in folder.rglob("*"):
        # No DB, credential store, raw minidump, profile, executable or unrelated media.
        if not path.is_file() or path.suffix.lower() not in (
            ".log",
            ".json",
            ".png",
            ".txt",
            ".wsb",
        ):
            continue
        if "profile" in path.relative_to(folder).parts:
            continue
        dest = out / "evidence" / path.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
for name in (
    "preflight.json",
    "notification-capabilities.json",
    "public-dht-samples.json",
    "packaged-files.json",
    "windows-tests.xml",
    "final-build.log",
):
    if (ROOT / name).exists():
        shutil.copy2(ROOT / name, out / name)
manifest = {
    "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip(),
    "source_status": subprocess.check_output(["git", "status", "--short"], cwd=source, text=True),
    "coverage_totals": dict(Counter(r["result"] for r in rows)),
    "windows_tests": dict(suite.attrib),
    "runtime_source_hashes": {
        str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (source / "app").rglob("*")
        if p.is_file() and p.suffix in (".py", ".json")
    },
}
manifest["windows_passed"] = (
    int(suite.attrib["tests"])
    - int(suite.attrib.get("skipped", 0))
    - int(suite.attrib["failures"])
    - int(suite.attrib["errors"])
)
(out / "run-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
table = "\n".join(f"| {r['area']} | {r['id']} | {r['result']} |" for r in rows)
text = f"""# Remaining GrabLine Windows effects audit

Every requested remaining area has an explicit result in [coverage.csv](coverage.csv).
These results supersede earlier blockers only at the level actually tested.

## Confirmed defects corrected

1. **Windows native crash during traceback dumping.** The new cold-start probe
   reproduced 0xC0000005 at python312.dll RVA 0x287720, matching the original Windows
   fault event. Its crash log was interrupted while dumping another thread during
   imports. The nearby exported symbol is `_Py_DumpTracebackThreads`. Windows now
   logs the faulting thread only; other platforms retain all-thread logging. Three
   candidate runs and three runs through the actual production configuration passed
   with 30 native UI Automation reads per run. This corrects the reproduced path;
   bounded runs do not prove that no other native crash can occur.
2. **VirusTotal malformed responses.** Null/incorrect nested statistics and invalid
   counts could escape as exceptions despite the advisory error contract. They now
   return an unavailable result safely.
3. **Safe Browsing malformed responses.** Non-object JSON and invalid match entries
   could raise exceptions. They now return an unavailable result safely.

The API regressions failed before correction. Actual external calls returned
VirusTotal HTTP 401 and Safe Browsing HTTP 400 for synthetic invalid keys, without
raising. Only a synthetic fixture hash and an example.com test URL were submitted.
The user explicitly chose error checks; valid-key certification remains blocked.

## Windows guest and hardware results

Hyper-V management still denied the current account, but Windows Sandbox ran.
Only test-owned paths were writable in the guest; clipboard, camera and microphone
redirection were disabled. Guest identity guards checked Virtual Machine,
WDAGUtilityAccount and a hostname different from the host before power actions.

The real Inno installer was compiled and tested inside the guest. Installation,
shortcuts/associations, actual HKCU startup registration, six native-host registry
entries/manifests, real host handoff/download checksums, update download/hash/open,
bad-hash rejection, actual Cancel, queue-finished quit/lock/shutdown and uninstall
preserving synthetic downloads have separate evidence. The update handoff used a
benign executable that only writes a marker; it does not certify an upgrade from
every previous release. Browser extension installation in the guest is separate
from direct native-host/registry checks.
Previous-version upgrades, upgrade while running, interactive first-run and
SmartScreen observations from the broader audit remain separate uncompleted
cases; this focused pass does not certify them.

The first guest transfer attempt used a helper sharing the app DB and timed out.
The helper was moved to its own DB; both real transfers then completed with the
correct hashes. That test-setup defect is not counted as an application defect.

Sandbox cannot certify retained login/reboot state because closing it discards
guest state. Its `powercfg /a` reports sleep/hibernate unsupported. This desktop
has no battery sensor: physical battery transitions require suitable hardware.
The earlier persistent-VM administrative prerequisite therefore remains.

Public DHT bootstrap learned a real routing node. This LAN's gateway did not
answer SSDP Internet Gateway Device or NAT-PMP queries. No router settings were
changed. Verified external inbound mappings/cleanup still need a responding
gateway and a dedicated off-LAN peer.

The real completion-sound API produced a synthetic two-tone detected on the
Windows WASAPI output loopback at 48 kHz/stereo. Only energy metrics were retained;
no system audio recording was saved. This verifies the digital output path, not
speaker acoustics or a person's hearing.

Both background and owned-window foreground toast checks completed and hashed
real app downloads, but did not observe a uniquely matching toast. The Windows
shell query returned state 2, `QUNS_BUSY`; Microsoft documents that state as a
fullscreen/presentation notification-blocking mode. No Windows notification/privacy
settings were changed. Visible delivery remains BLOCKED, not an application failure
or a pass. [Microsoft state definitions](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/ne-shellapi-query_user_notification_state).

## Verification and evidence

The complete suite passed **{manifest["windows_passed"]} tests**, with 14 platform skips
and 15 public-network tests excluded. The Deno extension suite passed 42 tests.
Lint, formatting, dependency
checks and CI-target mypy passed. The final package was rebuilt separately and its
GUI/native host completed another checksum-verified transfer. No new one-hour soak
or private-provider/live-site certification is claimed in this focused pass.

The original native-crash reproduction remains a FAIL in its historical evidence;
its current corrected result is separate. Do not sum an expected failing baseline
as an unresolved current feature failure. Raw minidumps and SQLite profiles remain
in the local QA workspace and are excluded from this deliverable.

| Area | Case | Result |
|---|---|---|
{table}

The exact source/build identities, hashes, logs, guest observations and limitations
are recorded alongside this report. No release or tag was published.
The suite and package cover the isolated source revision in run-manifest.json.
Unrelated uncommitted development-checkout edits were preserved and are outside
that certification.
"""
(out / "REMAINING-AUDIT.md").write_text(text, encoding="utf-8")
hashes = {}
for path in out.rglob("*"):
    if path.is_file() and path.name != "SHA256SUMS.json":
        with path.open("rb") as stream:
            hashes[str(path.relative_to(out))] = hashlib.file_digest(stream, "sha256").hexdigest()
(out / "SHA256SUMS.json").write_text(json.dumps(hashes, indent=2), encoding="utf-8")
print(json.dumps({"commit": manifest["commit"], "coverage_totals": manifest["coverage_totals"]}))
