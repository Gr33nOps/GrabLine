"""Evidence ledger for opt-in Windows QA; never touches the normal app profile."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(os.environ.get("GRABLINE_QA_ROOT", Path(__file__).resolve().parents[3] / "run"))
ROOT.mkdir(parents=True, exist_ok=True)


class Blocked(RuntimeError):
    """A positively identified external prerequisite, not an application failure."""


def require_gui_result(result):
    if result.returncode and result.stderr.startswith("SECURITY_DIALOG_BLOCKED:"):
        raise Blocked(result.stderr.strip())
    assert result.returncode == 0, result.stderr


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(name: str, value: object) -> None:
    target = ROOT / name
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")
    temporary.replace(target)


def record(case: str, status: str, level: str, detail: object, **evidence: object) -> None:
    assert status in {"PASS", "FAIL", "BLOCKED", "NOT APPLICABLE"}
    value = {
        "id": case,
        "status": status,
        "level": level,
        "time_utc": datetime.now(UTC).isoformat(),
        "detail": detail,
        **evidence,
    }
    # Keep earlier outcomes when an interrupted/failed case is rerun.
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    previous = ROOT / "cases" / (case + ".json")
    if previous.exists():
        prior = json.loads(previous.read_text(encoding="utf-8"))
        save(f"attempts/{case}/{stamp}-previous.json", prior)
    save(f"cases/{case}.json", value)
    print(json.dumps(value, default=str), flush=True)


@contextlib.contextmanager
def case(identifier: str, level: str):
    started = time.monotonic()
    evidence: dict[str, object] = {}
    try:
        yield evidence
    except Blocked as exc:
        record(identifier, "BLOCKED", level, str(exc), **evidence)
    except Exception as exc:
        record(identifier, "FAIL", level, str(exc), traceback=traceback.format_exc(), **evidence)
    else:
        evidence.setdefault("seconds", round(time.monotonic() - started, 3))
        record(identifier, "PASS", level, "All assertions succeeded", **evidence)


def summary() -> list[dict]:
    return [
        json.loads(p.read_text(encoding="utf-8")) for p in sorted((ROOT / "cases").glob("*.json"))
    ]
