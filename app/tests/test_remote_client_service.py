"""This PC's own downloads and the host's commands (PROTOCOL.md sections 20
and 21), and the live view of a host's jobs, tested on ``LocalDownloads`` and
``LinkService`` with a stub connection so every message is visible."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from app.core.manager import DownloadManager
from app.core.models import JobStatus
from app.core.settings import Settings
from app.db.database import Database
from app.remote.client import local, session
from app.remote.client.local import LocalDownloads, MovePlan, Outcome
from app.remote.client.service import LinkService, _Link, _row, host_job
from app.remote.client.session import LinkRecord
from app.tests.qt_app import ensure_app
from app.tests.remote_client import pump

pytestmark = pytest.mark.timeout(60)

URL = "https://user:secret@example.com/dir/file.zip"


class StubRunner:
    """Stands in for ``LinkRunner``: records what the service sends."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.alive = True
        self.stopped = False

    def send(self, message: dict[str, Any]) -> bool:
        self.sent.append(message)
        return True

    def stop(self, *, flush: bool = False) -> None:
        self.stopped = True

    def join(self, timeout: float = 0) -> None:
        pass

    def wake(self) -> None:
        pass

    def ack(self) -> None:
        pass

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [m for m in self.sent if m["type"] == kind]


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        ensure_app()
        self.db = Database(tmp_path / "rig.db")
        self.settings = Settings(self.db)
        self.manager = DownloadManager(self.db, settings=self.settings, max_concurrent=0)
        self.service = LinkService(self.manager, key_dir=tmp_path / "keys", device_name="LAPTOP")
        self.dest = tmp_path / "dl"
        self.dest.mkdir()
        self.runner = StubRunner()
        record = LinkRecord(
            pc_id="A3D7Za2aw26jbGofVV3ZAQ",
            name="DESKTOP",
            pin="x" * 43,
            device_id="KsucBZHeyJguN6H0BzJQiw",
            addresses=("127.0.0.1",),
            port=47821,
            last_address="127.0.0.1",
            key_file="link-0000000000000000.pem",
            paired_at=1,
            last_seen_at=1,
        )
        self.link = _Link(record=record, runner=self.runner, token=1)  # type: ignore[arg-type]
        self.link.state = session.ONLINE
        self.link.features = frozenset({"jobs", "control", "events", "phoneJobs"})
        self.service._links[record.pc_id] = self.link
        self.pc_id = record.pc_id

    def job(self, name: str = "a.zip", url: str = "https://example.com/a.zip") -> int:
        return self.db.create_job(url, str(self.dest), name).id

    def command(self, command_id: int, action: str, job_id: int) -> None:
        self.service._on_host_event(
            self.link,
            {"event": "phone.command", "commandId": command_id, "action": action, "jobId": job_id},
        )

    def results(self) -> list[dict[str, Any]]:
        return self.runner.of_type("phoneCommandResult")

    def reply(self, request: dict[str, Any], kind: str = "ok", **body: Any) -> None:
        self.service._on_message(self.link, {"id": request["id"], "type": kind, **body})

    def close(self) -> None:
        self.service.shutdown()
        self.manager.shutdown()
        self.db.close()


@pytest.fixture(autouse=True)
def _temp_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))


@pytest.fixture()
def rig(tmp_path: Path) -> Iterator[Rig]:
    made = Rig(tmp_path)
    try:
        yield made
    finally:
        made.close()


# ------------------------------------------------------------------ reports


def test_the_report_has_the_section_20_shape(rig: Rig) -> None:
    job_id = rig.job("movie.mkv", URL)
    (item,) = LocalDownloads(rig.manager).report()
    assert set(item) == {
        "id", "name", "url", "status", "kind", "downloaded", "total",
        "speed", "eta", "createdAt", "error",
    }  # fmt: skip
    assert item["id"] == job_id and item["name"] == "movie.mkv"
    assert item["status"] == "queued" and item["kind"] == "direct"
    assert item["url"] == "https://example.com/dir/file.zip", "user:password@ is removed"
    assert item["speed"] == 0 and item["eta"] is None and item["error"] is None


def test_the_report_is_newest_first_and_capped_at_100(rig: Rig) -> None:
    for index in range(130):
        rig.job(f"f{index}.bin")
    report = LocalDownloads(rig.manager).report()
    assert len(report) == 100
    ids = [item["id"] for item in report]
    assert ids == sorted(ids, reverse=True)


def test_names_are_never_paths_and_local_links_are_not_reported(rig: Rig) -> None:
    rig.db.create_job("file:///C:/Users/me/secret.torrent", str(rig.dest), "x.torrent")
    rig.db.create_job("https://example.com/b", str(rig.dest), "C:\\Users\\me\\b.bin")
    items = {i["name"]: i for i in LocalDownloads(rig.manager).report()}
    assert items["x.torrent"]["url"] == ""
    assert not any("Users" in i["name"] or "\\" in i["name"] for i in items.values())


