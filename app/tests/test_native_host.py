from __future__ import annotations

import io
import struct
from pathlib import Path
from typing import Any

import pytest

from app.core import instance
from app.db.database import Database
from app.native_host.host import handle_message, serve
from app.native_host.protocol import (
    MAX_MESSAGE_BYTES,
    ProtocolError,
    read_message,
    write_message,
)


@pytest.fixture(autouse=True)
def isolated_pid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(instance, "pid_file", lambda: tmp_path / "grabline.pid")


# ------------------------------------------------------------- protocol


def test_protocol_roundtrip():
    buffer = io.BytesIO()
    write_message(buffer, {"type": "ping", "n": 1})
    buffer.seek(0)
    assert read_message(buffer) == {"type": "ping", "n": 1}
    assert read_message(buffer) is None  # clean EOF


def test_protocol_rejects_truncated_header():
    with pytest.raises(ProtocolError, match="truncated"):
        read_message(io.BytesIO(b"\x01\x00"))


def test_protocol_rejects_truncated_body():
    buffer = io.BytesIO(struct.pack("<I", 100) + b"{}")
    with pytest.raises(ProtocolError, match="truncated"):
        read_message(buffer)


def test_protocol_rejects_oversized_messages():
    buffer = io.BytesIO(struct.pack("<I", MAX_MESSAGE_BYTES + 1))
    with pytest.raises(ProtocolError, match="too large"):
        read_message(buffer)
    with pytest.raises(ProtocolError, match="oversized"):
        write_message(io.BytesIO(), {"blob": "x" * (MAX_MESSAGE_BYTES + 1)})


def test_protocol_rejects_non_object_and_bad_json():
    for payload in (b"[1, 2]", b"not json"):
        buffer = io.BytesIO(struct.pack("<I", len(payload)) + payload)
        with pytest.raises(ProtocolError):
            read_message(buffer)


# ----------------------------------------------------------------- host


def test_ping_reports_app_not_running(db: Database):
    reply = handle_message(db, {"type": "ping"})
    assert reply["type"] == "pong"
    assert reply["appRunning"] is False


def test_ping_reports_app_running(db: Database):
    instance.write_pid()  # this test process counts as "running"
    reply = handle_message(db, {"type": "ping"})
    assert reply["appRunning"] is True


def test_download_creates_handoff(db: Database):
    reply = handle_message(
        db,
        {
            "type": "download",
            "url": "https://example.com/video.mp4",
            "pageUrl": "https://example.com/watch",
            "pageTitle": "  A Page  ",
        },
    )
    assert reply["type"] == "queued"
    handoffs = db.claim_handoffs()
    assert len(handoffs) == 1
    handoff = handoffs[0]
    assert handoff.id == reply["handoffId"]
    assert handoff.url == "https://example.com/video.mp4"
    assert handoff.page_url == "https://example.com/watch"
    assert handoff.page_title == "A Page"
    assert handoff.source == "extension"


def test_only_if_running_records_nothing_when_the_app_is_closed(db: Database):
    """The extension sets this while deciding whether to cancel a browser
    download. With no app to receive it, recording a handoff anyway meant the
    browser finished the file and the app downloaded it all over again at the
    next launch."""
    reply = handle_message(
        db,
        {
            "type": "download",
            "url": "https://example.com/file.zip",
            "onlyIfRunning": True,
        },
    )
    assert reply == {"type": "notRunning", "appRunning": False}
    assert db.claim_handoffs() == []


# ----------------------------------------- cookie pass-through (headers)


def test_download_carries_cookie_referer_and_user_agent(db: Database):
    handle_message(
        db,
        {
            "type": "download",
            "url": "https://example.com/gated.zip",
            "cookie": "session=abc; theme=dark",
            "referer": "https://example.com/account",
            "userAgent": "Mozilla/5.0 (Grabline)",
        },
    )
    headers = db.claim_handoffs()[0].headers
    assert headers["Cookie"] == "session=abc; theme=dark"
    assert headers["Referer"] == "https://example.com/account"
    assert headers["User-Agent"] == "Mozilla/5.0 (Grabline)"


def test_cookie_header_strips_crlf_injection(db: Database):
    handle_message(
        db,
        {
            "type": "download",
            "url": "https://example.com/f.bin",
            "cookie": "session=abc\r\nX-Evil: 1",
        },
    )
    assert db.claim_handoffs()[0].headers["Cookie"] == "session=abcX-Evil: 1"


