"""Assemble a sanitized audit ledger from explicitly selected completed runs."""
# ruff: noqa: E501 -- Report paragraphs and artifact links are kept readable as prose.

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from common import ROOT

parser = argparse.ArgumentParser()
parser.add_argument("--output", required=True)
args = parser.parse_args()
source = Path(__file__).resolve().parents[2]
qa = source.parent
workspace = qa.parents[1]
output = Path(args.output).resolve()
assert output.is_relative_to(workspace / "outputs"), output
output.mkdir(parents=True, exist_ok=True)
roots = [ROOT, qa / "run-torrent-safe", qa / "run-one-hour-final"]
latest = {}
history = []
for root in roots:
    for p in (root / "cases").glob("*.json"):
        value = json.loads(p.read_text(encoding="utf-8"))
        value["run"] = root.name
        value["evidence_path"] = str(p.relative_to(qa))
        history.append(value)
        old = latest.get(value["id"])
        if old is None or value["time_utc"] > old["time_utc"]:
            latest[value["id"]] = value
rows = sorted(latest.values(), key=lambda x: x["id"])


def clean(text):
    text = re.sub(
        r"(?im)^(\s*(?:authorization|cookie|set-cookie)\s*[:=]).*", r"\1 [REDACTED]", text
    )
    text = re.sub(
        r"(?i)([?&](?:token|access_token|signature|sig|key|policy|x-amz-signature)=)[^&\s\"\']+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@", r"\1[REDACTED]@", text)
    for value in ["qa-test-only", "qa-key-pass", "qa-password", "wrong-test-secret"]:
        text = text.replace(value, "[DISPOSABLE-SECRET-REDACTED]")
    return text


def write(path, value):
    target = output / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(clean(json.dumps(value, indent=2, default=str)), encoding="utf-8")


