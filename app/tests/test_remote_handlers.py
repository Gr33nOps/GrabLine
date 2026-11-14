"""RemoteHandlers against a real database and manager (no real downloads)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.core.manager import DownloadManager
from app.core.models import Job, JobStatus
from app.db.database import Database
from app.remote import folders
from app.remote.handlers import (
    DUPLICATE_WINDOW_S,
    LiveFigures,
    RemoteHandlers,
    job_object,
    strip_paths,
    strip_userinfo,
)
from app.remote.requests_log import RequestLog
from app.remote.schema import RequestError, validate

DEVICE = "KsucBZHeyJguN6H0BzJQiw"
OTHER = "uD_okqTpI9aCLgizY9Xbdg"
URL = "https://example.com/files/a.iso"


class Clock:
    def __init__(self) -> None:
        self.now = 1_794_700_000.0

    def __call__(self) -> float:
        return self.now


class Env:
    def __init__(self, db: Database, manager: DownloadManager, tmp: Path) -> None:
        self.db = db
        self.manager = manager
        self.tmp = tmp
        self.clock = Clock()
        self.handlers = self.new_handlers()

    def new_handlers(self) -> RemoteHandlers:
        return RemoteHandlers(
            self.manager,
            self.manager.settings,
            RequestLog(self.db, clock=self.clock),
            identity=lambda: ("A3D7Za2aw26jbGofVV3ZAQ", "Study PC"),
            live=lambda _job_id: LiveFigures(speed=100, eta=7),
            clock=self.clock,
        )

    def call(self, kind: str, device: str = DEVICE, **fields: Any) -> dict[str, Any]:
        request = validate({"id": "r-1", "type": kind, **fields})
        out: list[dict[str, Any]] = []
        errors: list[RequestError] = []
        self.handlers.handle(device, request, out.append, errors.append)
        if errors:
            raise errors[0]
        assert len(out) == 1
        return out[0]

    def job(self, status: JobStatus, *, name: str = "f.bin", error: str | None = None) -> Job:
        dest = self.tmp / "downloads"
        dest.mkdir(exist_ok=True)
        job = self.db.create_job(f"https://example.com/{name}", str(dest), name)
        self.db.set_job_status(job.id, status, error)
        return job

    def status(self, job_id: int) -> JobStatus:
        job = self.db.get_job(job_id)
        assert job is not None
        return job.status


@pytest.fixture()
def env(db: Database, tmp_path: Path) -> Iterator[Env]:
    manager = DownloadManager(db, max_concurrent=0)
    manager.settings.download_dir = tmp_path / "downloads"
    try:
        yield Env(db, manager, tmp_path)
    finally:
        manager.shutdown()


def rid(n: int) -> str:
    return f"req-{n:04d}-0123456789abcdef"


# --------------------------------------------------------------- download


def test_download_inserts_a_phone_handoff(env: Env):
    reply = env.call("download", url=URL, requestId=rid(1), title="T", quality="720p")
    assert reply["type"] == "queued" and reply["duplicate"] is False
    assert reply["requestId"] == rid(1) and reply["handoffUrl"] == URL
    assert reply["previousJobId"] == 0 and reply["appRunning"] is True
    handoff = env.db.claim_handoffs()[0]
    assert handoff.id == reply["handoffId"]
    assert handoff.source == "phone"
    assert handoff.headers == {}
    assert handoff.quality == "720p"
    assert handoff.options == {
        "requestId": rid(1),
        "deviceId": DEVICE,
        "queueId": None,
        "folderId": None,
        "title": "T",
        "quality": "720p",
        "unattended": True,
    }


def test_download_with_known_queue_and_folder(env: Env):
    queue = env.manager.create_queue("Night")
    fav = env.tmp / "Movies"
    fav.mkdir()
    env.manager.settings.favorite_folders = [str(fav)]
    movies = next(f for f in folders.list_folders(env.manager.settings, env.db) if not f.is_default)
    env.call("download", url=URL, requestId=rid(1), queueId=queue.id, folderId=movies.id)
    options = env.db.claim_handoffs()[0].options
    assert options["queueId"] == queue.id and options["folderId"] == movies.id


def test_previous_job_id_is_reported(env: Env):
    job = env.db.create_job(URL, str(env.tmp), "a.iso")
    assert env.call("download", url=URL, requestId=rid(1))["previousJobId"] == job.id


def test_same_request_id_is_a_duplicate_with_the_first_handoff(env: Env):
    first = env.call("download", url=URL, requestId=rid(1))
    env.clock.now += 3600  # well past the 5 s window: only the requestId matters
    again = env.call("download", url=URL, requestId=rid(1))
    assert again["duplicate"] is True and again["handoffId"] == first["handoffId"]
    assert len(env.db.claim_handoffs()) == 1


def test_duplicate_request_id_survives_a_restart(env: Env):
    first = env.call("download", url=URL, requestId=rid(1))
    env.handlers = env.new_handlers()  # new instance, same database
    again = env.call("download", url=URL, requestId=rid(1))
    assert again["duplicate"] is True and again["handoffId"] == first["handoffId"]
    assert len(env.db.claim_handoffs()) == 1


def test_request_id_memory_is_per_device(env: Env):
    first = env.call("download", url=URL, requestId=rid(1))
    other = env.call("download", device=OTHER, url=URL, requestId=rid(1))
    assert other["duplicate"] is False and other["handoffId"] != first["handoffId"]


def test_same_link_within_five_seconds_is_a_duplicate(env: Env):
    first = env.call("download", url=URL, requestId=rid(1), quality="1080p")
    env.clock.now += DUPLICATE_WINDOW_S - 0.5
    second = env.call("download", url=URL, requestId=rid(2), quality="1080p")
    assert second["duplicate"] is True and second["handoffId"] == first["handoffId"]
    assert len(env.db.claim_handoffs()) == 1
    # The second requestId is remembered too: a retry of it is a plain duplicate later.
    env.clock.now += 100
    third = env.call("download", url=URL, requestId=rid(2), quality="1080p")
    assert third["duplicate"] is True and third["handoffId"] == first["handoffId"]


def test_same_link_after_five_seconds_is_new(env: Env):
    first = env.call("download", url=URL, requestId=rid(1))
    env.clock.now += DUPLICATE_WINDOW_S + 0.1
    second = env.call("download", url=URL, requestId=rid(2))
    assert second["duplicate"] is False and second["handoffId"] != first["handoffId"]


def test_different_target_is_not_a_duplicate(env: Env):
    queue = env.manager.create_queue("Q")
    env.call("download", url=URL, requestId=rid(1))
    assert env.call("download", url=URL, requestId=rid(2), quality="mp3")["duplicate"] is False
    assert env.call("download", url=URL, requestId=rid(3), queueId=queue.id)["duplicate"] is False
    assert env.call("download", url=URL + "x", requestId=rid(4))["duplicate"] is False
    assert env.call("download", device=OTHER, url=URL, requestId=rid(5))["duplicate"] is False


def test_unknown_queue_and_folder_are_not_found(env: Env):
    with pytest.raises(RequestError) as info:
        env.call("download", url=URL, requestId=rid(1), queueId=999)
    assert info.value.code == "notFound"
    with pytest.raises(RequestError) as info:
        env.call("download", url=URL, requestId=rid(1), folderId="fTm90SXNzdWVk")
    assert info.value.code == "notFound"
    assert env.db.claim_handoffs() == []


# ------------------------------------------------------------------ links


def test_links_make_one_phone_links_handoff(env: Env):
    urls = ["https://example.com/a.zip", "https://example.com/b.zip"]
    reply = env.call("links", urls=urls, requestId=rid(1), pageUrl="https://example.com/p")
    assert reply == {
        "type": "queued",
        "count": 2,
        "appRunning": True,
        "requestId": rid(1),
        "handoffId": reply["handoffId"],
        "duplicate": False,
    }
    handoffs = env.db.claim_handoffs()
    assert len(handoffs) == 1
    assert handoffs[0].source == "phone-links"
    assert list(handoffs[0].payload) == urls
    assert handoffs[0].options["deviceId"] == DEVICE
    assert handoffs[0].headers == {}
    again = env.call("links", urls=urls, requestId=rid(1))
    assert again["duplicate"] is True and again["handoffId"] == reply["handoffId"]


def test_links_unknown_queue_is_not_found(env: Env):
    with pytest.raises(RequestError) as info:
        env.call("links", urls=[URL], requestId=rid(1), queueId=42)
    assert info.value.code == "notFound"


# ------------------------------------------------------------------- jobs


def test_jobs_paging_cursor_total_and_order(env: Env):
    ids = [env.job(JobStatus.QUEUED, name=f"f{n}.bin").id for n in range(7)]
    page1 = env.call("jobs", limit=3)
    assert [j["id"] for j in page1["jobs"]] == ids[::-1][:3]
    assert page1["total"] == 7 and page1["nextCursor"]
    page2 = env.call("jobs", limit=3, cursor=page1["nextCursor"])
    assert [j["id"] for j in page2["jobs"]] == ids[::-1][3:6]
    page3 = env.call("jobs", limit=3, cursor=page2["nextCursor"])
    assert [j["id"] for j in page3["jobs"]] == ids[::-1][6:]
    assert page3["nextCursor"] is None
    # A job added meanwhile does not shift the pages already walked.
    env.job(JobStatus.QUEUED, name="new.bin")
    assert [j["id"] for j in env.call("jobs", limit=3, cursor=page1["nextCursor"])["jobs"]] == [
        j["id"] for j in page2["jobs"]
    ]
    exact = env.call("jobs", limit=8)
    assert exact["nextCursor"] is None and len(exact["jobs"]) == 8


def test_jobs_filters(env: Env):
    queued = env.job(JobStatus.QUEUED, name="q").id
    running = env.job(JobStatus.DOWNLOADING, name="d").id
    paused = env.job(JobStatus.PAUSED, name="p").id
    done = env.job(JobStatus.COMPLETED, name="c").id
    failed = env.job(JobStatus.FAILED, name="f").id
    cancelled = env.job(JobStatus.CANCELLED, name="x").id

    def ids(flt: str) -> set[int]:
        reply = env.call("jobs", filter=flt)
        assert reply["total"] == len(reply["jobs"])
        return {j["id"] for j in reply["jobs"]}

    assert ids("all") == {queued, running, paused, done, failed, cancelled}
    assert ids("active") == {queued, running, paused}
    assert ids("finished") == {done}
    assert ids("failed") == {failed, cancelled}


def test_recent_returns_job_objects_newest_first(env: Env):
    first = env.job(JobStatus.QUEUED, name="a").id
    second = env.job(JobStatus.QUEUED, name="b").id
    reply = env.call("recent", limit=2)
    assert [j["id"] for j in reply["jobs"]] == [second, first]
    assert reply["appRunning"] is True


def test_job_carries_the_creating_devices_request_id(env: Env):
    job = env.job(JobStatus.QUEUED)
    log = RequestLog(env.db, clock=env.clock)
    log.record_jobs(DEVICE, rid(9), [job.id])
    assert env.call("jobs")["jobs"][0]["requestId"] == rid(9)
    assert env.call("jobs", device=OTHER)["jobs"][0]["requestId"] is None


# ---------------------------------------------------------------- control

ALL = list(JobStatus)


def _control(env: Env, status: JobStatus, action: str) -> tuple[str, JobStatus | None, int]:
    job = env.job(status)
    try:
        reply = env.call("control", jobId=job.id, action=action)
    except RequestError as exc:
        return exc.code, env.status(job.id), job.id
    assert reply["type"] == "ok"
    if action == "remove":
        assert reply["job"] is None
        return "ok", None, job.id
    assert reply["job"]["id"] == job.id
    return "ok", env.status(job.id), job.id


# (action, from status) -> (outcome, resulting status or None when unchecked/gone)
S = JobStatus
CELLS: dict[tuple[str, JobStatus], tuple[str, JobStatus | None]] = {
    ("pause", S.QUEUED): ("ok", S.PAUSED),
    ("pause", S.DOWNLOADING): ("ok", None),  # no worker here to stop
    ("pause", S.PAUSED): ("ok", S.PAUSED),
    ("pause", S.COMPLETED): ("conflict", S.COMPLETED),
    ("pause", S.FAILED): ("conflict", S.FAILED),
    ("pause", S.CANCELLED): ("conflict", S.CANCELLED),
    ("resume", S.QUEUED): ("ok", S.QUEUED),
    ("resume", S.DOWNLOADING): ("ok", S.DOWNLOADING),
    ("resume", S.PAUSED): ("ok", S.QUEUED),
    ("resume", S.COMPLETED): ("conflict", S.COMPLETED),
    ("resume", S.FAILED): ("conflict", S.FAILED),
    ("resume", S.CANCELLED): ("conflict", S.CANCELLED),
    ("cancel", S.QUEUED): ("ok", S.CANCELLED),
    ("cancel", S.DOWNLOADING): ("ok", None),
    ("cancel", S.PAUSED): ("ok", S.CANCELLED),
    ("cancel", S.CANCELLED): ("ok", S.CANCELLED),
    ("cancel", S.COMPLETED): ("conflict", S.COMPLETED),
    ("cancel", S.FAILED): ("conflict", S.FAILED),
    ("retry", S.FAILED): ("ok", S.QUEUED),
    ("retry", S.CANCELLED): ("ok", S.QUEUED),
    ("retry", S.QUEUED): ("ok", S.QUEUED),
    ("retry", S.DOWNLOADING): ("ok", S.DOWNLOADING),
    ("retry", S.PAUSED): ("conflict", S.PAUSED),
    ("retry", S.COMPLETED): ("conflict", S.COMPLETED),
}


CELL_IDS: list[tuple[str, JobStatus]] = sorted(CELLS, key=lambda k: f"{k[0]}-{k[1].value}")


@pytest.mark.parametrize(("action", "status"), CELL_IDS)
def test_control_transition_table(env: Env, action: str, status: JobStatus):
    outcome, after = CELLS[(action, status)]
    got_outcome, got_status, _ = _control(env, status, action)
    assert got_outcome == outcome
    if after is not None:
        assert got_status is after


def test_control_table_covers_every_non_remove_cell():
    for action in ("pause", "resume", "cancel", "retry"):
        allowed = {s for (a, s) in CELLS if a == action}
        assert allowed <= set(ALL)
    # Every cell of the protocol table that the spec lists as allowed is present.
    assert ("pause", S.QUEUED) in CELLS and ("retry", S.CANCELLED) in CELLS


@pytest.mark.parametrize("status", [S.QUEUED, S.PAUSED, S.FAILED, S.CANCELLED, S.COMPLETED])
def test_remove_drops_the_row(env: Env, status: JobStatus):
    outcome, after, job_id = _control(env, status, "remove")
    assert outcome == "ok" and after is None
    assert env.db.get_job(job_id) is None


def test_remove_keeps_a_completed_file_on_disk(env: Env):
    job = env.job(JobStatus.COMPLETED, name="keep.bin")
    target = Path(job.dest_dir) / "keep.bin"
    target.write_bytes(b"data")
    env.call("control", jobId=job.id, action="remove")
    assert env.db.get_job(job.id) is None
    assert target.read_bytes() == b"data"


def test_control_unknown_job_is_not_found(env: Env):
    for action in ("pause", "remove", "retry"):
        with pytest.raises(RequestError) as info:
            env.call("control", jobId=999_999, action=action)
        assert info.value.code == "notFound"


# ----------------------------------------------------------------- global


def test_global_pause_resume_and_speed_limit(env: Env):
    queued = env.job(JobStatus.QUEUED).id
    paused = env.job(JobStatus.PAUSED).id
    state = env.call("global", action="get")["state"]
    assert state == {"paused": False, "speedLimit": None, "speed": 0, "active": 0, "queued": 1}

    state = env.call("global", action="pauseAll")["state"]
    assert env.status(queued) is JobStatus.PAUSED
    assert state["paused"] is True and state["queued"] == 0

    state = env.call("global", action="resumeAll")["state"]
    assert env.status(queued) is JobStatus.QUEUED and env.status(paused) is JobStatus.QUEUED
    assert state["paused"] is False and state["queued"] == 2

    state = env.call("global", action="setSpeedLimit", bytesPerSecond=2_097_152)["state"]
    assert state["speedLimit"] == 2_097_152
    assert env.manager.settings.speed_limit_kbps == 2048
    state = env.call("global", action="clearSpeedLimit")["state"]
    assert state["speedLimit"] is None and env.manager.settings.speed_limit_kbps == 0


def test_speed_limit_rounds_down_but_never_to_zero(env: Env):
    env.call("global", action="setSpeedLimit", bytesPerSecond=1024)
    assert env.manager.settings.speed_limit_kbps == 1
    env.call("global", action="setSpeedLimit", bytesPerSecond=1500)
    assert env.manager.settings.speed_limit_kbps == 1


def test_global_speed_sums_downloading_jobs(env: Env):
    env.job(JobStatus.DOWNLOADING)
    env.job(JobStatus.DOWNLOADING)
    state = env.call("global", action="get")["state"]
    assert state["active"] == 2 and state["speed"] == 200


# ----------------------------------------------------------------- queues


def test_queues_reply_never_contains_a_path(env: Env):
    env.manager.create_queue("Night")
    favs = []
    for name in ("Movies", "Music"):
        path = env.tmp / "deep" / "nest" / name
        path.mkdir(parents=True)
        favs.append(str(path))
    other = env.tmp / "other" / "Movies"  # same last name: gets its parent's name
    other.mkdir(parents=True)
    env.manager.settings.favorite_folders = [*favs, str(other)]
    reply = env.call("queues")
    assert reply["queues"][0]["name"] == "Night"
    assert len(reply["folders"]) == 4
    assert sum(1 for f in reply["folders"] if f["isDefault"]) == 1
    text = json.dumps(reply)
    assert str(env.tmp) not in text and str(env.tmp).replace("\\", "\\\\") not in text
    for folder in reply["folders"]:
        for value in (folder["id"], folder["name"]):
            assert "\\" not in value and ":" not in value and not value.startswith("/")
        assert folder["id"].startswith("f") and len(folder["id"]) <= 32
    names = {f["name"] for f in reply["folders"]}
    assert names == {"downloads", "Music", "nest/Movies", "other/Movies"}


def test_folder_ids_resolve_and_forged_ids_do_not(env: Env):
    fav = env.tmp / "Fav"
    fav.mkdir()
    env.manager.settings.favorite_folders = [str(fav)]
    listed = env.call("queues")["folders"]
    settings = env.manager.settings
    resolved = {f["id"]: folders.resolve_folder(settings, env.db, f["id"]) for f in listed}
    assert Path(str(resolved[listed[0]["id"]])) == settings.download_dir
    assert all(p is not None for p in resolved.values())
    assert any(p is not None and p.name == "Fav" for p in resolved.values())
    # QA-08: forged ids for real folders the PC did not list.
    other = env.tmp / "Unlisted"
    other.mkdir()
    forged_secret = folders.folder_id(b"\x00" * 32, other)
    for forged in (forged_secret, "fTm90SXNzdWVk", "f" + "A" * 14, str(other), "../Fav"):
        assert folders.resolve_folder(settings, env.db, forged) is None
    # Removing the favourite stops its id resolving at once.
    fav_id = next(f["id"] for f in listed if f["name"] == "Fav")
    settings.favorite_folders = []
    assert folders.resolve_folder(settings, env.db, fav_id) is None


def test_folder_ids_are_stable_across_calls(env: Env):
    assert env.call("queues")["folders"] == env.call("queues")["folders"]


# ------------------------------------------------------------- job object


def test_job_object_strips_credentials_from_urls(env: Env):
    job = env.db.create_job("https://user:pa55@example.com:8443/x.bin?a=1", str(env.tmp), "x.bin")
    out = env.call("jobs")["jobs"][0]
    assert out["id"] == job.id
    assert out["url"] == "https://example.com:8443/x.bin?a=1"
    assert "pa55" not in json.dumps(out) and "user" not in out["url"]


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("https://u:p@h.example/a", "https://h.example/a"),
        ("https://u@h.example/a", "https://h.example/a"),
        ("https://[::1]:9/a", "https://[::1]:9/a"),
        ("https://u:p@[::1]:9/a", "https://[::1]:9/a"),
        ("https://h.example/a@b", "https://h.example/a@b"),
    ],
)
def test_strip_userinfo(raw: str, clean: str):
    assert strip_userinfo(raw) == clean


def test_job_object_strips_paths_from_errors_and_names(env: Env):
    job = env.job(
        JobStatus.FAILED,
        name="x.bin",
        error="cannot write C:\\Users\\me\\Docs\\x.bin: access denied (/home/me/dl/x.bin)",
    )
    out = env.call("jobs")["jobs"][0]
    assert out["id"] == job.id and out["status"] == "failed"
    assert "Users" not in out["error"] and "home" not in out["error"]
    assert "access denied" in out["error"]
    assert out["finishedAt"] is not None


def test_error_only_on_failed_jobs_and_name_never_a_path(env: Env):
    env.db.create_job("https://e.example/t", str(env.tmp), "n.bin", title="C:\\secret\\dir\\t.mp4")
    out = env.call("jobs")["jobs"][0]
    assert out["error"] is None and out["finishedAt"] is None
    assert "secret" not in out["name"] and "\\" not in out["name"]
    assert strip_paths("\\\\server\\share\\f.txt failed") == "… failed"
    assert strip_paths(None) is None and strip_paths("   ") is None


def test_job_object_fields_and_live_figures(env: Env):
    running = env.job(JobStatus.DOWNLOADING)
    queued = env.job(JobStatus.QUEUED)
    by_id = {j["id"]: j for j in env.call("jobs")["jobs"]}
    assert by_id[running.id]["speed"] == 100 and by_id[running.id]["eta"] == 7
    assert by_id[queued.id]["speed"] == 0 and by_id[queued.id]["eta"] is None
    assert set(by_id[queued.id]) == {
        "id", "url", "name", "status", "kind", "downloaded", "total", "speed", "eta",
        "queueId", "createdAt", "finishedAt", "error", "requestId", "fetchable",
    }  # fmt: skip
    assert by_id[queued.id]["fetchable"] is False and by_id[queued.id]["createdAt"] > 0


def test_job_object_function_clamps_downloaded(env: Env):
    job = env.job(JobStatus.QUEUED)
    out = job_object(job, -5, None, None, LiveFigures(), request_id=None)
    assert out["downloaded"] == 0 and out["createdAt"] == 0 and out["total"] is None


# ------------------------------------------------------------------ misc


def test_ping_status_and_focus(env: Env):
    pong = env.call("ping")
    assert pong["type"] == "pong" and pong["pcName"] == "Study PC" and pong["appRunning"] is True
    assert pong["serverTime"] == int(env.clock.now)
    env.job(JobStatus.QUEUED, name="s.bin")
    status = env.call("status", urls=["https://example.com/s.bin", "https://example.com/none"])
    assert status["jobs"][0]["status"] == "queued" and status["jobs"][1]["status"] == "pending"
    assert env.call("focus", target="settings") == {"type": "ok", "appRunning": True}
    assert env.db.claim_handoffs()[0].source == "focus"


def test_analyze_without_a_backend_is_unsupported(env: Env):
    with pytest.raises(RequestError) as info:
        env.call("analyze", url=URL)
    assert info.value.code == "unsupported"


def test_unexpected_exceptions_become_internal(env: Env, monkeypatch: pytest.MonkeyPatch):
    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("secret path C:\\x")

    monkeypatch.setattr(env.db, "list_jobs", boom)
    with pytest.raises(RequestError) as info:
        env.call("jobs")
    assert info.value.code == "internal" and "secret" not in info.value.message
