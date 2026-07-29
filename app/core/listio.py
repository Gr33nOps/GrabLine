"""Import and export the download list as a portable JSON file.

Handy for backing up a big queue or moving it to another machine. Exported
items carry everything needed to recreate the download; importing queues them
fresh (they download on the target machine).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.core.models import JobKind
from app.db.database import Database

FORMAT = "grabline-downloads"
VERSION = 1

#: Per-job options that carry secrets and must never be written to an export.
#: http_headers holds the Cookie/Referer/User-Agent a browser handoff passed
#: through - live session cookies. An export is a file the user may share (a
#: backup, a bug report, a move to another machine), so these are stripped
#: (CWE-312). They are session-specific and re-derived by the browser anyway,
#: so a re-imported download simply re-acquires them if it still needs them.
_SECRET_OPTION_KEYS = ("http_headers", "cookie_file")


def _safe_options(options: dict[str, Any] | None) -> dict[str, Any]:
    if not options:
        return {}
    return {k: v for k, v in options.items() if k not in _SECRET_OPTION_KEYS}


def export_jobs(db: Database) -> dict[str, Any]:
    items = [
        {
            "url": job.url,
            "filename": job.filename,
            "dest_dir": job.dest_dir,
            "kind": job.kind.value,
            "title": job.title,
            "options": _safe_options(job.options),
        }
        for job in db.list_jobs()
    ]
    return {"format": FORMAT, "version": VERSION, "items": items}


def write_file(db: Database, path: Path) -> int:
    data = export_jobs(db)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return len(data["items"])


def import_jobs(db: Database, data: dict[str, Any]) -> int:
    """Recreate the downloads described by ``data`` as fresh queued jobs.
    Returns how many were imported."""
    if data.get("format") != FORMAT:
        raise ValueError("this file is not a GrabLine download list")
    imported = 0
    for item in data.get("items", []):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", "")).strip()
        if urlsplit(url).scheme not in ("http", "https"):
            continue
        dest_dir = str(item.get("dest_dir") or "").strip()
        filename = str(item.get("filename") or "").strip()
        if not dest_dir or not filename:
            continue
        try:
            kind = JobKind(item.get("kind", "direct"))
        except ValueError:
            kind = JobKind.DIRECT
        options = item.get("options") if isinstance(item.get("options"), dict) else {}
        db.create_job(
            url,
            dest_dir,
            filename,
            kind=kind,
            title=item.get("title"),
            options=options,
        )
        imported += 1
    return imported


def read_file(db: Database, path: Path) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("this file is not a GrabLine download list")
    return import_jobs(db, data)