def test_a_failed_job_reports_its_error_in_plain_words(rig: Rig) -> None:
    job_id = rig.job()
    rig.db.set_job_status(job_id, JobStatus.FAILED, error="No space left on D:\\Downloads\\x.part")
    (item,) = LocalDownloads(rig.manager).report()
    assert item["status"] == "failed" and "Downloads" not in str(item["error"])


def test_long_names_and_links_are_cut_to_the_limits(rig: Rig) -> None:
    rig.db.create_job("https://example.com/" + "a" * 3000, str(rig.dest), "n" * 400)
    (item,) = LocalDownloads(rig.manager).report()
    assert len(item["name"]) <= 256 and item["url"] == ""
    assert local.cut("\U0001f600" * 200, 256) == "\U0001f600" * 128  # UTF-16 units, no split pairs


# ----------------------------------------------------------------- commands


def test_pause_resume_cancel_retry_remove(rig: Rig) -> None:
    local_ = LocalDownloads(rig.manager)
    job_id = rig.job()
    assert local_.run("pause", job_id) == Outcome(local.OK, None, True)
    assert rig.db.get_job(job_id).status is JobStatus.PAUSED  # type: ignore[union-attr]
    assert local_.run("pause", job_id).result == local.REFUSED  # type: ignore[union-attr]
    assert local_.run("resume", job_id).result == local.OK  # type: ignore[union-attr]
    assert rig.db.get_job(job_id).status is JobStatus.QUEUED  # type: ignore[union-attr]
    assert local_.run("cancel", job_id).result == local.OK  # type: ignore[union-attr]
    assert rig.db.get_job(job_id).status is JobStatus.CANCELLED  # type: ignore[union-attr]
    assert local_.run("retry", job_id).result == local.OK  # type: ignore[union-attr]
    assert rig.db.get_job(job_id).status is JobStatus.QUEUED  # type: ignore[union-attr]
    assert local_.run("remove", job_id).result == local.OK  # type: ignore[union-attr]
    assert rig.db.get_job(job_id) is None


def test_commands_are_checked_against_the_state(rig: Rig) -> None:
    local_ = LocalDownloads(rig.manager)
    job_id = rig.job()
    for action in ("resume", "retry"):
        outcome = local_.run(action, job_id)
        assert isinstance(outcome, Outcome) and outcome.result == local.REFUSED
        assert outcome.message == local.WRONG_STATE_TEXT
    assert rig.db.get_job(job_id).status is JobStatus.QUEUED  # type: ignore[union-attr]


def test_unknown_jobs_and_actions(rig: Rig) -> None:
    local_ = LocalDownloads(rig.manager)
    assert local_.run("pause", 4242) == Outcome(local.NOT_FOUND, local.NOT_FOUND_TEXT)
    job_id = rig.job()
    assert local_.run("explode", job_id) == Outcome(local.REFUSED, local.UNKNOWN_ACTION_TEXT)


def test_move_to_pc_is_a_plan_and_cancels_only_when_finished(rig: Rig) -> None:
    local_ = LocalDownloads(rig.manager)
    job_id = rig.job("a.zip", URL)
    plan = local_.run("moveToPc", job_id)
    assert isinstance(plan, MovePlan)
    assert plan.url == "https://example.com/dir/file.zip" and plan.title == "a.zip"
    assert rig.db.get_job(job_id).status is JobStatus.QUEUED  # type: ignore[union-attr]
    local_.finish_move(job_id)
    assert rig.db.get_job(job_id).status is JobStatus.CANCELLED  # type: ignore[union-attr]


def test_a_finished_download_is_kept_when_moved(rig: Rig) -> None:
    job_id = rig.job("done.zip")
    (rig.dest / "done.zip").write_bytes(b"data")
    rig.db.set_job_status(job_id, JobStatus.COMPLETED)
    local_ = LocalDownloads(rig.manager)
    assert isinstance(local_.run("moveToPc", job_id), MovePlan)
    local_.finish_move(job_id)
    assert rig.db.get_job(job_id).status is JobStatus.COMPLETED  # type: ignore[union-attr]
    assert (rig.dest / "done.zip").read_bytes() == b"data"


def test_a_cancelled_or_local_only_download_cannot_be_moved(rig: Rig) -> None:
    local_ = LocalDownloads(rig.manager)
    cancelled = rig.job()
    rig.db.set_job_status(cancelled, JobStatus.CANCELLED)
    outcome = local_.run("moveToPc", cancelled)
    assert isinstance(outcome, Outcome) and outcome.result == local.REFUSED
    torrent = rig.db.create_job("file:///tmp/x.torrent", str(rig.dest), "x.torrent").id
    outcome = local_.run("moveToPc", torrent)
    assert isinstance(outcome, Outcome) and outcome.result == local.REFUSED


# --------------------------------------- commands through the service


def test_a_command_is_run_and_answered(rig: Rig) -> None:
    job_id = rig.job()
    rig.command(7, "pause", job_id)
    assert rig.db.get_job(job_id).status is JobStatus.PAUSED  # type: ignore[union-attr]
    (result,) = rig.results()
    assert result["commandId"] == 7 and result["result"] == "ok" and result["message"] is None


