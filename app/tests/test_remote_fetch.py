"""Files, fetch, fileHash and outbox (PROTOCOL.md section 13) over real TLS.

A real ``RemoteService`` with a real manager and database (``Env`` from
test_remote_service.py); a TLS WebSocket client plays the phone, reads the
binary frames itself and acknowledges them like the Android app does.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import queue
import struct
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.core.models import Job, JobStatus
from app.remote import fetch, wire
from app.remote import listener as listener_module
from app.remote.outbox import Outbox
from app.tests.remote_client import Client, pump, settle
from app.tests.test_remote_security import ShiftedTime, ask, quiet, service_login
from app.tests.test_remote_service import Env

pytestmark = pytest.mark.timeout(90)

URL = "https://example.com/files/a.bin"
MIB = 1024 * 1024
_ids = itertools.count(1)


@pytest.fixture(autouse=True)
def _temp_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))


@pytest.fixture()
def env(tmp_path: Path) -> Iterator[Env]:
    made = Env(tmp_path)
    try:
        yield made
    finally:
        made.close()


def finished(env: Env, name: str, data: bytes, status: JobStatus = JobStatus.COMPLETED) -> Job:
    (env.dest / name).write_bytes(data)
    job = env.db.create_job(URL, str(env.dest), name)
    env.db.set_job_status(job.id, status, None)
    return job


def rid() -> str:
    return f"f-{next(_ids)}"


def fetch_start(client: Client, job_id: Any, **extra: Any) -> dict[str, Any]:
    return ask(client, {"id": rid(), "type": "fetch", "jobId": job_id, **extra})


def binary_frames(client: Client, since: int) -> list[bytes]:
    return [
        payload for opcode, payload, _t in list(client.frames)[since:] if opcode == wire.OP_BINARY
    ]


def receive(
    client: Client, start: dict[str, Any], *, since: int, ack: bool = True, timeout: float = 30.0
) -> tuple[bytes, dict[str, Any]]:
    """Collect one stream: (the bytes, the ``fetchEnd`` event). Checks every
    frame header and acknowledges as it goes unless ``ack`` is false."""
    stream_id = start["streamId"]
    data = bytearray()
    cursor = since
    deadline = time.monotonic() + timeout

    def take() -> bool:
        nonlocal cursor
        frames = list(client.frames)
        fresh = False
        for opcode, payload, _t in frames[cursor:]:
            if opcode != wire.OP_BINARY:
                continue
            magic, sid, offset = struct.unpack(">4sIQ", payload[:16])
            assert magic == b"GLF1" and sid == stream_id
            assert offset == start["offset"] + len(data), "frames arrive in order, no gaps"
            assert 1 <= len(payload) - 16 <= fetch.CHUNK_SIZE
            data.extend(payload[16:])
            fresh = True
        cursor = len(frames)
        return fresh

    while time.monotonic() < deadline:
        pump(lambda: len(client.frames) > cursor or not client.inbox.empty(), timeout=0.2)
        if take() and ack:
            client.send(
                {
                    "type": "fetchAck",
                    "streamId": stream_id,
                    "received": start["offset"] + len(data),
                }
            )
        try:
            kind, value = client.inbox.get_nowait()
        except queue.Empty:
            continue
        if kind == "text" and value.get("type") == "fetchEnd" and value["streamId"] == stream_id:
            take()
            return bytes(data), value
    raise AssertionError("the stream did not end")


# ------------------------------------------------------------------ fetch


def test_fetch_a_finished_file_with_acks(env: Env) -> None:
    payload = os.urandom(3 * MIB + 123)
    job = finished(env, "movie.mp4", payload)
    mtime = int((env.dest / "movie.mp4").stat().st_mtime)
    _phone, _pin, client = service_login(env)
    since = len(client.frames)
    start = fetch_start(client, job.id)
    assert start["type"] == "fetchStart" and start["protocolVersion"] == 6
    assert start["jobId"] == job.id and start["name"] == "movie.mp4"
    assert start["size"] == len(payload) and start["offset"] == 0
    assert start["length"] == len(payload) and start["streamId"] == 1
    assert start["chunkSize"] == 262_144 and start["windowBytes"] == 8_388_608
    assert start["etag"] == f"s{len(payload)}-m{mtime}"
    assert "_open" not in start and str(env.dest) not in json.dumps(start)
    data, end = receive(client, start, since=since)
    assert data == payload
    assert end["status"] == "complete" and end["nextOffset"] == len(payload)
    assert end["protocolVersion"] == 6 and "id" not in end


def test_the_window_stops_the_stream_until_the_phone_acks(env: Env) -> None:
    payload = os.urandom(20 * MIB)
    job = finished(env, "big.bin", payload)
    _phone, _pin, client = service_login(env)
    since = len(client.frames)
    start = fetch_start(client, job.id)
    assert pump(lambda: sum(len(p) - 16 for p in binary_frames(client, since)) >= 8 * MIB)
    settle(0.5)
    frames = binary_frames(client, since)
    assert sum(len(p) - 16 for p in frames) == fetch.WINDOW_BYTES  # not a byte more
    assert max(len(p) for p in frames) == 262_160
    data, end = receive(client, start, since=since)  # acks now flow
    assert data == payload and end["status"] == "complete"


def test_resume_with_offset_and_etag(env: Env) -> None:
    payload = os.urandom(MIB)
    job = finished(env, "r.bin", payload)
    _phone, _pin, client = service_login(env)
    first = fetch_start(client, job.id, length=1000)
    assert first["length"] == 1000
    data, end = receive(client, first, since=0)
    assert data == payload[:1000] and end["status"] == "complete" and end["nextOffset"] == 1000
    since = len(client.frames)
    again = fetch_start(client, job.id, offset=1000, etag=first["etag"])
    assert again["offset"] == 1000 and again["length"] == len(payload) - 1000
    assert again["streamId"] == 2
    data, end = receive(client, again, since=since)
    assert data == payload[1000:] and end["nextOffset"] == len(payload)


def test_etag_mismatch_or_a_changed_file_is_a_conflict(env: Env) -> None:
    job = finished(env, "c.bin", b"a" * 5000)
    _phone, _pin, client = service_login(env)
    assert fetch_start(client, job.id, etag="s1-m1")["code"] == "conflict"
    first = fetch_start(client, job.id, length=10)
    receive(client, first, since=0)
    (env.dest / "c.bin").write_bytes(b"b" * 6000)  # another size, another etag
    reply = fetch_start(client, job.id, offset=10, etag=first["etag"])
    assert reply["type"] == "error" and reply["code"] == "conflict"
    fresh = fetch_start(client, job.id)  # starting again from 0 works
    data, _end = receive(client, fresh, since=len(client.frames))
    assert data == b"b" * 6000


def test_offset_past_the_end_is_a_conflict_and_the_end_is_an_empty_stream(env: Env) -> None:
    job = finished(env, "o.bin", b"z" * 100)
    _phone, _pin, client = service_login(env)
    assert fetch_start(client, job.id, offset=101)["code"] == "conflict"
    start = fetch_start(client, job.id, offset=100)
    assert start["length"] == 0
    data, end = receive(client, start, since=len(client.frames))
    assert data == b"" and end["status"] == "complete" and end["nextOffset"] == 100
    empty = finished(env, "empty.bin", b"")
    start = fetch_start(client, empty.id)
    assert start["size"] == 0
    assert receive(client, start, since=len(client.frames))[1]["status"] == "complete"


def test_unfinished_unknown_and_missing_jobs(env: Env) -> None:
    """QA-40: only a completed job's one regular file is ever served."""
    running = finished(env, "running.bin", b"x" * 10, JobStatus.DOWNLOADING)
    gone = finished(env, "gone.bin", b"x")
    (env.dest / "gone.bin").unlink()
    folder = env.db.create_job(URL, str(env.dest), "folder")
    env.db.set_job_status(folder.id, JobStatus.COMPLETED, None)
    (env.dest / "folder").mkdir()
    _phone, _pin, client = service_login(env)
    assert fetch_start(client, running.id)["code"] == "conflict"
    assert fetch_start(client, 999_999)["code"] == "notFound"
    assert fetch_start(client, gone.id)["code"] == "notFound"
    assert fetch_start(client, folder.id)["code"] == "notFound"
    assert not binary_frames(client, 0)


