"""Exactly-once memory for phone requests (PROTOCOL.md 12, `download`; QA-14).

``(deviceId, requestId) -> handoffId`` is kept for 7 days and at most 10,000
rows per device, so a phone retrying the same share after a lost reply gets
the first handoff back instead of a second download. Which phone created which
job is kept too, so a job object can carry the creating device's requestId.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.db.database import Database

KEEP_SECONDS = 7 * 24 * 3600
MAX_PER_DEVICE = 10_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS remote_requests (
    device_id  TEXT NOT NULL,
    request_id TEXT NOT NULL,
    handoff_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (device_id, request_id)
);
CREATE INDEX IF NOT EXISTS idx_remote_requests_age ON remote_requests(device_id, created_at);
CREATE TABLE IF NOT EXISTS remote_request_jobs (
    job_id     INTEGER PRIMARY KEY,
    device_id  TEXT NOT NULL,
    request_id TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
"""


class RequestLog:
    def __init__(self, db: Database, *, clock: Callable[[], float] = time.time) -> None:
        self._db = db
        self._clock = clock
        with db._lock, db._conn:
            db._conn.executescript(_SCHEMA)

    def lookup(self, device_id: str, request_id: str) -> int | None:
        cutoff = int(self._clock()) - KEEP_SECONDS
        with self._db._lock:
            row = self._db._conn.execute(
                "SELECT handoff_id FROM remote_requests "
                "WHERE device_id = ? AND request_id = ? AND created_at >= ?",
                (device_id, request_id, cutoff),
            ).fetchone()
        return int(row[0]) if row else None

    def remember(self, device_id: str, request_id: str, handoff_id: int) -> None:
        now = int(self._clock())
        with self._db._lock, self._db._conn:
            conn = self._db._conn
            conn.execute(
                "INSERT OR IGNORE INTO remote_requests VALUES (?, ?, ?, ?)",
                (device_id, request_id, handoff_id, now),
            )
            conn.execute("DELETE FROM remote_requests WHERE created_at < ?", (now - KEEP_SECONDS,))
            conn.execute(
                "DELETE FROM remote_requests WHERE device_id = ? AND rowid NOT IN ("
                "SELECT rowid FROM remote_requests WHERE device_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?)",
                (device_id, device_id, MAX_PER_DEVICE),
            )

    def record_jobs(self, device_id: str, request_id: str, job_ids: Iterable[int]) -> None:
        now = int(self._clock())
        with self._db._lock, self._db._conn:
            self._db._conn.executemany(
                "INSERT OR REPLACE INTO remote_request_jobs VALUES (?, ?, ?, ?)",
                [(job_id, device_id, request_id, now) for job_id in job_ids],
            )

    def request_for_job(self, job_id: int, device_id: str | None) -> str | None:
        if device_id is None:
            return None
        with self._db._lock:
            row = self._db._conn.execute(
                "SELECT request_id FROM remote_request_jobs WHERE job_id = ? AND device_id = ?",
                (job_id, device_id),
            ).fetchone()
        return str(row[0]) if row else None

    def forget_device(self, device_id: str) -> None:
        with self._db._lock, self._db._conn:
            self._db._conn.execute("DELETE FROM remote_requests WHERE device_id = ?", (device_id,))
            self._db._conn.execute(
                "DELETE FROM remote_request_jobs WHERE device_id = ?", (device_id,)
            )