def test_sharing_off_refuses_every_command_without_touching_anything(rig: Rig) -> None:
    job_id = rig.job()
    rig.service.set_shared(False)
    rig.runner.sent.clear()
    for number, action in enumerate(("pause", "cancel", "remove", "moveToPc"), start=1):
        rig.command(number, action, job_id)
    results = rig.results()
    assert [r["result"] for r in results] == ["refused"] * 4
    assert all(r["message"] == local.NOT_SHARED_TEXT for r in results)
    stored = rig.db.get_job(job_id)
    assert stored is not None and stored.status is JobStatus.QUEUED
    assert not rig.runner.of_type("download"), "nothing may be sent to the host either"


def test_unshared_is_reported_once_per_connection(rig: Rig) -> None:
    rig.service.set_shared(False)
    unshared = rig.runner.of_type("phoneJobs")
    assert len(unshared) == 1 and unshared[0]["shared"] is False and unshared[0]["jobs"] == []
    rig.service.set_shared(False)
    rig.job()
    rig.service._report_tick()
    assert len(rig.runner.of_type("phoneJobs")) == 1, (
        "a list must never go out while sharing is off"
    )


def test_a_command_from_a_host_without_the_feature_is_ignored(rig: Rig) -> None:
    rig.link.features = frozenset({"jobs", "control"})
    job_id = rig.job()
    rig.command(1, "pause", job_id)
    assert rig.results() == []
    assert rig.db.get_job(job_id).status is JobStatus.QUEUED  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "message",
    [
        {"event": "phone.command", "commandId": 0, "action": "pause", "jobId": 1},
        {"event": "phone.command", "commandId": "1", "action": "pause", "jobId": 1},
        {"event": "phone.command", "commandId": True, "action": "pause", "jobId": 1},
        {"event": "phone.command", "commandId": 1, "action": 5, "jobId": 1},
        {"event": "phone.command", "commandId": 1, "action": "pause", "jobId": "1"},
        {"event": "phone.command", "commandId": 2**31, "action": "pause", "jobId": 1},
    ],
)
def test_malformed_commands_are_dropped(rig: Rig, message: dict[str, Any]) -> None:
    rig.service._on_host_event(rig.link, message)
    assert rig.results() == []


def test_move_to_pc_sends_download_and_cancels_only_after_queued(rig: Rig) -> None:
    job_id = rig.job("a.zip", URL)
    rig.command(3, "moveToPc", job_id)
    (download,) = rig.runner.of_type("download")
    assert download["url"] == "https://example.com/dir/file.zip"
    assert download["quality"] == "best" and 16 <= len(download["requestId"]) <= 64
    assert rig.results() == [], "no answer before the host replied"
    assert rig.db.get_job(job_id).status is JobStatus.QUEUED  # type: ignore[union-attr]
    rig.reply(download, "queued", handoffId=1, duplicate=False)
    assert rig.db.get_job(job_id).status is JobStatus.CANCELLED  # type: ignore[union-attr]
    (result,) = rig.results()
    assert result["commandId"] == 3 and result["result"] == "ok"


def test_move_to_pc_leaves_the_job_alone_when_the_host_refuses(rig: Rig) -> None:
    job_id = rig.job("a.zip", URL)
    rig.command(4, "moveToPc", job_id)
    (download,) = rig.runner.of_type("download")
    rig.reply(download, "error", code="forbidden")
    assert rig.db.get_job(job_id).status is JobStatus.QUEUED  # type: ignore[union-attr]
    (result,) = rig.results()
    assert result["result"] == "failed"


def test_move_to_pc_leaves_the_job_alone_when_the_host_drops(rig: Rig) -> None:
    job_id = rig.job("a.zip", URL)
    rig.command(5, "moveToPc", job_id)
    rig.service._fail_pending(rig.pc_id, "offline")
    assert rig.db.get_job(job_id).status is JobStatus.QUEUED  # type: ignore[union-attr]


def test_a_report_follows_a_command_within_a_tick(rig: Rig) -> None:
    job_id = rig.job()
    rig.service._report_tick()
    first = rig.runner.of_type("phoneJobs")
    assert len(first) == 1 and first[0]["shared"] is True
    rig.service._report_tick()
    assert len(rig.runner.of_type("phoneJobs")) == 1, "unchanged: nothing is sent"
    rig.command(9, "pause", job_id)
    rig.service._report_tick()
    latest = rig.runner.of_type("phoneJobs")[-1]
    assert latest["jobs"][0]["status"] == "paused"


# -------------------------------------------------- the host's jobs, live


def _job(job_id: int, status: Any = "queued", **changes: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "id": job_id, "url": "https://example.com/x", "name": f"job {job_id}", "status": status,
        "kind": "direct", "downloaded": 0, "total": 100, "speed": 0, "eta": None,
        "queueId": None, "createdAt": 1, "finishedAt": None, "error": None, "requestId": None,
        "fetchable": False,
    }  # fmt: skip
    raw.update(changes)
    return raw


def _answer_jobs(rig: Rig, jobs: list[dict[str, Any]], cursor: str | None = None) -> None:
    request = rig.runner.of_type("jobs")[-1]
    rig.reply(request, "jobs", jobs=jobs, nextCursor=cursor, total=len(jobs))