def test_bad_header_values_are_dropped(db: Database):
    handle_message(
        db,
        {
            "type": "download",
            "url": "https://example.com/f.bin",
            "cookie": 42,  # not a string
            "referer": "not-a-url",  # not http(s)
            "userAgent": "   ",  # blank after strip
        },
    )
    assert db.claim_handoffs()[0].headers == {}


def test_download_without_headers_stores_empty(db: Database):
    handle_message(db, {"type": "download", "url": "https://example.com/f.bin"})
    assert db.claim_handoffs()[0].headers == {}


def test_download_rejects_bad_urls(db: Database):
    for url in ("ftp://host/f", "javascript:alert(1)", "", None, 42, "x" * 9000):
        reply = handle_message(db, {"type": "download", "url": url})
        assert reply["type"] == "error"
    assert db.claim_handoffs() == []


def test_page_title_is_truncated(db: Database):
    handle_message(db, {"type": "download", "url": "https://x.test/f", "pageTitle": "t" * 2000})
    handoff = db.claim_handoffs()[0]
    assert handoff.page_title is not None and len(handoff.page_title) == 512


def test_unknown_type_is_an_error_reply(db: Database):
    reply = handle_message(db, {"type": "reboot"})
    assert reply["type"] == "error"


# -------------------------------------------- quality + status (F1.3)


def test_download_carries_a_valid_quality_label(db: Database):
    handle_message(
        db, {"type": "download", "url": "https://example.com/watch?v=1", "quality": "1080p"}
    )
    assert db.claim_handoffs()[0].quality == "1080p"


def test_bogus_quality_labels_are_dropped(db: Database):
    for quality in ("4320p", "<script>", "", 42, "x" * 100):
        handle_message(db, {"type": "download", "url": "https://example.com/v", "quality": quality})
    assert all(handoff.quality is None for handoff in db.claim_handoffs())


def test_status_reports_pending_and_job_progress(db: Database):
    job = db.create_job("https://example.com/v.mp4", "/tmp", "v.mp4")
    db.update_job_total(job.id, 1000)
    db.replace_segments(job.id, [(0, 999)])
    db.update_segment_progress({db.segments_for(job.id)[0].id: 250})

    reply = handle_message(
        db,
        {
            "type": "status",
            "urls": ["https://example.com/v.mp4", "https://example.com/unknown", "ftp://x"],
        },
    )
    assert reply["type"] == "status"
    assert len(reply["jobs"]) == 2  # the ftp URL is dropped, not echoed
    known, unknown = reply["jobs"]
    assert known["status"] == "queued"
    assert known["downloaded"] == 250
    assert known["total"] == 1000
    assert known["name"] == "v.mp4"
    assert unknown == {"url": "https://example.com/unknown", "status": "pending"}


def test_download_stores_sniffed_fallbacks(db: Database):
    handle_message(
        db,
        {
            "type": "download",
            "url": "https://movies.example/watch/123",
            "fallbackUrls": [
                "https://cdn.example/movie/master.m3u8",
                "javascript:alert(1)",  # dropped
                "https://movies.example/watch/123",  # same as url - dropped
                "https://cdn.example/movie/720.mp4",
            ],
        },
    )
    handoff = db.claim_handoffs()[0]
    assert handoff.payload == (
        "https://cdn.example/movie/master.m3u8",
        "https://cdn.example/movie/720.mp4",
    )


def test_latest_job_for_url_prefers_the_newest(db: Database):
    db.create_job("https://example.com/f", "/tmp", "old.bin")
    newer = db.create_job("https://example.com/f", "/tmp", "new.bin")
    found = db.latest_job_for_url("https://example.com/f")
    assert found is not None and found.id == newer.id
    assert db.latest_job_for_url("https://example.com/other") is None


# -------------------------------------------------------- gallery (F2.2)


def test_gallery_creates_one_handoff_with_payload(db: Database):
    reply = handle_message(
        db,
        {
            "type": "gallery",
            "urls": [
                "https://example.com/a.jpg",
                "javascript:alert(1)",  # dropped
                "https://example.com/b.png",
            ],
            "pageUrl": "https://example.com/gallery",
            "pageTitle": "Holiday",
        },
    )
    assert reply["type"] == "queued"
    assert reply["count"] == 2
    handoffs = db.claim_handoffs()
    assert len(handoffs) == 1
    handoff = handoffs[0]
    assert handoff.source == "gallery"
    assert handoff.url == "https://example.com/gallery"
    assert handoff.payload == ("https://example.com/a.jpg", "https://example.com/b.png")


