"""The phone's downloads on the PC (PROTOCOL.md section 20, feature ``phoneJobs``).

Pure pieces (the store, the action table, the command limiter) are tested
directly; the protocol and ``RemoteService`` are tested through a real TLS
WebSocket "phone", the way test_remote_service.py does.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from app.remote import phonejobs
from app.remote.phonejobs import PhoneJob, PhoneJobStore
from app.remote.ratelimit import CommandLimiter
from app.tests.remote_client import Client, pump, settle
from app.tests.test_remote_service import Env, _wait_event

pytestmark = pytest.mark.timeout(60)


def job(job_id: int = 12, **changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "id": job_id,
        "name": f"file-{job_id}.iso",
        "url": f"https://example.org/{job_id}.iso",
        "status": "downloading",
        "kind": "direct",
        "downloaded": 100,
        "total": 1000,
        "speed": 50,
        "eta": 18,
        "createdAt": 1794700100,
        "error": None,
    }
    fields.update(changes)
    return fields


def report(jobs: list[dict[str, Any]], *, shared: bool = True, rid: str = "p-1") -> dict[str, Any]:
    return {"id": rid, "type": "phoneJobs", "shared": shared, "jobs": jobs}


# --------------------------------------------------------------- pure pieces


def test_store_replaces_and_forgets() -> None:
    store = PhoneJobStore()
    assert store.get("d1") is None
    first = store.replace("d1", True, [job(1), job(2)], 1000.7)
    assert first.received_at == 1000 and [j.id for j in first.jobs] == [1, 2]
    # A newer list replaces the older one, it is never merged into it.
    second = store.replace("d1", True, [job(3)], 1001)
    assert [j.id for j in second.jobs] == [3]
    assert store.get("d1") is second and len(store) == 1
    store.replace("d2", False, [], 1002)
    assert store.retain(["d2"]) == ["d1"]
    assert store.get("d1") is None and store.get("d2") is not None
    store.forget("d2")
    store.forget("d2")  # forgetting twice is fine
    assert len(store) == 0


def test_phone_job_fraction_and_unknown_total() -> None:
    assert PhoneJob.from_fields({**job(), "createdAt": 5}).fraction == pytest.approx(0.1)
    assert PhoneJob.from_fields({**job(total=None), "createdAt": 5}).fraction is None
    assert PhoneJob.from_fields({**job(downloaded=5000), "createdAt": 5}).fraction == 1.0


@pytest.mark.parametrize(
    ("status", "kind", "url", "expected"),
    [
        ("downloading", "direct", "https://x/y", ("pause", "cancel", "moveToPc")),
        ("queued", "video", "https://x/y", ("pause", "cancel", "moveToPc")),
        ("waiting", "direct", "https://x/y", ("pause", "cancel", "moveToPc")),
        ("paused", "torrent", "magnet:?xt=urn:btih:1", ("resume", "cancel", "moveToPc")),
        ("failed", "cloud", "sftp://h/f", ("retry", "remove", "moveToPc")),
        ("cancelled", "direct", "https://x/y", ("retry", "remove", "moveToPc")),
        ("completed", "direct", "https://x/y", ("remove", "moveToPc")),
        # A copy from a PC has nothing to move; neither does a job with no link.
        ("failed", "transfer", "", ("retry", "remove")),
        ("downloading", "transfer", "https://x/y", ("pause", "cancel")),
        ("paused", "direct", "", ("resume", "cancel")),
        # A status the PC does not know has no actions.
        ("unknown", "direct", "https://x/y", ()),
    ],
)
def test_actions_fit_the_status(
    status: str, kind: str, url: str, expected: tuple[str, ...]
) -> None:
    fields = {**job(status=status, kind=kind, url=url), "createdAt": 1}
    assert PhoneJob.from_fields(fields).actions() == expected
    assert set(expected) <= set(phonejobs.ACTIONS)


def test_command_limiter_allows_ten_a_second_per_device() -> None:
    now = [100.0]
    limiter = CommandLimiter(clock=lambda: now[0])
    assert [limiter.allow("a") for _ in range(12)] == [True] * 10 + [False] * 2
    assert limiter.allow("b")  # another device has its own budget
    now[0] += 0.5
    assert not limiter.allow("a")
    now[0] += 0.6  # a second after the first ten
    assert limiter.allow("a")
    limiter.forget("a")
    assert [limiter.allow("a") for _ in range(10)] == [True] * 10


# ------------------------------------------------------ through the listener


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


class Rig:
    """One paired phone and the signals the UI would see."""

    def __init__(self, env: Env) -> None:
        self.env = env
        self.phone, self.pin = env.pair()
        self.device = self.phone.device_id
        self.changed: list[str] = []
        self.finished: list[tuple[int, str, str]] = []
        self.online: list[str] = []
        env.service.phoneJobsChanged.connect(self.changed.append)
        env.service.commandFinished.connect(lambda i, r, m: self.finished.append((i, r, m)))
        env.service.deviceOnlineChanged.connect(self.online.append)

    def login(self, topics: list[str] | None = None) -> Client:
        client = self.env.login(self.phone, self.pin)
        if topics is not None:
            reply = self.env.ask(client, {"id": "s-1", "type": "subscribe", "topics": topics})
            assert reply["type"] == "subscribed", reply
        return client

    def send_report(self, client: Client, jobs: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
        return self.env.ask(client, report(jobs, **kw))

    def seed(self, jobs: list[dict[str, Any]] | None = None) -> None:
        """Store a list without a connection, as an earlier connection would."""
        self.env.service.handlers.phone.replace(
            self.device, True, jobs if jobs is not None else [job(12), job(11)], 1_700_000_000
        )


def command_event(client: Client) -> dict[str, Any]:
    return _wait_event(client, lambda e: e["event"] == "phone.command")


def test_phone_jobs_is_stored_acknowledged_and_announced(env: Env) -> None:
    rig = Rig(env)
    client = rig.login()
    assert env.service.phone_jobs(rig.device) is None  # an older phone sends nothing
    reply = rig.send_report(client, [job(12), job(11, status="completed", eta=None, speed=0)])
    assert reply == {"id": "p-1", "type": "ok", "protocolVersion": 6}
    assert rig.changed == [rig.device]
    snapshot = env.service.phone_jobs(rig.device)
    assert snapshot is not None and snapshot.shared is True
    assert [(j.id, j.status, j.name) for j in snapshot.jobs] == [
        (12, "downloading", "file-12.iso"),
        (11, "completed", "file-11.iso"),
    ]
    assert snapshot.received_at > 1_700_000_000


def test_a_new_list_replaces_the_old_one_and_not_shared_empties_it(env: Env) -> None:
    rig = Rig(env)
    client = rig.login()
    rig.send_report(client, [job(1), job(2), job(3)])
    rig.send_report(client, [job(3, status="paused", speed=0)], rid="p-2")
    snapshot = env.service.phone_jobs(rig.device)
    assert snapshot is not None
    assert [(j.id, j.status) for j in snapshot.jobs] == [(3, "paused")]
    reply = rig.send_report(client, [], shared=False, rid="p-3")
    assert reply["type"] == "ok"
    snapshot = env.service.phone_jobs(rig.device)
    assert snapshot is not None and snapshot.shared is False and snapshot.jobs == ()
    assert len(rig.changed) == 3


@pytest.mark.parametrize(
    "bad",
    [
        report([job(1), job(1)]),  # duplicate ids
        report([job(1, name="")]),
        report([job(1, status=7)]),
        report([job(n) for n in range(1, 102)]),  # 101 jobs
        report([job(1)], shared=False),  # not shared, so the list must be empty
        {"id": "p-9", "type": "phoneJobs", "jobs": []},
        {"id": "p-9", "type": "phoneJobs", "shared": True, "jobs": "none"},
    ],
)
def test_a_bad_list_is_refused_and_the_stored_list_stays(env: Env, bad: dict[str, Any]) -> None:
    rig = Rig(env)
    client = rig.login()
    rig.send_report(client, [job(5)])
    bad = {**bad, "id": "p-bad"}
    reply = env.ask(client, bad)
    assert reply["type"] == "error" and reply["code"] == "badRequest"
    assert reply["id"] == "p-bad"
    snapshot = env.service.phone_jobs(rig.device)
    assert snapshot is not None and [j.id for j in snapshot.jobs] == [5]
    assert len(rig.changed) == 1  # only the good list was announced


def test_a_full_list_of_longest_strings_fits_one_frame(env: Env) -> None:
    rig = Rig(env)
    client = rig.login()
    big = [
        job(
            n,
            name="n" * 256,
            url="https://example.org/" + "u" * 2028,
            error="e" * 512,
            status="failed",
        )
        for n in range(1, 101)
    ]
    assert rig.send_report(client, big)["type"] == "ok"
    snapshot = env.service.phone_jobs(rig.device)
    assert snapshot is not None and len(snapshot.jobs) == 100


def test_phone_jobs_costs_one_token_each(env: Env) -> None:
    """Section 9: 20 tokens a second, burst 40, shared with every other request."""
    rig = Rig(env)
    client = rig.login()
    replies: list[dict[str, Any]] = []
    for wave in range(5):  # waves of 20: the 32-unanswered cap is a different limit
        for n in range(20):
            client.send(report([job(1)], rid=f"f-{wave}-{n}"))
        replies += [client.message() for _ in range(20)]
    limited = [r for r in replies if r.get("code") == "rateLimited"]
    accepted = [r for r in replies if r["type"] == "ok"]
    assert len(accepted) + len(limited) == 100
    assert 40 <= len(accepted) <= 60 and limited
    assert all(r["retryAfterMs"] >= 1 for r in limited)


def test_a_phone_that_is_not_served_the_feature_gets_unsupported(env: Env) -> None:
    rig = Rig(env)
    client = rig.login()
    env.service.listener._features = ("jobs", "events")
    reply = rig.send_report(client, [job(1)])
    assert reply["type"] == "error" and reply["code"] == "unsupported"
    assert env.service.phone_jobs(rig.device) is None
    reply = env.ask(
        client,
        {
            "id": "c-1",
            "type": "phoneCommandResult",
            "commandId": 1,
            "result": "ok",
            "message": None,
        },
    )
    assert reply["code"] == "unsupported"


def test_removing_the_device_drops_its_list_and_waiting_commands(env: Env) -> None:
    rig = Rig(env)
    client = rig.login(["phone"])
    rig.send_report(client, [job(1)])
    command = env.service.send_phone_command(rig.device, 1, "pause")
    assert command_event(client)["commandId"] == command
    assert env.service.remove_device(rig.device) is True
    assert env.service.handlers.phone.get(rig.device) is None
    assert len(env.service.handlers.phone) == 0
    assert env.service.phone_jobs(rig.device) is None
    assert pump(lambda: (command, "offline", "") in rig.finished)
    # No later timeout, and a late answer from the removed phone is not heard.
    settle(0.05)
    assert [f for f in rig.finished if f[0] == command] == [(command, "offline", "")]


def test_a_phone_that_unpairs_itself_loses_its_list_too(env: Env) -> None:
    rig = Rig(env)
    client = rig.login()
    rig.send_report(client, [job(1)])
    assert env.ask(client, {"id": "u-1", "type": "unpair"})["type"] == "ok"
    assert pump(lambda: env.service.handlers.phone.get(rig.device) is None)
    assert env.service.store.count() == 0


def test_the_list_is_never_written_to_the_database(env: Env, tmp_path: Path) -> None:
    rig = Rig(env)
    client = rig.login()
    rig.send_report(client, [job(1, name="SECRET-HOLIDAY-VIDEO.mp4")])
    settle(0.05)
    env.db.close()
    raw = (tmp_path / "service-test.db").read_bytes()
    wal = tmp_path / "service-test.db-wal"
    if wal.exists():
        raw += wal.read_bytes()
    assert b"SECRET-HOLIDAY-VIDEO" not in raw
    # The fixture's own close() runs again after the test; reopen so it can.
    from app.db.database import Database

    env.db = Database(tmp_path / "service-test.db")


def test_online_state_follows_the_connection(env: Env) -> None:
    rig = Rig(env)
    assert env.service.is_online(rig.device) is False
    client = rig.login()
    assert pump(lambda: rig.online == [rig.device])
    assert env.service.is_online(rig.device) is True
    stamp = env.service.store.get(rig.device)
    assert stamp is not None
    client.close()
    assert pump(lambda: rig.online == [rig.device, rig.device])
    assert env.service.is_online(rig.device) is False


# ------------------------------------------------------------------- commands


def test_subscribe_accepts_the_phone_topic(env: Env) -> None:
    rig = Rig(env)
    client = rig.login()
    reply = env.ask(client, {"id": "s-2", "type": "subscribe", "topics": ["jobs", "phone", "x"]})
    assert reply["topics"] == ["jobs", "phone"]


def test_a_command_reaches_the_phone_and_the_result_comes_back(env: Env) -> None:
    rig = Rig(env)
    client = rig.login(["phone"])
    rig.send_report(client, [job(12), job(11)])
    command = env.service.send_phone_command(rig.device, 12, "pause")
    assert command >= 1
    event = command_event(client)
    assert event["type"] == "event" and event["protocolVersion"] == 6
    assert (event["commandId"], event["action"], event["jobId"]) == (command, "pause", 12)
    assert isinstance(event["seq"], int) and isinstance(event["ts"], int)
    assert rig.finished == []  # nothing is reported until the phone answers
    reply = env.ask(
        client,
        {
            "id": "c-1",
            "type": "phoneCommandResult",
            "commandId": command,
            "result": "refused",
            "message": "That can't be paused",
        },
    )
    assert reply == {"id": "c-1", "type": "ok", "protocolVersion": 6}
    assert rig.finished == [(command, "refused", "That can't be paused")]


def test_command_ids_are_unique_and_in_range(env: Env) -> None:
    rig = Rig(env)
    client = rig.login(["phone"])
    rig.seed()
    first = env.service.send_phone_command(rig.device, 12, "pause")
    second = env.service.send_phone_command(rig.device, 11, "cancel")
    assert 1 <= first < second <= 2**31 - 1
    assert command_event(client)["commandId"] == first
    assert command_event(client)["commandId"] == second


@pytest.mark.parametrize("action", ["pause", "resume", "cancel", "retry", "remove", "moveToPc"])
def test_every_action_is_sent_verbatim(env: Env, action: str) -> None:
    rig = Rig(env)
    client = rig.login(["phone"])
    rig.seed()
    assert env.service.send_phone_command(rig.device, 12, action) >= 1
    assert command_event(client)["action"] == action


def test_the_newest_subscribed_connection_gets_the_command(env: Env) -> None:
    rig = Rig(env)
    older = rig.login(["phone"])
    newer = rig.login(["phone"])
    rig.seed()
    command = env.service.send_phone_command(rig.device, 12, "resume")
    assert command_event(newer)["commandId"] == command
    with pytest.raises(AssertionError):
        _wait_event(older, lambda e: e["event"] == "phone.command", timeout=0.3)


def test_a_newer_connection_that_did_not_subscribe_is_skipped(env: Env) -> None:
    rig = Rig(env)
    older = rig.login(["jobs", "phone"])
    newer = rig.login(["jobs"])
    rig.seed()
    command = env.service.send_phone_command(rig.device, 12, "resume")
    assert command_event(older)["commandId"] == command
    with pytest.raises(AssertionError):
        _wait_event(newer, lambda e: e["event"] == "phone.command", timeout=0.3)


def test_a_connection_that_is_not_subscribed_to_phone_means_offline(env: Env) -> None:
    rig = Rig(env)
    client = rig.login(["jobs"])
    rig.seed()
    command = env.service.send_phone_command(rig.device, 12, "pause")
    assert pump(lambda: rig.finished == [(command, "offline", "")])
    with pytest.raises(AssertionError):
        _wait_event(client, lambda e: e["event"] == "phone.command", timeout=0.3)


def test_commands_go_only_to_the_device_they_name(env: Env) -> None:
    rig = Rig(env)
    mine = rig.login(["phone"])
    env.started.clear()
    env.pending.clear()
    other_phone, other_pin = env.pair()
    other = env.login(other_phone, other_pin)
    assert env.ask(other, {"id": "s-1", "type": "subscribe", "topics": ["phone"]})["type"] == (
        "subscribed"
    )
    rig.seed()
    command = env.service.send_phone_command(rig.device, 12, "pause")
    assert command_event(mine)["commandId"] == command
    with pytest.raises(AssertionError):
        _wait_event(other, lambda e: e["event"] == "phone.command", timeout=0.3)
    # The other phone cannot answer a command that was not sent to it...
    env.ask(
        other,
        {
            "id": "c-1",
            "type": "phoneCommandResult",
            "commandId": command,
            "result": "ok",
            "message": None,
        },
    )
    settle(0.05)
    assert rig.finished == []
    # ...and the right phone still can.
    env.ask(
        mine,
        {
            "id": "c-2",
            "type": "phoneCommandResult",
            "commandId": command,
            "result": "ok",
            "message": None,
        },
    )
    assert rig.finished == [(command, "ok", "")]


def test_an_unknown_or_repeated_result_is_ignored_but_acknowledged(env: Env) -> None:
    rig = Rig(env)
    client = rig.login(["phone"])
    rig.seed()
    ghost = {"type": "phoneCommandResult", "commandId": 999, "result": "ok", "message": None}
    assert env.ask(client, {"id": "g-1", **ghost})["type"] == "ok"
    command = env.service.send_phone_command(rig.device, 12, "pause")
    command_event(client)
    answer = {**ghost, "commandId": command}
    assert env.ask(client, {"id": "g-2", **answer})["type"] == "ok"
    assert env.ask(client, {"id": "g-3", **answer, "result": "failed"})["type"] == "ok"
    settle(0.05)
    assert rig.finished == [(command, "ok", "")]


def test_a_bad_result_is_refused(env: Env) -> None:
    rig = Rig(env)
    client = rig.login(["phone"])
    reply = env.ask(
        client,
        {
            "id": "b-1",
            "type": "phoneCommandResult",
            "commandId": 0,
            "result": "ok",
            "message": None,
        },
    )
    assert reply["code"] == "badRequest"
    reply = env.ask(
        client,
        {
            "id": "b-2",
            "type": "phoneCommandResult",
            "commandId": 1,
            "result": "yes",
            "message": None,
        },
    )
    assert reply["code"] == "badRequest"
    assert rig.finished == []


def test_a_command_the_phone_never_answers_times_out(env: Env) -> None:
    rig = Rig(env)
    client = rig.login(["phone"])
    rig.seed()
    env.service.command_timeout_ms = 80
    command = env.service.send_phone_command(rig.device, 12, "pause")
    command_event(client)
    assert pump(lambda: rig.finished == [(command, "timeout", "")])
    # A result that comes after the timeout is acknowledged and ignored.
    reply = env.ask(
        client,
        {
            "id": "late",
            "type": "phoneCommandResult",
            "commandId": command,
            "result": "ok",
            "message": None,
        },
    )
    assert reply["type"] == "ok"
    settle(0.05)
    assert rig.finished == [(command, "timeout", "")]


def test_the_default_timeout_is_fifteen_seconds(env: Env) -> None:
    assert env.service.command_timeout_ms == 15_000


def test_a_job_that_is_not_in_the_newest_list_is_not_sent(env: Env) -> None:
    rig = Rig(env)
    client = rig.login(["phone"])
    rig.seed([job(12)])
    command = env.service.send_phone_command(rig.device, 99, "cancel")
    assert pump(lambda: rig.finished == [(command, "notFound", "")])
    with pytest.raises(AssertionError):
        _wait_event(client, lambda e: e["event"] == "phone.command", timeout=0.3)


def test_a_phone_that_never_sent_a_list_gets_no_commands(env: Env) -> None:
    rig = Rig(env)
    rig.login(["phone"])
    command = env.service.send_phone_command(rig.device, 1, "pause")
    assert pump(lambda: rig.finished == [(command, "notFound", "")])


def test_invalid_commands_are_refused_without_an_id(env: Env) -> None:
    rig = Rig(env)
    rig.login(["phone"])
    rig.seed()
    assert env.service.send_phone_command(rig.device, 12, "format") == 0
    assert env.service.send_phone_command(rig.device, 12, "") == 0
    assert env.service.send_phone_command("A" * 22, 12, "pause") == 0
    assert env.service.send_phone_command("../etc", 12, "pause") == 0
    settle(0.05)
    assert rig.finished == []


def test_no_connection_at_all_means_offline(env: Env) -> None:
    rig = Rig(env)
    rig.seed()
    command = env.service.send_phone_command(rig.device, 12, "pause")
    assert pump(lambda: rig.finished == [(command, "offline", "")])


def test_at_most_ten_commands_a_second_to_one_device(env: Env) -> None:
    rig = Rig(env)
    client = rig.login(["phone"])
    rig.seed()
    env.service.command_timeout_ms = 60_000
    ids = [env.service.send_phone_command(rig.device, 12, "pause") for _ in range(12)]
    assert all(ids)
    assert pump(lambda: len(rig.finished) == 2)
    assert rig.finished == [(ids[10], "busy", ""), (ids[11], "busy", "")]
    seen = [command_event(client)["commandId"] for _ in range(10)]
    assert seen == ids[:10]
    with pytest.raises(AssertionError):
        _wait_event(client, lambda e: e["event"] == "phone.command", timeout=0.3)


def test_shutdown_ends_waiting_commands(env: Env) -> None:
    rig = Rig(env)
    rig.login(["phone"])
    rig.seed()
    command = env.service.send_phone_command(rig.device, 12, "pause")
    env.service.shutdown()
    assert (command, "offline", "") in rig.finished


def test_a_command_event_keeps_the_event_sequence(env: Env) -> None:
    """Commands share the connection's ``seq`` with the other events."""
    rig = Rig(env)
    client = rig.login(["phone", "global"])
    rig.seed()
    env.service.send_phone_command(rig.device, 12, "pause")
    env.service.send_phone_command(rig.device, 11, "pause")
    first = command_event(client)
    second = command_event(client)
    assert second["seq"] == first["seq"] + 1