def test_the_snapshot_pages_through_jobs_and_events_keep_it_live(rig: Rig) -> None:
    changed: list[str] = []
    rig.service.hostJobsChanged.connect(changed.append)
    rig.service._load_jobs(rig.link)
    first = rig.runner.of_type("jobs")[0]
    assert first["limit"] == 100 and first["filter"] == "all" and "cursor" not in first
    _answer_jobs(rig, [_job(5), _job(4)], cursor="NA")
    second = rig.runner.of_type("jobs")[1]
    assert second["cursor"] == "NA"
    _answer_jobs(rig, [_job(3)])
    snapshot = rig.service.host_jobs(rig.pc_id)
    assert snapshot is not None and [j.id for j in snapshot.jobs] == [5, 4, 3]

    seq = iter(range(1, 100))

    def event(name: str, **fields: Any) -> None:
        rig.service._on_host_event(rig.link, {"event": name, "seq": next(seq), **fields})

    event("job.added", job=_job(6))
    event("job.updated", job=_job(6, "paused"))
    event("job.progress", jobId=5, status="downloading", downloaded=50, total=100, speed=9, eta=3)
    event("job.removed", jobId=4)
    event("job.finished", job=_job(3, "completed", downloaded=100))
    snapshot = rig.service.host_jobs(rig.pc_id)
    assert snapshot is not None
    by_id = {j.id: j for j in snapshot.jobs}
    assert sorted(by_id) == [3, 5, 6]
    assert by_id[6].status == "paused" and by_id[3].status == "completed"
    assert by_id[5].status == "downloading" and by_id[5].downloaded == 50 and by_id[5].speed == 9
    assert changed, "the page is told about every change"


def test_a_gap_in_seq_reloads_the_list(rig: Rig) -> None:
    rig.service._load_jobs(rig.link)
    _answer_jobs(rig, [_job(1)])
    rig.service._on_host_event(rig.link, {"event": "job.added", "seq": 1, "job": _job(2)})
    count = len(rig.runner.of_type("jobs"))
    rig.service._on_host_event(rig.link, {"event": "job.added", "seq": 2, "job": _job(3)})
    assert len(rig.runner.of_type("jobs")) == count, "no gap, no reload"
    rig.service._on_host_event(rig.link, {"event": "job.added", "seq": 9, "job": _job(4)})
    assert len(rig.runner.of_type("jobs")) == count + 1
    # The new page does not list 3 (the host no longer has it); job 4 came in the
    # event that revealed the gap, which is newer than the page.
    _answer_jobs(rig, [_job(1), _job(2)])
    snapshot = rig.service.host_jobs(rig.pc_id)
    assert snapshot is not None and sorted(j.id for j in snapshot.jobs) == [1, 2, 4]


def test_an_event_during_a_load_is_newer_than_the_page(rig: Rig) -> None:
    rig.service._load_jobs(rig.link)
    rig.service._on_host_event(
        rig.link, {"event": "job.updated", "seq": 1, "job": _job(1, "paused")}
    )
    _answer_jobs(rig, [_job(1, "queued"), _job(2)])
    snapshot = rig.service.host_jobs(rig.pc_id)
    assert snapshot is not None
    assert {j.id: j.status for j in snapshot.jobs} == {1: "paused", 2: "queued"}


def test_host_strings_are_shown_as_plain_safe_text() -> None:
    job = host_job(_job(1, name="photo\u202egpj.exe", error="a\u2067b"))
    assert job is not None and "\u202e" not in job.name and "\ufffd" in job.name
    assert job.error is not None and "\u2067" not in job.error


@pytest.mark.parametrize(
    "raw",
    [
        None, "x", [], {}, _job(0), _job(1, name=5), _job(1, status=7), _job(1, downloaded=-1),
        _job(1, downloaded=True), _job(1, total="9"), _job(1, eta=-5), _job(1, url=None),
    ],
)  # fmt: skip
def test_malformed_host_jobs_are_dropped(raw: Any) -> None:
    assert host_job(raw) is None


def test_an_unknown_status_has_no_actions() -> None:
    job = host_job(_job(1, "teleporting"))
    assert job is not None and job.status == "unknown" and job.actions(movable=False) == ()


def test_host_jobs_offer_no_move_to_pc() -> None:
    job = host_job(_job(1))
    assert job is not None
    assert "moveToPc" not in job.actions(movable=False) and "moveToPc" in job.actions()


# ------------------------------------------------------------- sending


