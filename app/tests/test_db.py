from __future__ import annotations

import sqlite3
from pathlib import Path

from app.core.models import JobKind, JobStatus
from app.db.database import Database

_PHASE0_SCHEMA = """
CREATE TABLE jobs (
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
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE segments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    seg_index  INTEGER NOT NULL,
    start_byte INTEGER NOT NULL,
    end_byte   INTEGER,
    downloaded INTEGER NOT NULL DEFAULT 0
);
"""


def test_job_roundtrip(db: Database):
    job = db.create_job("http://x.test/a.bin", "/tmp/dl", "a.bin")
    assert job.status is JobStatus.QUEUED
    fetched = db.get_job(job.id)
    assert fetched is not None
    assert (fetched.url, fetched.filename) == ("http://x.test/a.bin", "a.bin")


def test_first_job_for_url_returns_earliest_match(db: Database):
    a = db.create_job("http://x.test/dup.bin", "/tmp/dl", "dup.bin")
    db.create_job("http://x.test/dup.bin", "/tmp/dl", "dup.bin")  # a later duplicate
    db.create_job("http://x.test/other.bin", "/tmp/dl", "other.bin")

    found = db.first_job_for_url("http://x.test/dup.bin")
    assert found is not None and found.id == a.id  # the earliest, not the latest
    assert db.first_job_for_url("http://x.test/missing.bin") is None


def test_status_and_error_persist(db: Database):
    job = db.create_job("http://x.test/a.bin", "/tmp/dl", "a.bin")
    db.set_job_status(job.id, JobStatus.FAILED, error="disk full")
    fetched = db.get_job(job.id)
    assert fetched is not None
    assert fetched.status is JobStatus.FAILED
    assert fetched.error == "disk full"
    db.set_job_status(job.id, JobStatus.QUEUED)
    fetched = db.get_job(job.id)
    assert fetched is not None
    assert fetched.error is None


def test_segments_roundtrip_and_progress(db: Database):
    job = db.create_job("http://x.test/a.bin", "/tmp/dl", "a.bin")
    segments = db.replace_segments(job.id, [(0, 99), (100, 199), (200, None)])
    assert [(s.start, s.end) for s in segments] == [(0, 99), (100, 199), (200, None)]
    db.update_segment_progress({segments[0].id: 100, segments[1].id: 40})
    assert db.job_downloaded(job.id) == 140
    reloaded = db.segments_for(job.id)
    assert reloaded[0].is_complete
    assert not reloaded[1].is_complete
    assert not reloaded[2].is_complete  # unknown end is never complete


def test_all_segment_progress_is_one_query(db: Database):
    a = db.create_job("http://x.test/a.bin", "/tmp/dl", "a.bin")
    b = db.create_job("http://x.test/b.bin", "/tmp/dl", "b.bin")
    db.create_job("http://x.test/c.bin", "/tmp/dl", "c.bin")  # no segments yet
    sa = db.replace_segments(a.id, [(0, 99), (100, 199)])
    sb = db.replace_segments(b.id, [(0, 99)])
    db.update_segment_progress({sa[0].id: 100, sa[1].id: 30, sb[0].id: 50})
    progress = db.all_segment_progress()
    assert progress[a.id] == 130
    assert progress[b.id] == 50
    assert progress.get(a.id) == db.job_downloaded(a.id)  # matches the per-job query


def test_mark_interrupted_flips_downloading_to_paused(db: Database):
    job = db.create_job("http://x.test/a.bin", "/tmp/dl", "a.bin")
    db.set_job_status(job.id, JobStatus.DOWNLOADING)
    assert db.mark_interrupted() == 1
    fetched = db.get_job(job.id)
    assert fetched is not None
    assert fetched.status is JobStatus.PAUSED


def test_find_resumable_job(db: Database):
    job = db.create_job("http://x.test/a.bin", "/tmp/dl", "a.bin")
    assert db.find_resumable_job("http://x.test/a.bin", "/tmp/dl") is not None
    db.set_job_status(job.id, JobStatus.COMPLETED)
    assert db.find_resumable_job("http://x.test/a.bin", "/tmp/dl") is None


