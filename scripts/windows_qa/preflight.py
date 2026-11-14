"""Collect capabilities without changing browser pairing or Windows settings."""

from __future__ import annotations

import importlib.metadata
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import psutil
from common import ROOT, record, save

import app

source = Path(__file__).resolve().parents[2]
assert Path(app.__file__).resolve().is_relative_to(source), app.__file__
tools = {name: shutil.which(name) for name in ("ffmpeg", "ffprobe", "deno", "7z", "git")}
report = {
    "source": str(source),
    "commit": subprocess.check_output(
        ["git", "-c", f"safe.directory={source.as_posix()}", "rev-parse", "HEAD"],
        cwd=source,
        text=True,
    ).strip(),
    "python": sys.version,
    "platform": platform.platform(),
    "tools": tools,
    "disk": shutil.disk_usage(ROOT)._asdict(),
    "versions": {
        name: importlib.metadata.version(name)
        for name in ("PySide6", "libtorrent", "yt-dlp", "httpx", "paramiko", "boto3")
    },
}
try:
    report["memory"] = psutil.virtual_memory()._asdict()
except Exception as exc:
    report["memory_error"] = str(exc)
save("preflight.json", report)
for name, path in tools.items():
    record(
        "ENV-" + name.upper(),
        "PASS" if path else "BLOCKED",
        "environment",
        path or "Executable unavailable",
    )
vm = subprocess.run(
    [
        "powershell.exe",
        "-NoProfile",
        "-Command",
        "Get-VM -ErrorAction Stop | Select-Object Name,State | ConvertTo-Json",
    ],
    capture_output=True,
    text=True,
    timeout=20,
)
save("vm-preflight.json", {"exit_code": vm.returncode, "stdout": vm.stdout, "stderr": vm.stderr})
record(
    "VM-ACCESS",
    "PASS" if vm.returncode == 0 else "BLOCKED",
    "environment",
    "VM inventory available"
    if vm.returncode == 0
    else (
        "Hyper-V management denied to this Windows account; "
        "administrator-provisioned disposable guest required"
    ),
    exit_code=vm.returncode,
)
print(json.dumps(report, indent=2))