def test_control_download_and_links_build_the_right_requests(rig: Rig) -> None:
    results: list[tuple[int, str, str]] = []
    rig.service.requestFinished.connect(lambda *a: results.append(a))
    token = rig.service.send_control(rig.pc_id, 7, "pause")
    sent = rig.runner.of_type("control")[0]
    assert (sent["jobId"], sent["action"]) == (7, "pause")
    rig.reply(sent, "ok", job=None)
    assert results == [(token, "ok", "")]

    token = rig.service.send_download(rig.pc_id, "https://example.com/f.bin", "f.bin")
    sent = rig.runner.of_type("download")[0]
    assert sent["quality"] == "best" and sent["title"] == "f.bin"
    assert "path" not in sent and "folderId" not in sent
    rig.reply(sent, "queued", duplicate=True)
    assert results[-1] == (token, "ok", "duplicate")

    token = rig.service.send_links(rig.pc_id, ["https://a.example/1", "bad", "https://a.example/2"])
    sent = rig.runner.of_type("links")[0]
    assert sent["urls"] == ["https://a.example/1", "https://a.example/2"]
    rig.reply(sent, "error", code="tooLarge")
    assert results[-1][1] == "failed"


@pytest.mark.parametrize(
    ("code", "expected"),
    [("notFound", "notFound"), ("conflict", "conflict"), ("rateLimited", "busy"),
     ("unsupported", "unsupported"), ("internal", "failed")],
)  # fmt: skip
def test_host_errors_are_mapped(rig: Rig, code: str, expected: str) -> None:
    results: list[tuple[int, str, str]] = []
    rig.service.requestFinished.connect(lambda *a: results.append(a))
    rig.service.send_control(rig.pc_id, 1, "pause")
    rig.reply(rig.runner.of_type("control")[0], "error", code=code)
    assert results[0][1] == expected


def test_nothing_is_sent_for_a_feature_the_host_does_not_list(rig: Rig) -> None:
    rig.link.features = frozenset({"jobs"})
    results: list[tuple[int, str, str]] = []
    rig.service.requestFinished.connect(lambda *a: results.append(a))
    token = rig.service.send_control(rig.pc_id, 1, "pause")
    assert pump(lambda: bool(results))
    assert results == [(token, "unsupported", "")]
    assert rig.runner.of_type("control") == []


def test_an_offline_link_answers_at_once_and_sends_nothing(rig: Rig) -> None:
    rig.link.state = session.OFFLINE
    results: list[tuple[int, str, str]] = []
    rig.service.requestFinished.connect(lambda *a: results.append(a))
    token = rig.service.send_download(rig.pc_id, "https://example.com/f")
    assert pump(lambda: bool(results))
    assert results == [(token, "offline", "")]
    assert rig.runner.sent == []


def test_invalid_arguments_return_zero(rig: Rig) -> None:
    assert rig.service.send_control(rig.pc_id, 1, "explode") == 0
    assert rig.service.send_control(rig.pc_id, 0, "pause") == 0
    assert rig.service.send_control("nope", 1, "pause") == 0
    assert rig.service.send_download(rig.pc_id, "ftp://x/y") == 0
    assert rig.service.send_download(rig.pc_id, "javascript:alert(1)") == 0
    assert rig.service.send_links(rig.pc_id, ["nope"]) == 0


def test_a_request_with_no_reply_times_out(rig: Rig) -> None:
    rig.service.request_timeout_s = 0.05
    results: list[tuple[int, str, str]] = []
    rig.service.requestFinished.connect(lambda *a: results.append(a))
    token = rig.service.send_control(rig.pc_id, 1, "pause")

    def expired() -> bool:
        rig.service._expire_requests()
        return bool(results)

    assert pump(expired, 3.0)
    assert results == [(token, "timeout", "")]


def test_going_offline_fails_what_is_waiting(rig: Rig) -> None:
    results: list[tuple[int, str, str]] = []
    rig.service.requestFinished.connect(lambda *a: results.append(a))
    token = rig.service.send_control(rig.pc_id, 1, "pause")
    rig.service._on_state(rig.link, session.OFFLINE)
    assert results == [(token, "offline", "")]
    assert rig.service.is_online(rig.pc_id) is False


def test_the_sharing_setting_defaults_on_and_persists(rig: Rig) -> None:
    assert rig.service.shared_setting() is True
    rig.service.set_shared(False)
    assert rig.service.shared_setting() is False
    assert rig.db.get_setting("remote_share_downloads") == "0"
    rig.service.set_shared(True)
    assert rig.service.shared_setting() is True


# ----------------------------------------------------- security review fixes


def _paired(rig: Rig, **changes: Any) -> Any:
    from app.remote.client import keys, pairing

    fields: dict[str, Any] = {
        "device_id": "KsucBZHeyJguN6H0BzJQiw",
        "pc_id": rig.pc_id,
        "pc_name": "IMPOSTOR",
        "pin": "y" * 43,
        "address": "127.0.0.1",
        "port": 9,
        "key": keys.new_key(),
    }
    fields.update(changes)
    return pairing.Paired(**fields)


def _stored_link(rig: Rig) -> str:
    """Give the rig's link a real key file and database row."""
    from app.remote.client import keys

    name = keys.save(keys.new_key(), rig.service._key_dir)
    rig.link.record = replace(rig.link.record, key_file=name)
    rig.db.upsert_remote_link(_row(rig.link.record))
    return name


