"""The shared protocol fixtures (protocol/fixtures) against the desktop code."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.core.manager import DownloadManager
from app.core.models import JobStatus
from app.db.database import Database
from app.remote.handlers import LiveFigures, RemoteHandlers
from app.remote.requests_log import RequestLog
from app.remote.schema import RequestError, validate

FIXTURES = Path(__file__).resolve().parents[2] / "protocol" / "fixtures"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))["fixtures"]
SCHEMA_CODES = {"badRequest", "forbidden", "unsupported", "tooLarge"}

#: Fixtures that need the listener or pairing; covered by the listener tests (P1-D11).
SKIP_LISTENER = {
    "invalid/auth.badSignature.json": "needs a paired device and the auth handshake",
    "invalid/auth.oldClient.json": "version check lives in the auth handshake",
    "invalid/auth.unknownDevice.json": "needs the device registry in the auth handshake",
    "invalid/pair.expired.json": "needs a pairing session",
    "invalid/pair.wrongCode.json": "needs a pairing session",
    "invalid/preauth.download.json": "state check lives in the listener",
    "invalid/preauth.jobs.json": "state check lives in the listener",
    "invalid/oversize.marker.json": "frame size limit is enforced by the listener (1009)",
    "invalid/notJson.marker.json": "frame decoding is done by the listener (4400)",
    "invalid/flood.marker.json": "rate limiting is done by the listener",
    "invalid/fetch.unfinishedJob.json": "fetch streaming is Phase 4 (listener)",
    "fetch.ack.json": "binary-stream notification, handled by the listener",
}
#: Handler-level errors, run through RemoteHandlers below.
HANDLER_ERRORS = {
    "invalid/control.conflict.json",
    "invalid/control.unknownJob.json",
    "invalid/download.unknownQueue.json",
    "invalid/download.unknownFolder.json",
}
DEVICE = "KsucBZHeyJguN6H0BzJQiw"


def load(name: str) -> dict[str, Any]:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def entries(direction: str, *, invalid: bool) -> list[str]:
    return sorted(
        e["file"]
        for e in MANIFEST
        if e["direction"] == direction and e["file"].startswith("invalid/") == invalid
    )


def test_manifest_matches_the_files_on_disk():
    listed = {e["file"] for e in MANIFEST}
    on_disk = {
        p.relative_to(FIXTURES).as_posix()
        for p in FIXTURES.rglob("*")
        if p.is_file() and p.name not in ("README.md", "manifest.json")
    }
    assert listed == on_disk


@pytest.mark.parametrize("name", entries("phone-to-pc", invalid=False))
def test_valid_request_fixtures_validate(name: str):
    if name in SKIP_LISTENER:
        pytest.skip(SKIP_LISTENER[name])
    message = load(name)["message"]
    request = validate(message)
    assert request.type == message["type"] and request.id == message["id"]
    ignored = load(name).get("expectIgnored", [])
    for key in ignored:
        assert key not in request.fields


def test_ignored_fields_fixture_still_replies_queued_shape():
    fixture = load("download.request.ignoredFields.json")
    assert fixture["expectReplyType"] == "queued"
    fields = validate(fixture["message"]).fields
    assert set(fields) == {"url", "requestId", "title", "pageUrl", "quality", "queueId", "folderId"}


def _invalid_cases() -> list[Any]:
    cases = []
    for name in entries("phone-to-pc", invalid=True):
        if name in SKIP_LISTENER or name in HANDLER_ERRORS:
            continue
        fixture = load(name)
        if fixture.get("expectError") in SCHEMA_CODES:
            cases.append(name)
    return cases


@pytest.mark.parametrize("name", _invalid_cases())
def test_invalid_fixtures_raise_their_schema_code(name: str):
    fixture = load(name)
    message = fixture["message"]
    if "generate" in fixture:  # links.tooMany.marker
        gen = fixture["generate"]
        message[gen["listField"]] = [
            gen["template"].format(n=n) for n in range(1, gen["count"] + 1)
        ]
    with pytest.raises(RequestError) as info:
        validate(message)
    assert info.value.code == fixture["expectError"]


def test_every_invalid_fixture_is_accounted_for():
    for name in entries("phone-to-pc", invalid=True):
        fixture = load(name)
        schema_level = fixture.get("expectError") in SCHEMA_CODES
        assert name in SKIP_LISTENER or name in HANDLER_ERRORS or schema_level, (
            f"{name} has no test"
        )
    for name in SKIP_LISTENER:
        assert (FIXTURES / name).exists()


# --------------------------------------------------------- handler level


class Env:
    def __init__(self, db: Database, manager: DownloadManager, tmp: Path) -> None:
        self.db = db
        self.manager = manager
        self.tmp = tmp
        self.handlers = RemoteHandlers(
            manager,
            manager.settings,
            RequestLog(db),
            identity=lambda: ("A3D7Za2aw26jbGofVV3ZAQ", "Study PC"),
            live=lambda _id: LiveFigures(),
        )

    def run(self, message: dict[str, Any]) -> dict[str, Any]:
        out: list[dict[str, Any]] = []
        errors: list[RequestError] = []
        self.handlers.handle(DEVICE, validate(message), out.append, errors.append)
        if errors:
            raise errors[0]
        return out[0]


@pytest.fixture()
def env(db: Database, tmp_path: Path) -> Iterator[Env]:
    manager = DownloadManager(db, max_concurrent=0)
    manager.settings.download_dir = tmp_path / "dl"
    try:
        yield Env(db, manager, tmp_path)
    finally:
        manager.shutdown()


def _job_with_id(env: Env, job_id: int, status: JobStatus) -> None:
    """Make a job with exactly this id and status (ids are autoincrement)."""
    while True:
        job = env.db.create_job("https://example.com/x.bin", str(env.tmp), "x.bin")
        if job.id >= job_id:
            break
    assert job.id == job_id
    env.db.set_job_status(job.id, status)


@pytest.mark.parametrize("name", sorted(HANDLER_ERRORS))
def test_handler_level_invalid_fixtures(env: Env, name: str):
    fixture = load(name)
    message = fixture["message"]
    if name.endswith("control.conflict.json"):
        _job_with_id(env, 40, JobStatus.COMPLETED)
    with pytest.raises(RequestError) as info:
        env.run(message)
    assert info.value.code == fixture["expectError"]


# ---------------------------------------------- reply shape (key sets)

IGNORE = {"id", "protocolVersion"}


def keys(message: dict[str, Any]) -> set[str]:
    return set(message) - IGNORE


def job_keys(job: dict[str, Any]) -> set[str]:
    return set(job)


def test_reply_shapes_match_the_fixtures(env: Env):
    env.manager.settings.favorite_folders = []
    env.manager.create_queue("Videos")
    job = env.db.create_job("https://example.com/files/a.iso", str(env.tmp), "a.iso")
    env.db.set_job_status(job.id, JobStatus.PAUSED)
    url = "https://example.com/files/a.iso"

    pairs: list[tuple[str, dict[str, Any]]] = [
        ("ping.reply.json", env.run({"id": "r", "type": "ping"})),
        (
            "download.reply.queued.json",
            env.run(
                {
                    "id": "r",
                    "type": "download",
                    "url": url + "?1",
                    "requestId": "req-0001-7b1e4c1a9f3d4e55",
                }
            ),
        ),
        (
            "links.reply.json",
            env.run(
                {
                    "id": "r",
                    "type": "links",
                    "urls": [url],
                    "requestId": "req-0005-3c9e1f7a2b6d4c88",
                }
            ),
        ),
        ("jobs.reply.json", env.run({"id": "r", "type": "jobs"})),
        ("recent.reply.json", env.run({"id": "r", "type": "recent", "limit": 2})),
        ("status.reply.json", env.run({"id": "r", "type": "status", "urls": [url]})),
        ("focus.reply.json", env.run({"id": "r", "type": "focus", "target": "settings"})),
        ("global.reply.json", env.run({"id": "r", "type": "global", "action": "get"})),
        ("queues.reply.json", env.run({"id": "r", "type": "queues"})),
        (
            "control.reply.ok.json",
            env.run({"id": "r", "type": "control", "jobId": job.id, "action": "resume"}),
        ),
        (
            "control.reply.removed.json",
            env.run({"id": "r", "type": "control", "jobId": job.id, "action": "remove"}),
        ),
        (
            "phoneJobs.reply.json",
            env.run(load("phoneJobs.request.json")["message"]),
        ),
        (
            "phoneCommandResult.reply.json",
            env.run(load("phoneCommandResult.request.json")["message"]),
        ),
    ]
    for name, reply in pairs:
        expected = load(name)["message"]
        assert keys(reply) == keys(expected), name
        assert reply["type"] == expected["type"], name

    # Duplicate reply has the same keys as the queued one.
    dup = env.run(
        {"id": "r", "type": "download", "url": url + "?1", "requestId": "req-0001-7b1e4c1a9f3d4e55"}
    )
    assert dup["duplicate"] is True
    assert keys(dup) == keys(load("download.reply.duplicate.json")["message"])

    # Job objects and the global state object.
    env.db.create_job(url + "2", str(env.tmp), "b.iso")
    jobs_reply = env.run({"id": "r", "type": "jobs"})
    fixture_job = load("jobs.reply.json")["message"]["jobs"][0]
    assert job_keys(jobs_reply["jobs"][0]) == job_keys(fixture_job)
    assert set(env.run({"id": "r", "type": "global", "action": "get"})["state"]) == set(
        load("global.reply.json")["message"]["state"]
    )
    assert set(env.run({"id": "r", "type": "queues"})["queues"][0]) == set(
        load("queues.reply.json")["message"]["queues"][0]
    )
    assert set(env.run({"id": "r", "type": "queues"})["folders"][0]) == set(
        load("queues.reply.json")["message"]["folders"][0]
    )


def test_error_fixtures_are_well_formed():
    for name in entries("pc-to-phone", invalid=False):
        if not name.startswith("error."):
            continue
        message = load(name)["message"]
        assert message["type"] == "error" and message["code"] and message["message"]


# ------------------------------------------------ phoneJobs (section 20)


def test_phone_jobs_fixtures_are_stored_by_the_handlers(env: Env):
    env.run(load("phoneJobs.request.json")["message"])
    snapshot = env.handlers.phone.get(DEVICE)
    assert snapshot is not None and snapshot.shared is True
    assert [job.id for job in snapshot.jobs] == [12, 11, 10]
    assert snapshot.jobs[2].kind == "transfer" and snapshot.jobs[2].url == ""
    env.run(load("phoneJobs.request.notShared.json")["message"])
    snapshot = env.handlers.phone.get(DEVICE)
    assert snapshot is not None and snapshot.shared is False and snapshot.jobs == ()


def test_the_command_event_fixture_has_the_documented_shape():
    event = load("event.phone.command.json")["message"]
    assert event["type"] == "event" and event["event"] == "phone.command"
    assert set(event) == {
        "type",
        "protocolVersion",
        "event",
        "seq",
        "ts",
        "commandId",
        "action",
        "jobId",
    }
    from app.remote import phonejobs

    assert event["action"] in phonejobs.ACTIONS


def test_auth_ok_fixture_lists_the_features_the_listener_serves():
    from app.remote.listener import DEFAULT_FEATURES

    features = load("auth.reply.ok.json")["message"]["features"]
    assert "phoneJobs" in features
    # The listener also serves "events"-style groups the fixture lists in the
    # same order; neither may drift from the other.
    assert tuple(features) == DEFAULT_FEATURES