def test_smart_job_options_roundtrip(db: Database):
    options = {"format_spec": "bv*+ba/b", "audio_format": None, "trim": [10.0, 20.0]}
    job = db.create_job(
        "http://x.test/v",
        "/tmp/dl",
        "v.mp4",
        kind=JobKind.SMART,
        title="A Video",
        options=options,
    )
    fetched = db.get_job(job.id)
    assert fetched is not None
    assert fetched.kind is JobKind.SMART
    assert fetched.title == "A Video"
    assert fetched.options == options


def test_downloaded_mirror_and_stored_progress(db: Database):
    job = db.create_job("http://x.test/v", "/tmp/dl", "v.mp4", kind=JobKind.SMART)
    db.update_job_downloaded(job.id, 12345)
    fetched = db.get_job(job.id)
    assert fetched is not None
    assert db.stored_progress(fetched) == 12345
    direct = db.create_job("http://x.test/d.bin", "/tmp/dl", "d.bin")
    segments = db.replace_segments(direct.id, [(0, 99)])
    db.update_segment_progress({segments[0].id: 50})
    fetched_direct = db.get_job(direct.id)
    assert fetched_direct is not None
    assert db.stored_progress(fetched_direct) == 50


def test_phase0_database_migrates_in_place(tmp_path: Path):
    db_path = tmp_path / "old.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(_PHASE0_SCHEMA)
    raw.execute(
        "INSERT INTO jobs (url, dest_dir, filename, status) VALUES (?, ?, ?, ?)",
        ("http://x.test/old.bin", "/tmp/dl", "old.bin", "paused"),
    )
    raw.commit()
    raw.close()

    db = Database(db_path)
    try:
        job = db.get_job(1)
        assert job is not None
        assert job.kind is JobKind.DIRECT
        assert job.options == {}
        assert job.downloaded == 0
        # new tables and columns are usable
        db.set_setting("k", "v")
        assert db.get_setting("k") == "v"
        smart = db.create_job("http://x.test/new", "/tmp/dl", "new.mp4", kind=JobKind.SMART)
        assert smart.kind is JobKind.SMART
    finally:
        db.close()


_OLD_HANDOFFS_SCHEMA = """
CREATE TABLE handoffs (
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
"""


def test_handoff_options_roundtrip(db: Database):
    opts = {"quality": "best", "audio_only": False, "n": 2}
    db.add_handoff("http://x.test/v", source="phone", options=opts)
    db.add_handoff("http://x.test/w", source="phone")
    first, second = db.claim_handoffs()
    assert first.source == "phone"
    assert first.options == opts
    assert second.options == {}


def test_handoff_options_stored_compact_and_sorted(db: Database):
    db.add_handoff("http://x.test/v", options={"b": 1, "a": 2})
    raw = db._conn.execute("SELECT options FROM handoffs").fetchone()[0]
    assert raw == '{"a":2,"b":1}'


def test_handoff_options_migrate_old_database(tmp_path: Path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_HANDOFFS_SCHEMA)
    conn.execute("INSERT INTO handoffs (url, source) VALUES ('http://x.test/old', 'phone')")
    conn.commit()
    conn.close()
    database = Database(path)
    try:
        cols = {r["name"] for r in database._conn.execute("PRAGMA table_info(handoffs)")}
        assert "options" in cols
        database.add_handoff("http://x.test/new", options={"k": "v"})
        old, new = database.claim_handoffs()
        assert old.options == {}
        assert new.options == {"k": "v"}
    finally:
        database.close()


def test_handoff_malformed_options_become_empty(db: Database, caplog):
    for bad in ("{not json", "[1, 2]", "null"):
        hid = db.add_handoff("http://x.test/v")
        with db._lock, db._conn:
            db._conn.execute("UPDATE handoffs SET options = ? WHERE id = ?", (bad, hid))
    with caplog.at_level("WARNING"):
        claimed = db.claim_handoffs()
    assert [h.options for h in claimed] == [{}, {}, {}]
    assert "malformed options" in caplog.text