def test_a_path_in_place_of_a_job_id_is_refused(env: Env) -> None:
    job = finished(env, "p.bin", b"secret")
    _phone, _pin, client = service_login(env)
    target = str(env.dest / "p.bin")
    reply = ask(client, {"id": rid(), "type": "fetch", "path": target})
    assert reply["code"] == "forbidden" and "secret" not in json.dumps(reply)
    reply = ask(client, {"id": rid(), "type": "fetch", "jobId": job.id, "filename": "x"})
    assert reply["code"] == "forbidden"
    for bad in (target, str(job.id), [job.id], 1.5, True):
        assert fetch_start(client, bad)["code"] == "badRequest"
    assert not binary_frames(client, 0)


def test_one_stream_per_connection(env: Env) -> None:
    job = finished(env, "two.bin", os.urandom(12 * MIB))
    _phone, _pin, client = service_login(env)
    since = len(client.frames)
    start = fetch_start(client, job.id)
    assert start["type"] == "fetchStart"
    assert fetch_start(client, job.id)["code"] == "conflict"
    data, end = receive(client, start, since=since)
    assert len(data) == 12 * MIB and end["status"] == "complete"
    assert fetch_start(client, job.id)["type"] == "fetchStart"  # free again


def test_cancel_ends_the_stream(env: Env) -> None:
    job = finished(env, "cancel.bin", os.urandom(20 * MIB))
    _phone, _pin, client = service_login(env)
    start = fetch_start(client, job.id)
    assert pump(lambda: len(binary_frames(client, 0)) >= 4)
    reply = ask(client, {"id": rid(), "type": "fetchCancel", "streamId": start["streamId"]})
    assert reply["type"] == "ok" and reply["protocolVersion"] == 6
    end = client.message()
    assert end["type"] == "fetchEnd" and end["status"] == "cancelled"
    assert end["streamId"] == start["streamId"] and end["nextOffset"] == 0
    settle(0.2)
    count = len(binary_frames(client, 0))
    settle(0.4)
    assert len(binary_frames(client, 0)) == count  # nothing more is sent
    # An unknown or finished stream is skipped, not an error.
    reply = ask(client, {"id": rid(), "type": "fetchCancel", "streamId": 99})
    assert reply["type"] == "ok"
    assert quiet(client)
    assert fetch_start(client, job.id, offset=0)["type"] == "fetchStart"


