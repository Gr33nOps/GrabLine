"""SQLite persistence for jobs and segment checkpoints.

WAL journaling keeps the database consistent across a kill -9 or power-style
interruption of the process, which is what makes resume-after-crash (F0.2)
trustworthy. A single connection is shared behind a lock so worker threads,
the checkpointer, and the UI thread can all talk to one Database instance.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.core.models import (
    RESUMABLE_STATUSES,
    Handoff,
    Job,
    JobKind,
    JobStatus,
    Queue,
    Segment,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url           TEXT NOT NULL,
    final_url     TEXT,
    dest_dir      TEXT NOT NULL,
    filename      TEXT NOT NULL,
    total_size    INTEGER,
    resumable     INTEGER NOT NULL DEFAULT 0,
    etag          TEXT,
    last_modified TEXT,
    status        TEXT NOT NULL DEFAULT 'queued',
    error         TEXT,
    kind          TEXT NOT NULL DEFAULT 'direct',
    title         TEXT,
    options       TEXT NOT NULL DEFAULT '{}',
    downloaded    INTEGER NOT NULL DEFAULT 0,
    priority      INTEGER NOT NULL DEFAULT 0,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS segments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    seg_index  INTEGER NOT NULL,
    start_byte INTEGER NOT NULL,
    end_byte   INTEGER,
    downloaded INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_segments_job_id ON segments(job_id);

-- The extension's progress pill looks a download up by URL on every poll
-- (once a second, per tracked URL). Without this the lookup is a full scan of
-- every job the user has ever had.
CREATE INDEX IF NOT EXISTS idx_jobs_url ON jobs(url);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Successfully downloaded bytes, bucketed by day + category + host, for the
-- live dashboard (today/week/month/lifetime, per-server, per-category).
CREATE TABLE IF NOT EXISTS download_stats (
    day      TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    host     TEXT NOT NULL DEFAULT '',
    bytes    INTEGER NOT NULL DEFAULT 0,
    files    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, category, host)
);

-- Named download queues / groups. Jobs reference them via jobs.queue_id;
-- a NULL queue_id means the default queue (global rules only).
CREATE TABLE IF NOT EXISTS queues (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    position         INTEGER NOT NULL DEFAULT 0,
    max_concurrent   INTEGER NOT NULL DEFAULT 0,
    paused           INTEGER NOT NULL DEFAULT 0,
    schedule_enabled INTEGER NOT NULL DEFAULT 0,
    start_time       TEXT NOT NULL DEFAULT '00:00',
    stop_time        TEXT NOT NULL DEFAULT '00:00',
    depends_on       INTEGER,
    category         TEXT NOT NULL DEFAULT ''
);

-- URLs handed over by GrabLine Connect (via the Native Messaging host).
-- The host inserts; the running app claims and runs them through the
-- resolver. This table IS the extension->app channel: no sockets exist.
CREATE TABLE IF NOT EXISTS handoffs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    url        TEXT NOT NULL,
    page_url   TEXT,
    page_title TEXT,
    source     TEXT NOT NULL DEFAULT 'extension',
    payload    TEXT NOT NULL DEFAULT '[]',
    quality    TEXT,
    headers    TEXT NOT NULL DEFAULT '{}',
    claimed    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The running app polls for unclaimed rows on a timer, forever, while claimed
-- ones accumulate for the life of the profile.
CREATE INDEX IF NOT EXISTS idx_handoffs_unclaimed ON handoffs(claimed, id);
"""

#: Columns added after Phase 0, applied to pre-existing databases on open.
_JOBS_MIGRATIONS = {
    "kind": "ALTER TABLE jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'direct'",
    "title": "ALTER TABLE jobs ADD COLUMN title TEXT",
    "options": "ALTER TABLE jobs ADD COLUMN options TEXT NOT NULL DEFAULT '{}'",
    "downloaded": "ALTER TABLE jobs ADD COLUMN downloaded INTEGER NOT NULL DEFAULT 0",
    "priority": "ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0",
    "retry_count": "ALTER TABLE jobs ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0",
    "queue_id": "ALTER TABLE jobs ADD COLUMN queue_id INTEGER",
}