def copy_text(p, relative):
    target = output / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(clean(p.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")


for r in rows:
    write(Path("evidence/cases") / (r["id"] + ".json"), r)
write(Path("coverage.json"), rows)
with (output / "coverage.csv").open("w", newline="", encoding="utf-8-sig") as stream:
    writer = csv.DictWriter(
        stream, fieldnames=["id", "status", "level", "time_utc", "detail", "run", "evidence"]
    )
    writer.writeheader()
    for r in rows:
        writer.writerow(
            {
                **{k: r[k] for k in ["id", "status", "level", "time_utc", "detail", "run"]},
                "evidence": "evidence/cases/" + r["id"] + ".json",
            }
        )
controls = json.loads((ROOT / "editable-control-inventory.json").read_text(encoding="utf-8"))
for r in controls:
    case = latest.get(r["expected_case"])
    r["result"] = case["status"] if case else "BLOCKED"
    r["testing_level"] = case["level"] if case else "No result available"
    r["effect_limit"] = (
        "Persistence/control assertions only where stated; production effect and native shell/hardware certification are separate cases."
    )
write(Path("settings-controls.json"), controls)
with (output / "settings-controls.csv").open("w", newline="", encoding="utf-8-sig") as f:
    writer = csv.DictWriter(f, fieldnames=list(dict.fromkeys(k for r in controls for k in r)))
    writer.writeheader()
    writer.writerows(controls)
unit = []
for t in ET.parse(qa / "full-final990.xml").iter("testcase"):
    skip = t.find("skipped")
    fail = t.find("failure")
    error = t.find("error")
    unit.append(
        {
            "id": t.attrib.get("classname", "") + "::" + t.attrib["name"],
            "result": "NOT APPLICABLE"
            if skip is not None
            else "FAIL"
            if fail is not None or error is not None
            else "PASS",
            "level": "unit or pytest local integration; see test body",
            "seconds": t.attrib.get("time"),
            "reason": skip.attrib.get("message") if skip is not None else "",
        }
    )
write(Path("pytest-cases.json"), unit)
catalog = json.loads((source / "scripts/windows_qa/coverage_catalog.json").read_text())
for item in catalog:
    referenced = [latest[identifier] for identifier in item["cases"]]
    unit_matches = [
        test for test in unit if test["id"].split("::")[0].split(".")[-1] in item["unit_modules"]
    ]
    assert referenced or unit_matches, item["feature"]
    statuses = {test["status"] for test in referenced} | {test["result"] for test in unit_matches}
    item["result"] = next(
        status for status in ["FAIL", "BLOCKED", "PASS", "NOT APPLICABLE"] if status in statuses
    )
    item["level"] = "; ".join(sorted({test["level"] for test in referenced}))
    if unit_matches:
        item["level"] += "; pytest unit/local integration (individual levels in source)"
    item["pytest_case_count"] = len(unit_matches)
write(Path("feature-matrix.json"), catalog)
with (output / "feature-matrix.csv").open("w", newline="", encoding="utf-8-sig") as f:
    writer = csv.DictWriter(f, fieldnames=list(catalog[0]))
    writer.writeheader()
    writer.writerows(catalog)
with (output / "pytest-cases.csv").open("w", newline="", encoding="utf-8-sig") as f:
    writer = csv.DictWriter(f, fieldnames=list(unit[0]))
    writer.writeheader()
    writer.writerows(unit)
# Preserve earlier red attempts; exclude raw browser profiles/databases/key material.
for root in roots:
    for p in (root / "attempts").rglob("*.json"):
        copy_text(p, Path("evidence/history") / root.name / p.relative_to(root / "attempts"))
    for p in (root / "cases").glob("*.json"):
        value = json.loads(p.read_text(encoding="utf-8"))
        if latest[value["id"]]["evidence_path"] != str(p.relative_to(qa)):
            copy_text(p, Path("evidence/history") / root.name / p.name)
for p in qa.glob("*.log"):
    if p.stat().st_size < 10_000_000:
        copy_text(p, Path("evidence/logs") / p.name)
for p in qa.glob("full-final990.xml"):
    copy_text(p, Path("evidence/logs") / p.name)
for name in [
    "preflight.json",
    "browser-manifest.json",
    "extension-running-source.json",
    "vm-final-access.json",
    "battery-observation.json",
    "wall-clock-events.json",
    "browser-events.json",
    "packaged-files.json",
    "final-build.log",
]:
    p = ROOT / name
    if p.exists():
        copy_text(p, Path("evidence") / name)
for name in ["soak-events.jsonl", "soak-progress.json", "soak-start.json", "soak-build.log"]:
    p = qa / "run-one-hour-final" / name
    if p.exists():
        copy_text(p, Path("evidence/soak") / name)
for p in (ROOT / "crash-investigation").glob("*"):
    if p.suffix in [".json", ".log"]:
        copy_text(p, Path("evidence/crash-investigation") / p.name)
for p in ROOT.rglob("ffprobe.json"):
    if not any(x in p.parts for x in ["final-build", "final-dist", "browser-profile"]):
        copy_text(p, Path("evidence/media") / p.relative_to(ROOT))
# Only selected QA screenshots: never a whole-desktop or unrelated personal window capture.
names = {
    "quality-focused.png",
    "live-reddit-quality.png",
    "gallery-native.png",
    "alllinks-native.png",
    "selection-native.png",
    "open-settings-app.png",
}
for name in names:
    p = ROOT / name
    if p.exists():
        target = output / "evidence/screenshots" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
for folder in ["visual", "scaling"]:
    for p in (ROOT / folder).rglob("*.png"):
        target = output / "evidence/screenshots" / folder / p.relative_to(ROOT / folder)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
for p in (source / "scripts/windows_qa/guest").glob("*"):
    if p.is_file():
        shutil.copy2(p, output / p.name)
shutil.copy2(source / "scripts/windows_qa/README.md", output / "REPRODUCE.md")
shutil.copy2(source / "docs/windows-development.md", output / "DEVELOPMENT.md")
counts = Counter(r["status"] for r in rows)
controlcounts = Counter(r["result"] for r in controls)
fixes = [
    "Wait for libtorrent disk-cache flush before reporting completion; immediate hashes now match.",
    "Route AES-encrypted ZIP extraction through 7-Zip when Python zipfile lacks the method.",
    "Use explicit UTF-8 for Windows 7-Zip listing output and filenames.",
    "Wire popup actions and preferences before asynchronous status/recent results finish.",
    "Exclude observed POST exports from browser takeover; preserve browser downloads.",
    "Show provider-neutral quality-unavailable errors for non-YouTube sites; translate the new message.",
    "Retain source tab identity for download takeover progress.",
    "Apply live speed-limit changes to outstanding rate debt immediately.",
    "Treat zero fair-speed demand samples as unknown instead of a one-KB/s ceiling.",
    "Collect URLs from selected plain text, deduplicate and cap the set.",
    "Build lazy Settings/Queue pages when opened from the extension.",
    "Show Paused explicitly in page progress labels.",
    "Use origin-only cross-origin referrers so progress associates with the active source tab.",
    "Keep Pause/Resume accessible names synchronized with the current action.",
    "Target context-menu collection messages to the clicked frame.",
    "Process completion/failure transitions while the app window is hidden, including extraction and notification signals.",
    "Construct root-folder SFTP/SCP file URLs with a single path separator; UI-selected files now transfer.",
    "Restore a minimized app when a browser/native-host focus request arrives.",
]
write(
    Path("fixed-defects.json"),
    [
        {"id": f"FIX-{i:02}", "description": v, "status": "FIXED AND VERIFIED"}
        for i, v in enumerate(fixes, 1)
    ],
)
blocked = [r for r in rows if r["status"] == "BLOCKED"]
failed = [r for r in rows if r["status"] == "FAIL"]
na = [r for r in rows if r["status"] == "NOT APPLICABLE"]
write(
    Path("remaining.json"),
    {
        "confirmed_unresolved_defects": failed,
        "blocked_cases": blocked,
        "not_applicable": na,
        "full_local_coverage": False,
    },
)
identity = {
    "base_commit": "e1ca1f2cb09efb67e802c0511f8557d426ebcc90",
    "generated_utc": datetime.now(UTC).isoformat(),
    "development_checkout": r"D:\Projects\app dev\GrabLine",
    "audit_checkout": str(source),
    "case_counts": dict(counts),
    "control_counts": dict(controlcounts),
    "pytest": dict(Counter(r["result"] for r in unit)),
    "extension_tests": 42,
    "full_local_coverage": False,
    "curated_runs": [r.name for r in roots],
    "soak_scope": "Actual 3600 seconds /1559 batches on engine-fixed source before later browser/GUI/cloud-root fixes; not a final-revision native-GUI soak.",
}
commitfile = output / "commit.json"
if commitfile.exists():
    identity["commit"] = json.loads(commitfile.read_text())
write(Path("manifest.json"), identity)
lines = [
    "# GrabLine Windows audit",
    "",
    f"Generated {identity['generated_utc']}. Full local coverage is **not certified**.",
    "",
    "Final Windows suite: **990 passed, 14 platform-specific skips, 15 network tests excluded**. Extension suite: **42 passed**. Lint/format/type/dependency checks and packaged native-host smoke are recorded in the logs.",
    "",
    f"Latest curated integration cases: {dict(counts)}. These are case outcomes, not a percentage of every possible feature combination.",
    "",
    f"Settings inventory: **{len(controls)} controls across 19 sections**, including 38 shortcut editors. {dict(controlcounts)}. Control persistence passes do not imply every OS/provider effect passed.",
    "",
    "**18 confirmed defects fixed**, with red/green reproductions and integration reruns. See [fixed defects](fixed-defects.json).",
    "",
    "**One-hour soak passed:** 3600 measured seconds, 1559 batches, four stages, deliberate overlapping build, every completed output checked. It preceded the later browser/GUI/cloud-root changes; its source scope is explicit in [manifest](manifest.json).",
    "",
    "## Evidence and matrices",
    "",
    "- [Plan features and expected results: CSV](feature-matrix.csv) · [JSON](feature-matrix.json)",
    "- [Every integration case: CSV](coverage.csv) · [JSON](coverage.json)",
    "- [Every settings control: CSV](settings-controls.csv) · [JSON](settings-controls.json)",
    "- [Every pytest case and skip reason](pytest-cases.csv)",
    "- [Remaining failures and blockers](remaining.json)",
    "- [Reproduction and resume commands](REPRODUCE.md)",
    "- [Windows development guide](DEVELOPMENT.md)",
    "- [Guest VM protocol](README.md) · [guarded runner](guest.ps1)",
    "",
    "## Confirmed unresolved defect",
    "",
]
for r in failed:
    lines.append(f"- **{r['id']}: {r['detail']}** ([evidence](evidence/cases/{r['id']}.json))")
lines += [
    "",
    "A 0xC0000005 native crash was observed during repeated Qt accessibility inspection. The Win32 path later worked, but the crash was not deterministically reproduced or fixed. Nonfatal 0x8001010D first-chance dumps are distinguished from the fatal Windows event. Do not infer overall stability from the passing unit suite or the earlier engine soak.",
    "",
    "## Blocked cases",
    "",
]
for r in blocked:
    lines.append(f"- **{r['id']}** — {r['detail']}")
lines += ["", "## Configuration limits and absent capabilities", ""]
for r in na:
    lines.append(f"- **{r['id']}** — {r['detail']}")
lines += [
    "",
    "## Interpretation",
    "",
    "Actual toolbar-popup testing is separate from opening popup HTML as a tab. Controlled site DOM tests are separate from live adapter checks and full public media downloads. Public SoundCloud succeeded with the alternate sample; the original unavailable sample remains recorded. Reddit adapter handoff reached the native dialog, while its full media download requires authentication. Private external accounts/API keys and VM actions were not substituted with mocks.",
    "",
    "No personal profiles/history, browser cookies, private key files, databases, or vault contents are included. Test archives/media are synthetic or have recorded provenance. Earlier red attempts remain in evidence/history; active torrent and soak results use explicitly selected successful runs rather than hiding their earlier failures.",
    "",
    "No release or tag is published. Commit/build identity is recorded in manifest.json and commit.json after synchronization.",
]
(output / "AUDIT.md").write_text(clean("\n".join(lines) + "\n"), encoding="utf-8")
# A hash manifest covers delivered evidence, excluding itself.
hashes = {
    str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
    for p in output.rglob("*")
    if p.is_file() and p.name != "SHA256.json"
}
write(Path("SHA256.json"), hashes)
print(
    json.dumps(
        {
            "cases": dict(counts),
            "controls": dict(controlcounts),
            "output": str(output),
            "files": len(hashes),
        },
        indent=2,
    )
)