def test_no_ack_for_sixty_seconds_fails_the_stream(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = finished(env, "stall.bin", os.urandom(20 * MIB))
    _phone, _pin, client = service_login(env)
    start = fetch_start(client, job.id)
    assert pump(lambda: sum(len(p) - 16 for p in binary_frames(client, 0)) >= 8 * MIB)
    clock = ShiftedTime()
    monkeypatch.setattr(listener_module, "time", clock)
    clock.monotonic.offset = 50.0
    assert quiet(client, 0.6)  # not yet
    clock.monotonic.offset = 61.0
    end = client.message()
    assert end["type"] == "fetchEnd" and end["status"] == "error" and end["code"] == "failed"
    assert end["streamId"] == start["streamId"] and end["nextOffset"] == 0


def test_an_ack_moves_the_stall_clock(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    job = finished(env, "slow.bin", os.urandom(20 * MIB))
    _phone, _pin, client = service_login(env)
    start = fetch_start(client, job.id)
    assert pump(lambda: sum(len(p) - 16 for p in binary_frames(client, 0)) >= 8 * MIB)
    clock = ShiftedTime()
    monkeypatch.setattr(listener_module, "time", clock)
    clock.monotonic.offset = 50.0
    client.send({"type": "fetchAck", "streamId": start["streamId"], "received": 2 * MIB})
    client.send({"id": rid(), "type": "ping"})  # keeps the idle timer fresh too
    assert client.message()["type"] == "pong"
    clock.monotonic.offset = 100.0  # 50 s after the ack
    client.send({"id": rid(), "type": "ping"})
    assert client.message()["type"] == "pong"
    assert quiet(client, 0.6)


# ---------------------------------------------------------------- fetchAck


def test_acks_are_silent_free_and_capped(env: Env) -> None:
    job = finished(env, "acks.bin", os.urandom(20 * MIB))
    _phone, _pin, client = service_login(env)
    # 45 acks for no stream: no reply, no tokens spent (the burst is 40).
    for _ in range(45):
        client.send({"type": "fetchAck", "streamId": 1, "received": 0})
    client.send({"type": "fetchAck", "streamId": "x", "received": -1})
    client.send({"id": "a-1", "type": "fetchAck", "streamId": 1, "received": 0})
    assert quiet(client, 0.5)
    assert ask(client, {"id": rid(), "type": "ping"})["type"] == "pong"
    settle(1.1)  # the per-second ack window empties
    start = fetch_start(client, job.id)
    assert pump(lambda: sum(len(p) - 16 for p in binary_frames(client, 0)) >= 8 * MIB)
    # 50 acks a second are heard; the 51st within the second is ignored.
    for _ in range(fetch.ACKS_PER_SECOND):
        client.send({"type": "fetchAck", "streamId": start["streamId"], "received": 0})
    client.send({"type": "fetchAck", "streamId": start["streamId"], "received": 8 * MIB})
    settle(0.4)
    assert sum(len(p) - 16 for p in binary_frames(client, 0)) == 8 * MIB
    settle(1.1)
    client.send({"type": "fetchAck", "streamId": start["streamId"], "received": 8 * MIB})
    assert pump(lambda: sum(len(p) - 16 for p in binary_frames(client, 0)) > 8 * MIB)


# ---------------------------------------------------------------- fileHash


def test_file_hash_matches_and_is_cached(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = os.urandom(5 * MIB + 7)
    job = finished(env, "h.bin", payload)
    _phone, _pin, client = service_login(env)
    first = ask(client, {"id": rid(), "type": "fileHash", "jobId": job.id})
    assert first["type"] == "fileHash" and first["jobId"] == job.id
    assert first["sha256"] == hashlib.sha256(payload).hexdigest()
    assert first["size"] == len(payload)
    start = fetch_start(client, job.id, length=1)
    assert start["etag"] == first["etag"]
    receive(client, start, since=0)
    reads: list[int] = []
    real_open = open

    def counting_open(*args: Any, **kwargs: Any) -> Any:
        reads.append(1)
        return real_open(*args, **kwargs)

    monkeypatch.setattr("app.remote.fetch.open", counting_open, raising=False)
    again = ask(client, {"id": rid(), "type": "fileHash", "jobId": job.id})
    assert again["sha256"] == first["sha256"] and not reads  # served from the cache
    # A changed file is hashed again.
    new = os.urandom(1000)
    (env.dest / "h.bin").write_bytes(new)
    third = ask(client, {"id": rid(), "type": "fileHash", "jobId": job.id})
    assert third["sha256"] == hashlib.sha256(new).hexdigest() and third["size"] == 1000


def test_file_hash_refusals(env: Env) -> None:
    running = finished(env, "hr.bin", b"x", JobStatus.DOWNLOADING)
    _phone, _pin, client = service_login(env)
    assert ask(client, {"id": rid(), "type": "fileHash", "jobId": running.id})["code"] == "conflict"
    assert ask(client, {"id": rid(), "type": "fileHash", "jobId": 12345})["code"] == "notFound"
    assert ask(client, {"id": rid(), "type": "fileHash", "path": "C:/x"})["code"] == "forbidden"


# ------------------------------------------------------------ files, jobs


def test_files_lists_finished_single_files_and_pages(env: Env) -> None:
    jobs = [finished(env, f"f{n}.mp4", b"x" * (n + 1)) for n in range(5)]
    finished(env, "running.bin", b"x", JobStatus.DOWNLOADING)
    folder = env.db.create_job(URL, str(env.dest), "folder")
    env.db.set_job_status(folder.id, JobStatus.COMPLETED, None)
    (env.dest / "folder").mkdir()
    gone = finished(env, "gone.bin", b"x")
    (env.dest / "gone.bin").unlink()
    _phone, _pin, client = service_login(env)
    page = ask(client, {"id": rid(), "type": "files", "limit": 2})
    assert page["type"] == "files" and len(page["files"]) == 2 and page["nextCursor"]
    first = page["files"][0]
    assert set(first) == {"jobId", "name", "size", "finishedAt", "mime"}
    assert first["jobId"] == jobs[4].id and first["name"] == "f4.mp4" and first["size"] == 5
    assert first["mime"] == "video/mp4" and isinstance(first["finishedAt"], int)
    seen = [f["jobId"] for f in page["files"]]
    while page["nextCursor"]:
        page = ask(client, {"id": rid(), "type": "files", "cursor": page["nextCursor"]})
        seen += [f["jobId"] for f in page["files"]]
    assert seen == [j.id for j in reversed(jobs)]
    assert gone.id not in seen and folder.id not in seen
    assert "path" not in json.dumps(page) and str(env.dest) not in json.dumps(page)


def test_job_objects_say_which_jobs_are_fetchable(env: Env) -> None:
    done = finished(env, "d.bin", b"x")
    running = finished(env, "r.bin", b"x", JobStatus.DOWNLOADING)
    missing = finished(env, "m.bin", b"x")
    (env.dest / "m.bin").unlink()
    _phone, _pin, client = service_login(env)
    jobs = {j["id"]: j for j in ask(client, {"id": rid(), "type": "jobs"})["jobs"]}
    assert jobs[done.id]["fetchable"] is True
    assert jobs[running.id]["fetchable"] is False and jobs[missing.id]["fetchable"] is False


# ------------------------------------------------------------------ outbox


def subscribe(client: Client, *topics: str) -> None:
    assert ask(client, {"id": rid(), "type": "subscribe", "topics": list(topics)})["topics"]


def test_outbox_add_event_list_and_ack(env: Env) -> None:
    phone, _pin, client = service_login(env)
    subscribe(client, "outbox")
    link = "https://example.com/files/manual.pdf"
    item = env.service.queue_for_phone(phone.device_id, link, "manual.pdf")
    assert item is not None
    event = client.message()
    assert event["type"] == "event" and event["event"] == "outbox.added"
    assert event["count"] == 1 and event["seq"] == 1 and "_device" not in event
    listing = ask(client, {"id": rid(), "type": "outbox"})
    assert listing["type"] == "outbox" and len(listing["items"]) == 1
    row = listing["items"][0]
    assert row == {
        "itemId": item,
        "url": link,
        "title": "manual.pdf",
        "quality": None,
        "createdAt": row["createdAt"],
    }
    # Unacknowledged items are offered again.
    assert ask(client, {"id": rid(), "type": "outbox"})["items"] == listing["items"]
    acked = ask(client, {"id": rid(), "type": "outboxAck", "itemIds": [item, item, 777]})
    assert acked["type"] == "ok" and acked["removed"] == 1
    assert ask(client, {"id": rid(), "type": "outbox"})["items"] == []
    again = ask(client, {"id": rid(), "type": "outboxAck", "itemIds": [item]})
    assert again["type"] == "ok" and again["removed"] == 0  # skipped, not an error


def test_outbox_is_per_device_or_any_phone(env: Env) -> None:
    one, _pin, first = service_login(env)
    env.started.clear()
    env.pending.clear()
    two, pin2 = env.pair()
    second = env.login(two, pin2)
    subscribe(first, "outbox")
    for_one = env.service.queue_for_phone(one.device_id, "https://example.com/one")
    for_two = env.service.queue_for_phone(two.device_id, "https://example.com/two")
    anyone = env.service.queue_for_phone(None, "magnet:?xt=urn:btih:abc", "A torrent")
    assert None not in (for_one, for_two, anyone)
    events = [first.message(), first.message()]  # one for its own item, one for "any"
    assert [e["count"] for e in events] == [1, 2]
    assert {i["itemId"] for i in ask(first, {"id": rid(), "type": "outbox"})["items"]} == {
        for_one,
        anyone,
    }
    assert {i["itemId"] for i in ask(second, {"id": rid(), "type": "outbox"})["items"]} == {
        for_two,
        anyone,
    }
    # One phone cannot clear another's item.
    assert ask(first, {"id": rid(), "type": "outboxAck", "itemIds": [for_two]})["removed"] == 0
    assert ask(second, {"id": rid(), "type": "outbox"})["items"][0]["itemId"] == for_two
    # An "any phone" item is gone for everyone once one phone acks it.
    assert ask(first, {"id": rid(), "type": "outboxAck", "itemIds": [anyone]})["removed"] == 1
    assert [i["itemId"] for i in ask(second, {"id": rid(), "type": "outbox"})["items"]] == [for_two]


def test_outbox_refuses_links_the_protocol_cannot_carry(env: Env) -> None:
    phone, _pin, _client = service_login(env)
    assert env.service.queue_for_phone(phone.device_id, "file:///etc/passwd") is None
    assert env.service.queue_for_phone(phone.device_id, "javascript:alert(1)") is None
    assert env.service.queue_for_phone("no-such-device", "https://example.com/a") is None
    assert env.service.handlers.outbox.count_for(phone.device_id) == 0


def test_removing_a_device_drops_its_outbox(env: Env) -> None:
    phone, _pin, _client = service_login(env)
    env.service.queue_for_phone(phone.device_id, "https://example.com/a")
    assert env.service.handlers.outbox.count_for(phone.device_id) == 1
    assert env.service.remove_device(phone.device_id)
    assert env.service.handlers.outbox.items_for(phone.device_id) == []


def test_outbox_table_rules(env: Env) -> None:
    box = Outbox(env.db)
    first = box.add("a", "https://example.com/1")
    second = box.add(None, "https://example.com/2", "T", "720p")
    assert [i.item_id for i in box.items_for("a")] == [first, second]
    assert [i.item_id for i in box.items_for("b")] == [second]
    assert box.items_for("b")[0].quality == "720p"
    assert box.ack("b", [first, second, second, 99]) == 1
    assert box.ack("a", [first]) == 1 and box.count_for("a") == 0


def test_phase4_features_are_advertised(env: Env) -> None:
    phone, pin = env.pair()
    client, nonce = env.connect(pin)
    client.send(phone.auth_request(pin, nonce))
    features = client.message()["features"]
    assert {"files", "fetch", "outbox"} <= set(features)
