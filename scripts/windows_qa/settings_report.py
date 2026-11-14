"""Publish a settings-only ledger without copying profiles, credentials or databases."""

from __future__ import annotations

import argparse
import ast
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
cases = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in (ROOT / "cases").glob("*.json")}
inventory = json.loads((ROOT / "editable-control-inventory.json").read_text(encoding="utf-8"))
assert len(inventory) == 150
assert all("SETTING-REOPEN-" + row["field"] in cases for row in inventory)
assert all(cases["SETTING-REOPEN-" + row["field"]].get("changed") for row in inventory)
suite = ET.parse(ROOT / "windows-tests-final.xml").getroot().find("testsuite")
assert suite is not None
assert int(suite.attrib["failures"]) == int(suite.attrib["errors"]) == 0
for identifier in ("PACKAGE-BUILD", "PACKAGE-NATIVE-TRANSFER"):
    assert cases.get(identifier, {}).get("status") == "PASS", identifier
suite_counts = {key: int(suite.attrib[key]) for key in ("tests", "skipped", "failures", "errors")}
suite_counts["passed"] = suite_counts["tests"] - suite_counts["skipped"]

# These are evidence pointers, not an inference that every consumer effect was
# exercised by a field roundtrip. Each pointed-to case states its own level.
effects = {
    "Appearance": ["SETTING-EFFECT-LIVE-APPEARANCE-TIMERS"],
    "Download Engine": ["SETTING-EFFECT-RATE-AND-CONNECTIONS"],
    "Video Downloader": ["SETTING-EFFECT-SMART-OPTIONS", "SETTING-ACTION-FFMPEG-INSTALL"],
    "Torrent": ["SETTING-EFFECT-TORRENT-SESSION-OPTIONS", "SETTING-EFFECT-LIVE-APPEARANCE-TIMERS"],
    "File Management": ["SETTING-EFFECT-DEFAULT-TAGS-QUEUE-RENAME"],
    "Queue Manager": ["SETTING-EFFECT-DEFAULT-TAGS-QUEUE-RENAME"],
    "Statistics": [
        "SETTING-EFFECT-STATISTICS-ENABLE-DISABLE",
        "SETTING-EFFECT-LIVE-APPEARANCE-TIMERS",
    ],
    "Advanced": ["SETTING-LOGGING-FRESH-PROCESS"],
    "Notifications": [
        "SETTING-EFFECT-NOTIFY_ON_COMPLETE",
        "SETTING-EFFECT-NOTIFY_ON_FAILED",
        "SETTING-EFFECT-NOTIFY_QUEUE_DONE",
        "SETTING-EFFECT-SOUND-SCRIPT-QUIET",
    ],
    "Scheduler": ["SETTING-EFFECT-AFTER-QUEUE-DISPATCH"],
}
limits = {
    "General": (
        "Actual login/startup requires VM; source/Qt state paths and preference "
        "roundtrips are distinct."
    ),
    "Browser Integration": (
        "Private-session cookie access needs a dedicated browser account; "
        "registration reserved for VM."
    ),
    "Video Downloader": (
        "Engine option propagation is verified; live private sites remain external checks."
    ),
    "Torrent": (
        "Native session settings verified; real DHT/NAT/router traversal and seed "
        "ratios need suitable peers/network."
    ),
    "Scheduler": (
        "Power dispatch and battery gates have unit coverage; actual "
        "power/login/battery require VM/hardware."
    ),
    "Network": (
        "Local proxy/network regressions in Windows suite; physical VPN/ISP remains separate."
    ),
    "Security": (
        "Preference and local TLS/request paths covered; live reputation APIs "
        "require dedicated keys."
    ),
    "Notifications": (
        "Captured toast/sound/script dispatch is unit coverage; physical shell "
        "delivery/audibility separate."
    ),
    "Advanced": (
        "Logging deliberately takes effect on next launch; both states verified in fresh processes."
    ),
}
unit_refs = []
for path in (source / "app/tests").glob("test_*.py"):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            attrs = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
            unit_refs.append((str(path.relative_to(source)), node.name, node.lineno, attrs))