def test_gallery_with_no_valid_urls_is_an_error(db: Database):
    for urls in ([], ["ftp://x/y"], "not-a-list", None):
        reply = handle_message(db, {"type": "gallery", "urls": urls})
        assert reply["type"] == "error"
    assert db.claim_handoffs() == []


def test_gallery_is_capped(db: Database):
    urls = [f"https://example.com/{i}.jpg" for i in range(500)]
    reply = handle_message(db, {"type": "gallery", "urls": urls})
    assert reply["count"] == 300
    assert len(db.claim_handoffs()[0].payload) == 300


def test_links_creates_handoff_with_source_links(db: Database):
    reply = handle_message(
        db,
        {
            "type": "links",
            "urls": [
                "https://files.example/a.zip",
                "not-a-url",  # dropped
                "https://files.example/b.pdf",
                "https://files.example/a.zip",  # duplicate dropped
            ],
            "pageUrl": "https://files.example/downloads",
            "pageTitle": "Downloads",
        },
    )
    assert reply["type"] == "queued"
    assert reply["count"] == 2
    handoff = db.claim_handoffs()[0]
    assert handoff.source == "links"
    assert handoff.payload == ("https://files.example/a.zip", "https://files.example/b.pdf")


def test_links_with_nothing_downloadable_is_an_error(db: Database):
    reply = handle_message(db, {"type": "links", "urls": ["ftp://x/y"]})
    assert reply["type"] == "error"
    assert db.claim_handoffs() == []


# ---------------------------------------------------------------- serve


def _framed(*messages: dict[str, Any]) -> io.BytesIO:
    buffer = io.BytesIO()
    for message in messages:
        write_message(buffer, message)
    buffer.seek(0)
    return buffer


def _replies(buffer: io.BytesIO) -> list[dict[str, Any]]:
    buffer.seek(0)
    out: list[dict[str, Any]] = []
    while (message := read_message(buffer)) is not None:
        out.append(message)
    return out


def test_serve_end_to_end(db: Database):
    stdin = _framed(
        {"type": "ping"},
        {"type": "download", "url": "https://example.com/a.zip"},
    )
    stdout = io.BytesIO()
    serve(stdin, stdout, db)
    replies = _replies(stdout)
    assert [reply["type"] for reply in replies] == ["pong", "queued"]
    assert len(db.claim_handoffs()) == 1


def test_serve_replies_error_on_protocol_garbage(db: Database):
    stdin = io.BytesIO(struct.pack("<I", 5) + b"nope!")
    stdout = io.BytesIO()
    serve(stdin, stdout, db)
    replies = _replies(stdout)
    assert len(replies) == 1
    assert replies[0]["type"] == "error"


def test_handoff_claims_are_exactly_once(db: Database):
    db.add_handoff("https://x.test/1")
    db.add_handoff("https://x.test/2")
    first = db.claim_handoffs()
    assert [h.url for h in first] == ["https://x.test/1", "https://x.test/2"]
    assert db.claim_handoffs() == []


def test_recent_returns_newest_jobs_first(db: Database):
    from app.core.models import JobKind

    for i in range(3):
        db.create_job(
            f"https://example.com/{i}.mp4",
            "/tmp",
            f"{i}.mp4",
            kind=JobKind.SMART,
            title=f"clip {i}",
        )
    reply = handle_message(db, {"type": "recent", "limit": 2})
    assert reply["type"] == "recent"
    assert [j["name"] for j in reply["jobs"]] == ["clip 2", "clip 1"]  # newest first
    assert all({"url", "status", "downloaded", "total", "name"} <= set(j) for j in reply["jobs"])


def test_focus_drops_a_marker_handoff(db: Database):
    reply = handle_message(db, {"type": "focus"})
    assert reply["type"] == "ok"
    handoffs = db.claim_handoffs()
    assert len(handoffs) == 1 and handoffs[0].source == "focus"


def test_unknown_type_still_rejected(db: Database):
    reply = handle_message(db, {"type": "nonsense"})
    assert reply["type"] == "error"
