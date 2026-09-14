"""Timed download window, import/export list, crawler, and update check."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from app.core import crawler, listio, update
from app.core.manager import DownloadManager, _in_window
from app.core.models import JobKind
from app.db.database import Database
from app.tests.media_server import MediaServer


@pytest.fixture()
def manager(db: Database) -> Iterator[DownloadManager]:
    mgr = DownloadManager(db, max_concurrent=0)
    yield mgr
    mgr.shutdown()


# --------------------------------------------------- timed download window


def test_window_helper_same_day_and_wrap():
    assert _in_window(datetime(2026, 1, 1, 3, 0), "01:00", "06:00")
    assert not _in_window(datetime(2026, 1, 1, 7, 0), "01:00", "06:00")
    assert _in_window(datetime(2026, 1, 1, 2, 0), "23:00", "07:00")  # wraps midnight
    assert not _in_window(datetime(2026, 1, 1, 12, 0), "23:00", "07:00")
    assert not _in_window(datetime(2026, 1, 1, 3, 0), "05:00", "05:00")  # empty


def test_downloads_allowed_respects_schedule(manager: DownloadManager):
    assert manager.downloads_allowed_now() is True  # disabled by default
    manager.settings.download_schedule_enabled = True
    manager.settings.download_start = "00:00"
    manager.settings.download_stop = "23:59"
    assert manager.downloads_allowed_now() is True
    manager.settings.download_start = "00:00"
    manager.settings.download_stop = "00:00"  # empty window: never allowed
    assert manager.downloads_allowed_now() is False


# ----------------------------------------------------- import / export list


def test_export_then_import_roundtrip(db: Database, tmp_path: Path):
    db.create_job("https://x.test/a.zip", "/tmp", "a.zip")
    db.create_job(
        "https://x.test/v",
        "/tmp",
        "v.mp4",
        kind=JobKind.SMART,
        title="Clip",
        options={"quality_label": "1080p"},
    )
    out = tmp_path / "list.json"
    assert listio.write_file(db, out) == 2

    fresh = Database(tmp_path / "fresh.db")
    try:
        assert listio.read_file(fresh, out) == 2
        jobs = fresh.list_jobs()
        assert {j.url for j in jobs} == {"https://x.test/a.zip", "https://x.test/v"}
        smart = next(j for j in jobs if j.kind is JobKind.SMART)
        assert smart.title == "Clip" and smart.options["quality_label"] == "1080p"
    finally:
        fresh.close()


def test_export_strips_session_cookies(db: Database, tmp_path: Path):
    """A browser-handoff download stores the tab's Cookie header in its
    options; exporting the list must not write those live session cookies to
    the shareable JSON file (CWE-312)."""
    db.create_job(
        "https://x.test/gated.bin",
        "/tmp",
        "gated.bin",
        options={
            "http_headers": {"Cookie": "session=SECRET-TOKEN", "Referer": "https://x.test/"},
            "cookie_file": "/home/me/cookies.txt",
            "quality_label": "1080p",  # a non-secret option that must survive
        },
    )
    out = tmp_path / "list.json"
    listio.write_file(db, out)
    text = out.read_text()
    assert "SECRET-TOKEN" not in text
    assert "http_headers" not in text
    assert "cookie_file" not in text
    assert "1080p" in text  # ordinary options are preserved

    data = listio.export_jobs(db)
    assert data["items"][0]["options"] == {"quality_label": "1080p"}


def test_import_rejects_foreign_and_bad_entries(db: Database, tmp_path: Path):
    good = {
        "format": "grabline-downloads",
        "version": 1,
        "items": [
            {"url": "https://x.test/a.bin", "dest_dir": "/tmp", "filename": "a.bin"},
            {"url": "ftp://x/y", "dest_dir": "/tmp", "filename": "y"},  # bad scheme
            {"url": "https://x.test/b", "dest_dir": "", "filename": ""},  # missing dest
        ],
    }
    assert listio.import_jobs(db, good) == 1
    with pytest.raises(ValueError):
        listio.import_jobs(db, {"format": "something-else", "items": []})


# ------------------------------------------------------------- site grabber


def test_crawler_collects_files_and_follows_depth(server: MediaServer):
    server.add(
        "/index.html",
        b"<a href='/a.zip'>a</a> <a href='/sub/page.html'>more</a> <a href='/x.txt'>x</a>",
        content_type="text/html",
    )
    server.add(
        "/sub/page.html",
        b"<a href='/b.pdf'>b</a> <img src='/c.jpg'>",
        content_type="text/html",
    )
    # depth 0: only files linked from the first page (a.zip; x.txt is not a
    # known downloadable type).
    shallow = crawler.crawl(server.url("/index.html"), depth=0)
    assert server.url("/a.zip") in shallow
    assert server.url("/b.pdf") not in shallow
    # depth 1: follows the same-host HTML link and finds b.pdf and c.jpg.
    deep = crawler.crawl(server.url("/index.html"), depth=1)
    assert server.url("/b.pdf") in deep
    assert server.url("/c.jpg") in deep


def test_crawler_stays_on_host(server: MediaServer):
    server.add(
        "/only.html",
        b"<a href='https://evil.example/other.html'>x</a> <a href='/local.zip'>z</a>",
        content_type="text/html",
    )
    found = crawler.crawl(server.url("/only.html"), depth=2)
    assert found == [server.url("/local.zip")]  # never left the host


# --------------------------------------------------------------- updates


def test_is_newer():
    assert update.is_newer("v1.2.0", "1.1.0")
    assert update.is_newer("2.0.0", "1.9.9")
    assert not update.is_newer("1.0.0", "1.0.0")
    assert not update.is_newer("v0.9.0", "1.0.0")