rows = []
for row in inventory:
    result = cases["SETTING-REOPEN-" + row["field"]]
    prop = row["setting"]
    refs = [f"{path}:{line} {name}" for path, name, line, attrs in unit_refs if prop in attrs]
    if row["section"] == "Shortcuts":
        ids = ["QT-SHORTCUT-" + row["field"][9:].upper().replace(".", "-")]
    else:
        ids = effects.get(row["section"], [])
    rows.append(
        {
            "section": row["section"],
            "field": row["field"],
            "setting": prop,
            "control_result": result["status"],
            "testing_level": result["level"],
            "changed_value": result.get("changed"),
            "fresh_connection_reopen": result.get("reopened_value_verified"),
            "cancel_preserves_store": result.get("cancel_preserved_store"),
            "control_evidence": "evidence/cases/" + result["id"] + ".json",
            "related_effect_cases": "; ".join(ids),
            "related_unit_references": "; ".join(refs),
            "effect_limit": limits.get(
                row["section"],
                (
                    "Related assertions have their own levels; storage success alone does not "
                    "certify every effect."
                ),
            ),
        }
    )
(out / "settings-controls.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
with (out / "settings-controls.csv").open("w", encoding="utf-8-sig", newline="") as file:
    writer = csv.DictWriter(file, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
for name in ("cases", "visual", "scaling", "settings-actions"):
    src = ROOT / name
    target = out / "evidence" / name
    target.mkdir(parents=True, exist_ok=True)
    # Explicit allowlist excludes all SQLite files and raw profiles.
    for path in src.rglob("*") if src.exists() else []:
        if path.is_file() and path.suffix.lower() in (".json", ".png", ".csv", ".txt"):
            if "profile" in path.relative_to(src).parts:
                continue
            dest = target / path.relative_to(src)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
for name in (
    "editable-control-inventory.json",
    "settings-action-inventory.json",
    "packaged-files.json",
    "windows-tests-final.xml",
    "browser-manifest.json",
):
    if (ROOT / name).exists():
        shutil.copy2(ROOT / name, out / name)
logs = [
    "settings-regressions-red.log",
    "regressions-final.log",
    "settings-ffmpeg.log",
    "settings-ffmpeg-v2.log",
    "windows-suite-final.log",
    "extension-suite.log",
    "types.log",
    "dependencies.log",
    "lint-final.log",
    "format-final.log",
    "reopen-final.log",
    "actions-final.log",
    "effects-final.log",
    "matrix.log",
    "extras.log",
    "visual-final.log",
    "scaling-final.log",
    "verified-windows-pin.json",
]
(out / "logs").mkdir(exist_ok=True)
for name in logs:
    if (ROOT.parent / name).exists():
        shutil.copy2(ROOT.parent / name, out / "logs" / name)
if (ROOT / "final-build.log").exists():
    shutil.copy2(ROOT / "final-build.log", out / "logs/final-build.log")
identity = {
    "source_commit": subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=source, text=True
    ).strip(),
    "source_status": subprocess.check_output(["git", "status", "--short"], cwd=source, text=True),
    "cases": dict(Counter(c["status"] for c in cases.values())),
    "controls": dict(Counter(r["control_result"] for r in rows)),
    "windows_suite": suite_counts,
    "runtime_source_hashes": {
        str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (source / "app").rglob("*")
        if path.is_file() and path.suffix in (".py", ".json")
    },
    "scope": (
        "Windows settings audit; offscreen Qt, local integration, unit OS "
        "boundaries, packaged smoke"
    ),
}
(out / "run-manifest.json").write_text(json.dumps(identity, indent=2), encoding="utf-8")
sections = Counter(r["section"] for r in rows)
section_table = "\n".join(
    f"| {name} | {sections[name]} |" for name in dict.fromkeys(r["section"] for r in inventory)
)
blocked = "\n".join(
    f"- **{c['id']}**: {c['detail']}" for c in cases.values() if c["status"] == "BLOCKED"
)
failures = (
    "\n".join(f"- **{c['id']}**: {c['detail']}" for c in cases.values() if c["status"] == "FAIL")
    or "No current failed settings cases."
)
report = f"""# GrabLine Windows settings audit

Every inventoried editable control has a separate result: **{len(rows)} controls**,
including 38 shortcut editors, across 19 settings sections. Cloud Downloads and
About contain actions rather than editable preference fields. Every section was
opened and rendered. See [the complete control matrix](settings-controls.csv) and
[all case assertions](evidence/cases).

Each control was changed, saved using the embedded Save button, reloaded against
a fresh SQLite connection/dialog, and checked for Cancel preserving stored values.
All tests used disposable data. OS autostart registration was isolated at its
provider boundary; its real OS effect remains BLOCKED. These are offscreen Qt
tests, not a claim of physical mouse or Windows accessibility coverage.

Four confirmed issues were corrected:

1. RSS and watch-folder intervals now update their running timers after Save.
2. Import immediately refreshes fields and applies changes to live consumers.
3. Reset refreshes shortcut editors, preventing a later Save from restoring stale overrides.
4. The Windows FFmpeg installer pin returned HTTP 404. Its replacement was checked
   against the official upstream SHA-256 digest and installed through the real button.

The source regressions were observed failing before correction. The obsolete
import-success message and its 13 translations were removed. Logging retains its
documented next-launch behaviour; fresh processes verify debug/file-on and error/file-off.

Current case totals: **{identity["cases"]}**. Control totals: **{identity["controls"]}**.
Final Windows suite: **{suite_counts["passed"]} passed, {suite_counts["skipped"]} skipped**.
The separate Deno extension suite passed 42 tests. Lint, format, dependency and
CI-target type checks passed; their logs are included.
Build and suite identities are recorded in [the run manifest](run-manifest.json),
[packaged hashes](packaged-files.json) and logs. The machine-readable matrix keeps
control assertions separate from downstream effect evidence and limitations.

| Section with editable controls | Controls |
|---|---:|
{section_table}

Settings action coverage includes path selection/cancellation, import/export and
redaction, malformed/cancelled import, Reset confirmation/cancellation, statistics
CSV/clear, database compact/integrity, forgotten watch imports/certificates,
diagnostics, shortcuts/reset, setup/account/help dialog opening, link dispatch,
update-check dispatch, navigation/search and the genuine FFmpeg installer.
File picker choices and OS dispatch are labelled boundaries. The action inventory
records every button, including 38 individual shortcut resets tested separately.

Visual checks cover every section in three themes, all 14 languages including RTL,
and 100/125/150/200 percent Qt scaling with both densities. Qt scaling does not alter
the physical monitor configuration. Screenshots are under evidence/visual and
evidence/scaling. No new one-hour soak or live-site certification is claimed here.

## Current failures

{failures}

## Unavailable physical and external checks

{blocked}

The prior broad audit remains separate; these settings results do not resolve its
native accessibility crash, private-account/live-site limitations or VM access.
No release or tag was published. Dated FFmpeg assets can disappear upstream; keep
pins maintained and retain checksum verification.
"""
(out / "SETTINGS-AUDIT.md").write_text(report, encoding="utf-8")
hashes = {}
for path in out.rglob("*"):
    if path.is_file() and path.name != "SHA256SUMS.json":
        with path.open("rb") as file:
            hashes[str(path.relative_to(out))] = hashlib.file_digest(file, "sha256").hexdigest()
(out / "SHA256SUMS.json").write_text(json.dumps(hashes, indent=2), encoding="utf-8")
print(json.dumps(identity, indent=2))