def test_m1_a_host_claiming_an_existing_pc_id_cannot_replace_the_link(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.remote.client import keys

    key_file = _stored_link(rig)
    discarded: list[Any] = []
    monkeypatch.setattr(rig.service, "_discard_pairing", discarded.append)
    finished: list[tuple[str, str]] = []
    rig.service.linkFinished.connect(lambda code, pc: finished.append((code, pc)))
    result = _paired(rig)  # same pcId, another pin
    rig.service._on_paired(result)
    assert finished == [("conflict", "")]
    # The real link is untouched: no unpair, still running, key kept, row unchanged.
    assert rig.runner.of_type("unpair") == [] and not rig.runner.stopped
    assert keys.load(key_file, rig.service._key_dir) is not None
    (row,) = rig.db.list_remote_links()
    assert row["pin"] == "x" * 43 and row["name"] == "DESKTOP" and row["key_file"] == key_file
    assert rig.service._links[rig.pc_id] is rig.link
    # The attempt itself is undone on the host that was just paired.
    assert discarded == [result]


def test_m1_the_same_host_linking_again_replaces_its_link(rig: Rig) -> None:
    from app.remote.client import keys

    old_file = _stored_link(rig)
    finished: list[tuple[str, str]] = []
    rig.service.linkFinished.connect(lambda code, pc: finished.append((code, pc)))
    rig.service._on_paired(_paired(rig, pin="x" * 43, pc_name="DESKTOP"))
    assert finished == [("ok", rig.pc_id)]
    assert rig.service._links[rig.pc_id] is not rig.link
    assert keys.load(old_file, rig.service._key_dir) is None, "the old key goes with the old link"
    (row,) = rig.db.list_remote_links()
    assert row["key_file"] != old_file


def test_l4_a_pairing_that_completes_after_cancel_is_undone_and_not_kept(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    discarded: list[Any] = []
    monkeypatch.setattr(rig.service, "_discard_pairing", discarded.append)
    finished: list[tuple[str, str]] = []
    rig.service.linkFinished.connect(lambda code, pc: finished.append((code, pc)))
    rig.service._links.clear()
    cancel = threading.Event()
    cancel.set()
    result = _paired(rig, pc_id="B" * 22)
    rig.service._on_paired((result, cancel))
    assert finished == [("cancelled", "")] and discarded == [result]
    assert rig.db.list_remote_links() == [] and rig.service.links() == []
    key_dir = rig.service._key_dir
    assert key_dir is None or not list(key_dir.glob("*"))


def test_l4_discarding_signs_in_to_that_host_and_unpairs(tmp_path: Path) -> None:
    """The real thing against a scripted host: the pairing nobody keeps is undone."""
    from app.remote.client import pairing
    from app.tests.fake_host import FakeConn, FakeHost

    seen: list[dict[str, Any]] = []
    holder: list[FakeHost] = []

    def script(conn: FakeConn) -> None:
        conn.hello()
        auth = conn.recv()
        assert auth is not None and auth["type"] == "auth"
        conn.send(
            {
                "id": auth["id"],
                "type": "authOk",
                "protocolVersion": 6,
                "pcId": holder[0].pc_id,
                "pcName": "DESKTOP",
                "addresses": [],
                "port": holder[0].port,
                "features": [],
            }
        )
        message = conn.recv(5.0)
        if message is not None:
            seen.append(message)
            conn.send({"id": message["id"], "type": "ok", "protocolVersion": 6})

    host = FakeHost(tmp_path / "h", script)
    holder.append(host)
    ensure_app()
    db = Database(tmp_path / "d.db")
    manager = DownloadManager(db, settings=Settings(db), max_concurrent=0)
    service = LinkService(manager, key_dir=tmp_path / "keys")
    try:
        from app.remote.client import keys

        paired = pairing.Paired(
            device_id="KsucBZHeyJguN6H0BzJQiw",
            pc_id=host.pc_id,
            pc_name="DESKTOP",
            pin=host.pin,
            address="127.0.0.1",
            port=host.port,
            key=keys.new_key(),
        )
        service._discard_pairing(paired)
        assert pump(lambda: bool(seen), 10.0), host.errors
        assert seen[0]["type"] == "unpair"
    finally:
        service.shutdown()
        manager.shutdown()
        db.close()
        host.stop()


def test_m2_host_job_strings_are_cut_to_the_section_20_limits() -> None:
    job = host_job(_job(1, name="n" * 10_000, error="e" * 10_000, kind="k" * 5_000))
    assert job is not None
    assert len(job.name) <= 256 and job.kind == "direct" and len(job.error or "") <= 512
    failed = host_job(_job(2, "failed", error="e" * 10_000))
    assert failed is not None and failed.error is not None and len(failed.error) <= 512
    long_url = host_job(_job(3, url="https://example.com/" + "u" * 5_000))
    assert long_url is not None and long_url.url == ""
    ok = host_job(_job(4, url="https://example.com/a", kind="torrent"))
    assert ok is not None and ok.kind == "torrent"


def test_m2_a_host_cannot_grow_the_job_list_or_touched_set(rig: Rig) -> None:
    from app.remote.client.service import MAX_HOST_JOBS

    rig.service._load_jobs(rig.link)  # loading: ``touched`` fills too
    for number in range(1, MAX_HOST_JOBS + 200):
        rig.service._on_host_event(
            rig.link, {"event": "job.added", "seq": number, "job": _job(number)}
        )
    assert len(rig.link.jobs) == MAX_HOST_JOBS and len(rig.link.touched) <= MAX_HOST_JOBS
    assert min(rig.link.jobs) == 200 and max(rig.link.jobs) == MAX_HOST_JOBS + 199, (
        "the lowest ids are evicted"
    )
    for number in range(5000, 5300):
        rig.service._on_host_event(
            rig.link, {"event": "job.removed", "seq": 10_000 + number, "jobId": number}
        )
    assert len(rig.link.touched) <= MAX_HOST_JOBS


def test_m3_another_host_cannot_answer_a_pending_request(rig: Rig) -> None:
    from app.remote.client.service import _Link

    other_record = replace(rig.link.record, pc_id="B" * 22, name="OTHER")
    other = _Link(record=other_record, runner=StubRunner(), token=2)  # type: ignore[arg-type]
    other.state = session.ONLINE
    results: list[tuple[int, str, str]] = []
    rig.service.requestFinished.connect(lambda *a: results.append(a))
    rig.service.send_control(rig.pc_id, 1, "pause")
    request = rig.runner.of_type("control")[0]
    # Host B answers with A's request id: nothing happens and the request waits.
    rig.service._on_message(other, {"id": request["id"], "type": "ok"})
    assert results == [] and request["id"] in rig.service._pending
    rig.reply(request, "ok", job=None)  # the real answer still works
    assert [r[1] for r in results] == ["ok"]


def test_m3_request_ids_are_not_guessable(rig: Rig) -> None:
    rig.service.send_control(rig.pc_id, 1, "pause")
    rig.service.send_control(rig.pc_id, 1, "resume")
    first, second = (m["id"] for m in rig.runner.of_type("control"))
    assert first != second and len(first) > 6 and first.split("-")[1] != second.split("-")[1]


def test_l1_only_queued_counts_for_a_move(rig: Rig) -> None:
    job_id = rig.job("a.zip", URL)
    rig.command(3, "moveToPc", job_id)
    (download,) = rig.runner.of_type("download")
    rig.reply(download, "ok")  # any other success type is not "queued"
    assert rig.db.get_job(job_id).status is JobStatus.QUEUED  # type: ignore[union-attr]
    assert rig.results()[-1]["result"] == "failed"


def test_l2_paging_stops_after_a_few_pages(rig: Rig) -> None:
    from app.remote.client.service import MAX_PAGES

    rig.service._load_jobs(rig.link)
    for number in range(30):
        if len(rig.runner.of_type("jobs")) <= number:
            break
        rig.reply(rig.runner.of_type("jobs")[number], "jobs", jobs=[], nextCursor="MQ", total=0)
    assert len(rig.runner.of_type("jobs")) == MAX_PAGES
    assert rig.service.host_jobs(rig.pc_id) is not None, "what arrived is kept"


@pytest.mark.parametrize(
    ("address", "allowed"),
    [
        ("10.1.2.3", True), ("192.168.1.20", True), ("172.16.0.9", True), ("172.31.255.1", True),
        ("169.254.10.1", True), ("[fd7a:115c:a1e0::20]", True), ("fd00::1", True),
        ("fe80::1", True), ("127.0.0.1", False), ("::1", False), ("fe80::1%eth0", False),
        ("10.0.0.1%1", False), ("fd00::" + "1" * 70, False), ("[::ffff:127.0.0.1]", False),
        ("8.8.8.8", False), ("172.32.0.1", False), ("100.64.0.1", False), ("2001:db8::1", False),
        ("0.0.0.0", False), ("example.com", False), ("laptop.local", False), ("", False),
        ("10.0.0.1:47821", False), ("http://10.0.0.1", False),
    ],
)  # fmt: skip
def test_l3_only_private_ip_literals_are_accepted(address: str, allowed: bool) -> None:
    from app.remote.addresses import is_client_target

    assert is_client_target(address) is allowed


def test_l3_host_supplied_addresses_are_filtered_before_they_are_stored(rig: Rig) -> None:
    _stored_link(rig)
    rig.service._on_online(
        rig.link,
        {
            "address": "127.0.0.1",
            "authOk": {
                "pcName": "DESKTOP",
                "features": [],
                "port": 47821,
                "addresses": ["10.0.0.5", "evil.example", "8.8.8.8", "[fd00::1]", "192.168.1.9"],
            },
        },
    )
    expected = ("10.0.0.5", "[fd00::1]", "192.168.1.9")
    assert rig.link.record.addresses == expected
    (row,) = rig.db.list_remote_links()
    assert tuple(row["addresses"]) == expected


def test_l6_orphan_key_files_are_removed_at_start(tmp_path: Path) -> None:
    from app.remote.client import keys

    ensure_app()
    db = Database(tmp_path / "o.db")
    manager = DownloadManager(db, settings=Settings(db), max_concurrent=0)
    folder = tmp_path / "keys"
    used = keys.save(keys.new_key(), folder)
    orphan = keys.save(keys.new_key(), folder)
    (folder / "notes.txt").write_text("not ours")
    record = LinkRecord(
        pc_id="A3D7Za2aw26jbGofVV3ZAQ", name="D", pin="x" * 43, device_id="K" * 22,
        addresses=("127.0.0.1",), port=9, last_address="127.0.0.1", key_file=used,
        paired_at=1, last_seen_at=1,
    )  # fmt: skip
    from app.remote.client.service import _row

    db.upsert_remote_link(_row(record))
    service = LinkService(manager, key_dir=folder)
    try:
        service.start()
        assert keys.load(used, folder) is not None
        assert keys.load(orphan, folder) is None and not (folder / orphan).exists()
        assert (folder / "notes.txt").exists(), "only link-*.pem files are touched"
    finally:
        service.shutdown()
        manager.shutdown()
        db.close()


def test_l7_a_failure_is_logged_by_type_only(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    def boom() -> Any:
        raise RuntimeError("secret C:/Users/me/x http://user:pw@host/?token=abc")

    monkeypatch.setattr(rig.service.local, "report", boom)
    with caplog.at_level(logging.DEBUG, logger="app.remote.client.service"):
        rig.service._report_tick()
        rig.service._send_report(rig.link, force=True)
        monkeypatch.setattr(rig.service.local, "run", lambda *a: boom())
        rig.command(1, "pause", rig.job())
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in text
    assert "secret" not in text and "pw@" not in text and "Users" not in text
    assert all(r.exc_info is None for r in caplog.records), "no traceback text"


# ------------------------------------------- second review (Low1, Note4-7)


def test_low1_huge_host_numbers_are_refused_not_kept() -> None:
    from app.remote.schema import MAX_SAFE_INT

    huge = 10**400
    for field in ("id", "downloaded", "total", "speed", "eta", "createdAt"):
        assert host_job(_job(5, **{field: huge})) is None or field in ("speed", "createdAt")
    assert host_job(_job(5, id=MAX_SAFE_INT + 1)) is None
    assert host_job(_job(5, downloaded=MAX_SAFE_INT + 1)) is None
    assert host_job(_job(5, total=MAX_SAFE_INT + 1)) is None
    assert host_job(_job(5, eta=MAX_SAFE_INT + 1)) is None
    # speed and createdAt are not required: out of range becomes 0
    job = host_job(_job(5, speed=huge, createdAt=huge, status="downloading"))
    assert job is not None and job.speed == 0 and job.created_at == 0
    assert job.fraction is not None or job.total is None
    ok = host_job(_job(6, downloaded=MAX_SAFE_INT, total=MAX_SAFE_INT))
    assert ok is not None and ok.fraction == 1.0


def test_low1_a_huge_progress_event_changes_nothing(rig: Rig) -> None:
    rig.service._on_host_event(rig.link, {"event": "job.added", "seq": 1, "job": _job(1)})
    rig.link.loaded = True
    rig.service._on_host_event(
        rig.link,
        {"event": "job.progress", "seq": 2, "jobId": 1, "status": "downloading",
         "downloaded": 10**400, "total": 10**400, "speed": 10**400, "eta": 10**400},
    )  # fmt: skip
    job = rig.link.jobs[1]
    assert job.downloaded == 0 and job.speed == 0


def test_note4_a_late_pairing_of_an_old_attempt_is_not_attributed_to_a_new_one(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    discarded: list[Any] = []
    monkeypatch.setattr(rig.service, "_discard_pairing", discarded.append)
    finished: list[tuple[str, str]] = []
    rig.service.linkFinished.connect(lambda code, pc: finished.append((code, pc)))
    rig.service._links.clear()
    old_cancel = threading.Event()
    old_cancel.set()  # the person cancelled attempt 1 ...
    rig.service._add_cancel = threading.Event()  # ... and started attempt 2
    result = _paired(rig, pc_id="C" * 22)
    rig.service._on_paired((result, old_cancel))
    assert finished == [("cancelled", "")] and discarded == [result]
    assert rig.service._add_cancel is not None and not rig.service._add_cancel.is_set()
    # An attempt whose own event is not set is kept.
    rig.service._on_paired((_paired(rig, pc_id="D" * 22), threading.Event()))
    assert finished[-1][0] == "ok"


def test_note6_finishing_a_load_applies_the_cap(rig: Rig) -> None:
    from app.remote.client.service import MAX_HOST_JOBS

    for number in range(1, MAX_HOST_JOBS + 101):
        job = host_job(_job(number))
        assert job is not None
        rig.link.jobs[number] = job
        rig.link.touched.add(number)
    rig.service._finish_load(rig.link, {})
    assert len(rig.link.jobs) == MAX_HOST_JOBS and min(rig.link.jobs) == 101


def test_note7_finished_discard_runners_are_pruned(rig: Rig) -> None:
    class Gone:
        alive = False

        def stop(self) -> None:
            pass

    rig.service._discards = [Gone(), Gone()]  # type: ignore[list-item]
    rig.service._discard_pairing(_paired(rig))
    assert len(rig.service._discards) == 1 and rig.service._discards[0].alive