_HANDOFFS_MIGRATIONS = {
    "payload": "ALTER TABLE handoffs ADD COLUMN payload TEXT NOT NULL DEFAULT '[]'",
    "quality": "ALTER TABLE handoffs ADD COLUMN quality TEXT",
    "headers": "ALTER TABLE handoffs ADD COLUMN headers TEXT NOT NULL DEFAULT '{}'",
}


def _job_from_row(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        url=row["url"],
        final_url=row["final_url"],
        dest_dir=row["dest_dir"],
        filename=row["filename"],
        total_size=row["total_size"],
        resumable=bool(row["resumable"]),
        etag=row["etag"],
        last_modified=row["last_modified"],
        status=JobStatus(row["status"]),
        error=row["error"],
        kind=JobKind(row["kind"]),
        title=row["title"],
        options=json.loads(row["options"] or "{}"),
        downloaded=row["downloaded"],
        priority=row["priority"],
        retry_count=row["retry_count"],
        queue_id=row["queue_id"],
    )


def _queue_from_row(row: sqlite3.Row) -> Queue:
    return Queue(
        id=row["id"],
        name=row["name"],
        position=row["position"],
        max_concurrent=row["max_concurrent"],
        paused=bool(row["paused"]),
        schedule_enabled=bool(row["schedule_enabled"]),
        start_time=row["start_time"],
        stop_time=row["stop_time"],
        depends_on=row["depends_on"],
        category=row["category"],
    )


def _segment_from_row(row: sqlite3.Row) -> Segment:
    return Segment(
        id=row["id"],
        job_id=row["job_id"],
        index=row["seg_index"],
        start=row["start_byte"],
        end=row["end_byte"],
        downloaded=row["downloaded"],
    )


class Database:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        # The DB holds API keys and browser-session cookies. Lock the file to
        # the owner on POSIX (defense in depth behind the 0700 data dir);
        # best-effort, and a no-op on Windows where the profile is ACL'd.
        if os.name == "posix":
            with contextlib.suppress(OSError):
                os.chmod(self._path, 0o600)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after Phase 0 to databases created before them."""
        for table, migrations in (("jobs", _JOBS_MIGRATIONS), ("handoffs", _HANDOFFS_MIGRATIONS)):
            # PRAGMA cannot take a bound parameter, so the table name is
            # interpolated into the SQL. It is one of our own literals today;
            # pin it to an allowlist so it can never drift into an injection
            # sink if a caller ever passes the name in from elsewhere.
            if table not in ("jobs", "handoffs"):
                raise ValueError(f"refusing to introspect unknown table {table!r}")
            existing = {
                row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            with self._conn:
                for column, statement in migrations.items():
                    if column not in existing:
                        self._conn.execute(statement)

    @property
    def revision(self) -> int:
        """A token that changes whenever any row in the database changes.

        Cheap validity check for caches built out of query results: an equal
        revision means the rows behind them cannot have moved, so the cached
        answer is still exactly right. (This is the only connection to the
        file, so its change counter sees every write.)
        """
        with self._lock:
            return self._conn.total_changes

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------- jobs

    def create_job(
        self,
        url: str,
        dest_dir: str,
        filename: str,
        *,
        kind: JobKind = JobKind.DIRECT,
        title: str | None = None,
        options: Mapping[str, Any] | None = None,
        queue_id: int | None = None,
    ) -> Job:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO jobs (url, dest_dir, filename, kind, title, options, queue_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    url,
                    dest_dir,
                    filename,
                    kind.value,
                    title,
                    json.dumps(dict(options or {})),
                    queue_id,
                ),
            )
            job_id = cur.lastrowid
        if job_id is None:  # pragma: no cover - sqlite always sets it
            raise RuntimeError("INSERT did not produce a row id")
        job = self.get_job(job_id)
        if job is None:  # pragma: no cover
            raise RuntimeError("job vanished right after INSERT")
        return job

    def get_job(self, job_id: int) -> Job | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _job_from_row(row) if row else None

    def job_timestamps(self, job_id: int) -> tuple[str | None, str | None]:
        """(created_at, updated_at) for a job, or (None, None) if it's gone.
        ``updated_at`` is bumped on the completion status write, so for a
        finished job it is effectively the finish time (detail panel History)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT created_at, updated_at FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            return None, None
        return row["created_at"], row["updated_at"]

    def list_jobs(self) -> list[Job]:
        # Higher priority first; ties (the default 0) fall back to insertion
        # order, so an untouched queue still runs oldest-first.
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY priority DESC, id ASC"
            ).fetchall()
        return [_job_from_row(row) for row in rows]

    def recent_jobs(self, limit: int = 5) -> list[Job]:
        """The most recently added jobs, newest first - for the extension's
        recent-downloads view."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (max(0, limit),)
            ).fetchall()
        return [_job_from_row(row) for row in rows]

    def set_priority(self, job_id: int, priority: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE jobs SET priority = ? WHERE id = ?", (priority, job_id))

    def set_retry_count(self, job_id: int, count: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE jobs SET retry_count = ? WHERE id = ?", (count, job_id))

    def latest_job_for_url(self, url: str) -> Job | None:
        """The most recent job for a URL - how the extension's progress pill
        (F1.3) finds "its" download without any job-id round trip."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE url = ? ORDER BY id DESC LIMIT 1", (url,)
            ).fetchone()
        return _job_from_row(row) if row else None

    def first_job_for_url(self, url: str) -> Job | None:
        """The earliest job pointing at a URL, for add-time duplicate detection.
        Uses the ``idx_jobs_url`` index, so it stays O(log n) instead of scanning
        the whole history the way a Python-side loop over every job did."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE url = ? ORDER BY id LIMIT 1", (url,)
            ).fetchone()
        return _job_from_row(row) if row else None

    def find_resumable_job(self, url: str, dest_dir: str) -> Job | None:
        placeholders = ", ".join("?" for _ in RESUMABLE_STATUSES)
        params = (url, dest_dir, *[status.value for status in RESUMABLE_STATUSES])
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM jobs WHERE url = ? AND dest_dir = ? "
                f"AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1",
                params,
            ).fetchone()
        return _job_from_row(row) if row else None

    def set_job_status(self, job_id: int, status: JobStatus, error: str | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET status = ?, error = ?, updated_at = datetime('now') WHERE id = ?",
                (status.value, error, job_id),
            )

    def update_job_probe(self, job: Job) -> None:
        """Persist what the probe learned (size, validators, better filename)."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET final_url = ?, filename = ?, total_size = ?, resumable = ?, "
                "etag = ?, last_modified = ?, updated_at = datetime('now') WHERE id = ?",
                (
                    job.final_url,
                    job.filename,
                    job.total_size,
                    int(job.resumable),
                    job.etag,
                    job.last_modified,
                    job.id,
                ),
            )

    def update_job_url(self, job_id: int, url: str) -> None:
        """Point a job at a new URL (mirror failover).

        Every probed field belongs to the *old* URL, so all of them are cleared
        and re-probed on the next run. Clearing final_url is what forces that
        re-probe: the downloader treats a stored final_url as a usable cached
        probe, so leaving it behind made the mirror inherit the dead URL's
        resumable/etag/size and fail instantly instead of downloading.
        """
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET url = ?, final_url = NULL, total_size = NULL, "
                "resumable = 0, etag = NULL, last_modified = NULL WHERE id = ?",
                (url, job_id),
            )

    def update_job_total(self, job_id: int, total_size: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET total_size = ?, updated_at = datetime('now') WHERE id = ?",
                (total_size, job_id),
            )

    def update_job_options(self, job_id: int, options: Mapping[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET options = ?, updated_at = datetime('now') WHERE id = ?",
                (json.dumps(dict(options)), job_id),
            )

    def update_job_filename(self, job_id: int, filename: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET filename = ?, updated_at = datetime('now') WHERE id = ?",
                (filename, job_id),
            )

    def update_job_title(self, job_id: int, title: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET title = ?, updated_at = datetime('now') WHERE id = ?",
                (title, job_id),
            )

    def update_job_dest(self, job_id: int, dest_dir: str) -> None:
        """Re-point a job at a new folder (the move-to-favorite action moves
        the file first; this keeps the row honest about where it lives)."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET dest_dir = ?, updated_at = datetime('now') WHERE id = ?",
                (dest_dir, job_id),
            )

    def delete_job(self, job_id: int) -> None:
        """Remove a job from the list/history; its segments cascade away.
        Never touches files on disk."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    # ------------------------------------------------------------- queues

    def create_queue(self, name: str) -> Queue:
        with self._lock, self._conn:
            position = self._conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM queues"
            ).fetchone()[0]
            cur = self._conn.execute(
                "INSERT INTO queues (name, position) VALUES (?, ?)", (name, position)
            )
            queue_id = cur.lastrowid
        queue = self.get_queue(int(queue_id or 0))
        if queue is None:  # pragma: no cover - sqlite always sets it
            raise RuntimeError("queue vanished right after INSERT")
        return queue

    def get_queue(self, queue_id: int) -> Queue | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM queues WHERE id = ?", (queue_id,)).fetchone()
        return _queue_from_row(row) if row else None

    def list_queues(self) -> list[Queue]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM queues ORDER BY position, id").fetchall()
        return [_queue_from_row(row) for row in rows]

    def update_queue(self, queue: Queue) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE queues SET name = ?, position = ?, max_concurrent = ?, paused = ?, "
                "schedule_enabled = ?, start_time = ?, stop_time = ?, depends_on = ?, "
                "category = ? WHERE id = ?",
                (
                    queue.name,
                    queue.position,
                    queue.max_concurrent,
                    int(queue.paused),
                    int(queue.schedule_enabled),
                    queue.start_time,
                    queue.stop_time,
                    queue.depends_on,
                    queue.category,
                    queue.id,
                ),
            )

    def delete_queue(self, queue_id: int) -> None:
        """Remove a queue; its jobs fall back to the default queue and any
        queue that depended on it is unblocked."""
        with self._lock, self._conn:
            self._conn.execute("UPDATE jobs SET queue_id = NULL WHERE queue_id = ?", (queue_id,))
            self._conn.execute(
                "UPDATE queues SET depends_on = NULL WHERE depends_on = ?", (queue_id,)
            )
            self._conn.execute("DELETE FROM queues WHERE id = ?", (queue_id,))

    def set_job_queue(self, job_id: int, queue_id: int | None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET queue_id = ?, updated_at = datetime('now') WHERE id = ?",
                (queue_id, job_id),
            )

    # ------------------------------------------------------- download stats

    def record_download(self, category: str, host: str, byte_count: int) -> None:
        """Add a finished download's bytes to today's totals (dashboard)."""
        if byte_count <= 0:
            return
        day = datetime.now().strftime("%Y-%m-%d")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO download_stats (day, category, host, bytes, files) "
                "VALUES (?, ?, ?, ?, 1) "
                "ON CONFLICT(day, category, host) DO UPDATE SET "
                "bytes = bytes + excluded.bytes, files = files + 1",
                (day, category, host, byte_count),
            )

    def bytes_since(self, day: str) -> int:
        """Total bytes recorded on or after ``day`` (YYYY-MM-DD)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(bytes), 0) FROM download_stats WHERE day >= ?", (day,)
            ).fetchone()
        return int(row[0])

    def lifetime_bytes(self) -> tuple[int, int]:
        """(total bytes, total files) ever recorded."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(bytes), 0), COALESCE(SUM(files), 0) FROM download_stats"
            ).fetchone()
        return int(row[0]), int(row[1])

    def bytes_by_host(self, limit: int = 10) -> list[tuple[str, int, int]]:
        """(host, bytes, files) for the busiest servers, biggest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT host, SUM(bytes), SUM(files) FROM download_stats "
                "WHERE host != '' GROUP BY host ORDER BY SUM(bytes) DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [(row[0], int(row[1]), int(row[2])) for row in rows]

    def bytes_by_category(self) -> list[tuple[str, int, int]]:
        """(category, bytes, files) per category, biggest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT category, SUM(bytes), SUM(files) FROM download_stats "
                "GROUP BY category ORDER BY SUM(bytes) DESC"
            ).fetchall()
        return [(row[0] or "Other", int(row[1]), int(row[2])) for row in rows]

    def clear_stats(self) -> None:
        """Wipe the dashboard statistics (Settings → Statistics). Download
        history (the jobs list) is untouched."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM download_stats")

    def prune_stats(self, keep_days: int) -> None:
        """Drop per-day statistics older than ``keep_days`` (0 = keep all)."""
        if keep_days <= 0:
            return
        cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM download_stats WHERE day < ?", (cutoff,))

    def stats_rows(self) -> list[tuple[str, str, str, int, int]]:
        """Every (day, category, host, bytes, files) row, for CSV export."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT day, category, host, bytes, files FROM download_stats ORDER BY day"
            ).fetchall()
        return [(r[0], r[1] or "", r[2] or "", int(r[3]), int(r[4])) for r in rows]

    def all_settings(self) -> dict[str, str]:
        """Every persisted setting, for export/backup."""
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        return {str(r[0]): str(r[1]) for r in rows}

    def import_settings(self, values: dict[str, str]) -> int:
        """Bulk-restore settings from an export; returns how many were set."""
        with self._lock, self._conn:
            for key, value in values.items():
                self._conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(key), str(value)),
                )
        return len(values)

    def reset_settings(self) -> None:
        """Delete every persisted setting - everything returns to defaults."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM settings")

    def vacuum(self) -> None:
        """Compact the database file (VACUUM cannot run inside a transaction)."""
        with self._lock:
            self._conn.execute("VACUUM")

    def integrity_check(self) -> str:
        """SQLite's own integrity verdict - "ok" when the file is healthy."""
        with self._lock:
            row = self._conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "unknown"

    def update_job_downloaded(self, job_id: int, downloaded: int) -> None:
        """Progress mirror for smart/hls jobs (direct jobs checkpoint segments)."""
        with self._lock, self._conn:
            self._conn.execute("UPDATE jobs SET downloaded = ? WHERE id = ?", (downloaded, job_id))

    def stored_progress(self, job: Job) -> int:
        """Bytes on record for a job that is not currently running."""
        if job.kind is JobKind.DIRECT:
            # A completed direct job has had its segment rows pruned (they had
            # nothing left to say); the job row carries the final figure.
            return self.job_downloaded(job.id) or job.downloaded
        return job.downloaded

    def mark_interrupted(self) -> int:
        """Flip jobs a dead process left 'downloading' to 'paused'. Run at startup."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE jobs SET status = ?, updated_at = datetime('now') WHERE status = ?",
                (JobStatus.PAUSED.value, JobStatus.DOWNLOADING.value),
            )
        return cur.rowcount

    # --------------------------------------------------------- handoffs

    def add_handoff(
        self,
        url: str,
        *,
        page_url: str | None = None,
        page_title: str | None = None,
        source: str = "extension",
        payload: Sequence[str] = (),
        quality: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO handoffs "
                "(url, page_url, page_title, source, payload, quality, headers) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    url,
                    page_url,
                    page_title,
                    source,
                    json.dumps(list(payload)),
                    quality,
                    json.dumps(dict(headers or {})),
                ),
            )
        handoff_id = cur.lastrowid
        if handoff_id is None:  # pragma: no cover - sqlite always sets it
            raise RuntimeError("INSERT did not produce a row id")
        return handoff_id

    def claim_handoffs(self) -> list[Handoff]:
        """Atomically take every unclaimed handoff (exactly-once processing)."""
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT * FROM handoffs WHERE claimed = 0 ORDER BY id"
            ).fetchall()
            if rows:
                self._conn.executemany(
                    "UPDATE handoffs SET claimed = 1 WHERE id = ?",
                    [(row["id"],) for row in rows],
                )
                # A claimed row has done its job. They were kept forever, so
                # this table grew by one row per browser download for the life
                # of the profile - and every poll scanned past all of them.
                self._conn.execute(
                    "DELETE FROM handoffs WHERE claimed = 1 AND id < ?", (rows[0]["id"],)
                )
        return [
            Handoff(
                id=row["id"],
                url=row["url"],
                page_url=row["page_url"],
                page_title=row["page_title"],
                source=row["source"],
                payload=tuple(json.loads(row["payload"] or "[]")),
                quality=row["quality"],
                headers=json.loads(row["headers"] or "{}"),
            )
            for row in rows
        ]

    # --------------------------------------------------------- settings

    def get_setting(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # --------------------------------------------------------- segments

    def replace_segments(
        self, job_id: int, spans: Sequence[tuple[int, int | None]]
    ) -> list[Segment]:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM segments WHERE job_id = ?", (job_id,))
            self._conn.executemany(
                "INSERT INTO segments (job_id, seg_index, start_byte, end_byte) "
                "VALUES (?, ?, ?, ?)",
                [(job_id, i, start, end) for i, (start, end) in enumerate(spans)],
            )
        return self.segments_for(job_id)

    def add_segment(self, job_id: int, seg_index: int, start: int, end: int | None) -> Segment:
        """Insert one segment (used when a worker steals work at runtime)."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO segments (job_id, seg_index, start_byte, end_byte) "
                "VALUES (?, ?, ?, ?)",
                (job_id, seg_index, start, end),
            )
            seg_id = cur.lastrowid
        if seg_id is None:  # pragma: no cover - sqlite always sets it
            raise RuntimeError("INSERT did not produce a row id")
        return Segment(
            id=seg_id, job_id=job_id, index=seg_index, start=start, end=end, downloaded=0
        )

    def segments_for(self, job_id: int) -> list[Segment]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM segments WHERE job_id = ? ORDER BY seg_index", (job_id,)
            ).fetchall()
        return [_segment_from_row(row) for row in rows]

    def update_segment_progress(self, progress: Mapping[int, int]) -> None:
        if not progress:
            return
        with self._lock, self._conn:
            self._conn.executemany(
                "UPDATE segments SET downloaded = ? WHERE id = ?",
                [(downloaded, segment_id) for segment_id, downloaded in progress.items()],
            )

    def set_segment_end(self, segment_id: int, end: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE segments SET end_byte = ? WHERE id = ?", (end, segment_id))

    def clear_segments(self, job_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM segments WHERE job_id = ?", (job_id,))

    def job_downloaded(self, job_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(downloaded), 0) AS total FROM segments WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return int(row["total"])

    def all_segment_progress(self) -> dict[int, int]:
        """Summed downloaded bytes per job, in one query - the UI snapshot
        polls this every refresh, so it must not be N separate SUM queries."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id, COALESCE(SUM(downloaded), 0) AS total FROM segments GROUP BY job_id"
            ).fetchall()
        return {int(row["job_id"]): int(row["total"]) for row in rows}
