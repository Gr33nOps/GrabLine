"""Strict phone-request validation (PROTOCOL.md section 2)."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from app.remote import schema
from app.remote.schema import RequestError, validate

RID = "req-0001-7b1e4c1a9f3d4e55"
URL = "https://example.com/a.zip"


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def msg(kind: str, **fields: Any) -> dict[str, Any]:
    return {"id": "r-1", "type": kind, **fields}


def code_of(message: object) -> str:
    with pytest.raises(RequestError) as info:
        validate(message)
    return info.value.code


# ------------------------------------------------------------ happy paths

PUBLIC_KEY = b64(bytes(range(91)))
PROOF = b64(bytes(range(32)))
DEVICE = "KsucBZHeyJguN6H0BzJQiw"


def test_happy_paths_for_every_type():
    cases: dict[str, dict[str, Any]] = {
        "pair": {
            "protocolVersion": 6,
            "deviceName": "Pixel 8",
            "publicKey": PUBLIC_KEY,
            "codeProof": PROOF,
        },
        "auth": {"protocolVersion": 6, "deviceId": DEVICE, "signature": b64(b"\x01" * 70)},
        "ping": {},
        "download": {"url": URL, "requestId": RID},
        "status": {"urls": [URL]},
        "recent": {},
        "focus": {},
        "links": {"urls": [URL], "requestId": RID},
        "jobs": {},
        "control": {"jobId": 1, "action": "pause"},
        "global": {"action": "get"},
        "queues": {},
        "analyze": {"url": URL},
        "subscribe": {},
        "unsubscribe": {},
        "unpair": {},
        "files": {},
        "fetch": {"jobId": 1},
        "fetchCancel": {"streamId": 1},
        "fileHash": {"jobId": 1},
        "outbox": {},
        "outboxAck": {"itemIds": [1]},
        "phoneJobs": {"shared": True, "jobs": []},
        "phoneCommandResult": {"commandId": 1, "result": "ok", "message": None},
    }
    assert set(cases) == set(schema.VALIDATORS)
    for kind, fields in cases.items():
        request = validate(msg(kind, **fields))
        assert request.type == kind
        assert request.id == "r-1"


def test_download_fields_and_defaults():
    request = validate(msg("download", url=f"  {URL} ", requestId=RID, title="  T  "))
    assert request.fields == {
        "url": URL,
        "requestId": RID,
        "title": "T",
        "pageUrl": None,
        "quality": "best",
        "queueId": None,
        "folderId": None,
    }


def test_defaults_and_clamps():
    assert validate(msg("recent")).fields == {"limit": 5}
    assert validate(msg("jobs")).fields == {"limit": 50, "after": None, "filter": "all"}
    assert validate(msg("subscribe")).fields["topics"] == ["jobs", "global"]
    assert validate(msg("subscribe", topics=["outbox", "x", "jobs"])).fields["topics"] == [
        "jobs",
        "outbox",
    ]


def test_ignored_fields_are_dropped_not_stored():
    request = validate(
        msg("download", url=URL, requestId=RID, cookie="a=b", source="phone", futureField=1)
    )
    assert "cookie" not in request.fields and "source" not in request.fields
    assert "futureField" not in request.fields


def test_magnet_urls_only_where_allowed():
    magnet = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
    assert validate(msg("download", url=magnet, requestId=RID)).fields["url"] == magnet
    assert code_of(msg("download", url="magnet:?dn=x", requestId=RID)) == "badRequest"
    assert code_of(msg("analyze", url=magnet)) == "badRequest"
    assert code_of(msg("download", url=URL, requestId=RID, pageUrl=magnet)) == "badRequest"


def test_request_cost():
    assert validate(msg("analyze", url=URL)).cost == 5
    assert validate(msg("links", urls=[URL], requestId=RID)).cost == 5
    assert validate(msg("ping")).cost == 1


# ------------------------------------------------------- envelope and ids


def test_message_must_be_an_object():
    bads: list[object] = [None, [], "ping", 5]
    for bad in bads:
        assert code_of(bad) == "badRequest"


@pytest.mark.parametrize("bad_id", [None, "", 5, True, "a b", "x" * 65, "é", ["a"]])
def test_missing_or_invalid_id(bad_id: object):
    message = {"type": "ping"} if bad_id is None else {"id": bad_id, "type": "ping"}
    assert code_of(message) == "badRequest"


def test_id_of_64_characters_is_fine():
    assert validate({"id": "x" * 64, "type": "ping"}).id == "x" * 64


def test_request_id_helper():
    assert schema.request_id({"id": "ok-1"}) == "ok-1"
    assert schema.request_id({"id": "bad id"}) is None
    assert schema.request_id([]) is None


def test_missing_or_non_string_type_is_bad_request():
    assert code_of({"id": "r"}) == "badRequest"
    assert code_of({"id": "r", "type": 5}) == "badRequest"


def test_unknown_type_is_unsupported():
    assert code_of(msg("shutdownPc")) == "unsupported"
    assert code_of(msg("gallery", urls=[URL])) == "unsupported"


def _nest(depth: int) -> Any:
    value: Any = 1
    for _ in range(depth):
        value = {"a": value}
    return value


def test_nesting_over_eight_is_refused():
    assert validate(msg("ping", extra=_nest(6))).type == "ping"  # depth 8 in total
    assert code_of(msg("ping", extra=_nest(7))) == "badRequest"
    deep_list: Any = []
    for _ in range(9):
        deep_list = [deep_list]
    assert code_of(msg("ping", extra=deep_list)) == "badRequest"


@pytest.mark.parametrize(
    "name", ["path", "folder", "dir", "destDir", "dest_dir", "filename", "file", "saveAs"]
)
def test_path_fields_are_forbidden(name: str):
    assert code_of(msg("download", url=URL, requestId=RID, **{name: "/etc"})) == "forbidden"
    assert code_of(msg("ping", **{name: ""})) == "forbidden"


def test_forbidden_wins_over_every_other_check():
    # Bad id, unknown type and a bad field would each fail on their own.
    assert code_of({"id": "bad id", "type": "nope", "path": "C:\\x"}) == "forbidden"
    assert code_of({"type": "download", "url": 5, "folder": "/"}) == "forbidden"
    # Nested too deeply AND path-bearing: nesting is checked first by design.
    assert code_of(msg("ping", path="x")) == "forbidden"


# ----------------------------------------------------------- types, ranges


def test_bool_is_not_an_int():
    assert code_of(msg("download", url=URL, requestId=RID, queueId=True)) == "badRequest"
    assert code_of(msg("control", jobId=True, action="pause")) == "badRequest"
    assert code_of(msg("recent", limit=True)) == "badRequest"
    assert code_of(msg("outboxAck", itemIds=[True])) == "badRequest"


def test_ints_reject_floats_strings_and_out_of_range():
    for bad in (1.0, "1", 0, -1, 2**53):
        assert code_of(msg("control", jobId=bad, action="pause")) == "badRequest"
    assert validate(msg("control", jobId=2**53 - 1, action="pause")).fields["jobId"] == 2**53 - 1
    assert code_of(msg("recent", limit=0)) == "badRequest"
    assert code_of(msg("recent", limit=21)) == "badRequest"
    assert validate(msg("recent", limit=20)).fields["limit"] == 20
    assert code_of(msg("jobs", limit=101)) == "badRequest"


def test_strings_no_nul_and_length_in_utf16_units():
    assert code_of(msg("download", url=URL, requestId=RID, title="a\x00b")) == "badRequest"
    assert code_of(msg("download", url=URL, requestId=RID, title=5)) == "badRequest"
    assert validate(msg("download", url=URL, requestId=RID, title="a" * 512)).fields["title"]
    assert code_of(msg("download", url=URL, requestId=RID, title="a" * 513)) == "badRequest"
    # An emoji is two UTF-16 units: 256 of them fit, 257 do not.
    assert validate(msg("download", url=URL, requestId=RID, title="\U0001f600" * 256))
    assert code_of(msg("download", url=URL, requestId=RID, title="\U0001f600" * 257)) == (
        "badRequest"
    )
    # Length is measured after trimming.
    assert validate(msg("download", url=URL, requestId=RID, title="  " + "a" * 512 + "  "))


def test_urls():
    for bad in ("", "ftp://x/y", "file:///C:/x", "https://", "http:///x", "javascript:alert(1)"):
        assert code_of(msg("download", url=bad, requestId=RID)) == "badRequest", bad
    assert code_of(msg("download", url=42, requestId=RID)) == "badRequest"
    assert code_of(msg("download", url="https://a.example/\x00", requestId=RID)) == "badRequest"
    long_ok = "https://example.com/" + "a" * (8192 - len("https://example.com/"))
    assert validate(msg("download", url=long_ok, requestId=RID))
    assert code_of(msg("download", url=long_ok + "a", requestId=RID)) == "badRequest"
    assert code_of(msg("download", requestId=RID)) == "badRequest"


def test_quality_set():
    for good in schema.QUALITIES:
        assert (
            validate(msg("download", url=URL, requestId=RID, quality=good)).fields["quality"]
            == good
        )
    assert {
        "best", "2160p", "1440p", "1080p", "720p", "480p", "360p", "mp3", "m4a", "flac"
    } == schema.QUALITIES  # fmt: skip
    for bad in ("4320p", "BEST", "", ["best"], 720, "best "):
        assert code_of(msg("download", url=URL, requestId=RID, quality=bad)) == "badRequest"
    # null means absent
    assert validate(msg("download", url=URL, requestId=RID, quality=None)).fields["quality"] == (
        "best"
    )


def test_request_id_pattern():
    for good in ("a" * 16, "A_-" * 21 + "A", "a" * 64):
        assert validate(msg("download", url=URL, requestId=good))
    for bad in ("a" * 15, "a" * 65, "a" * 15 + " ", "a" * 15 + "é", "a" * 15 + "\n", 5, ""):
        assert code_of(msg("download", url=URL, requestId=bad)) == "badRequest", repr(bad)
    assert code_of(msg("links", urls=[URL])) == "badRequest"


def test_folder_id_pattern():
    assert validate(msg("download", url=URL, requestId=RID, folderId="fAbc_-9")).fields["folderId"]
    assert validate(msg("download", url=URL, requestId=RID, folderId="a" * 32))
    for bad in ("a" * 33, "", "C:\\Windows", "../../x", "a/b", "a b", 5):
        assert code_of(msg("download", url=URL, requestId=RID, folderId=bad)) == "badRequest"


def test_queue_id():
    assert validate(msg("download", url=URL, requestId=RID, queueId=7)).fields["queueId"] == 7
    assert code_of(msg("download", url=URL, requestId=RID, queueId=0)) == "badRequest"
    assert code_of(msg("download", url=URL, requestId=RID, queueId="2")) == "badRequest"


# ------------------------------------------------------------ list limits


def test_links_lists():
    many = [f"https://example.com/{n}" for n in range(301)]
    assert code_of(msg("links", urls=many, requestId=RID)) == "tooLarge"
    ok = validate(msg("links", urls=many[:300], requestId=RID))
    assert len(ok.fields["urls"]) == 300
    # Duplicates and invalid entries are dropped, order kept.
    dedup = validate(msg("links", urls=[URL, "nope", URL, "https://b.example/"], requestId=RID))
    assert dedup.fields["urls"] == [URL, "https://b.example/"]
    assert code_of(msg("links", urls=["nope"], requestId=RID)) == "badRequest"
    assert code_of(msg("links", urls=[], requestId=RID)) == "badRequest"
    assert code_of(msg("links", urls="x", requestId=RID)) == "badRequest"


def test_status_list_limit():
    assert validate(msg("status", urls=[f"https://e.example/{n}" for n in range(50)]))
    assert code_of(msg("status", urls=[f"https://e.example/{n}" for n in range(51)])) == "tooLarge"
    assert code_of(msg("status")) == "badRequest"


def test_outbox_ack_limits():
    assert code_of(msg("outboxAck", itemIds=list(range(1, 102)))) == "tooLarge"
    assert len(validate(msg("outboxAck", itemIds=list(range(1, 101)))).fields["itemIds"]) == 100
    assert code_of(msg("outboxAck", itemIds=[])) == "badRequest"
    assert code_of(msg("outboxAck", itemIds=[0])) == "badRequest"
    assert code_of(msg("outboxAck", itemIds=["1"])) == "badRequest"
    assert validate(msg("outboxAck", itemIds=[3, 3, 4])).fields["itemIds"] == [3, 4]


def test_subscribe_topics():
    assert code_of(msg("subscribe", topics="jobs")) == "badRequest"
    assert code_of(msg("subscribe", topics=["jobs"] * 17)) == "badRequest"


# ------------------------------------------------------ jobs, control, global


def test_jobs_filter_and_limit():
    for flt in ("all", "active", "finished", "failed"):
        assert validate(msg("jobs", filter=flt)).fields["filter"] == flt
    assert validate(msg("jobs", filter=None)).fields["filter"] == "all"
    assert code_of(msg("jobs", filter="done")) == "badRequest"
    assert code_of(msg("jobs", filter=["all"])) == "badRequest"
    assert validate(msg("jobs", limit=100)).fields["limit"] == 100


def test_control_actions():
    for action in ("pause", "resume", "cancel", "retry", "remove"):
        assert validate(msg("control", jobId=3, action=action)).fields["action"] == action
    assert code_of(msg("control", jobId=3, action="deleteFile")) == "badRequest"
    assert code_of(msg("control", jobId=3)) == "badRequest"
    assert code_of(msg("control", jobId="C:\\Users\\me\\file.bin", action="remove")) == "badRequest"
    assert code_of(msg("control", action="pause")) == "badRequest"


def test_global_actions():
    for action in ("get", "pauseAll", "resumeAll", "clearSpeedLimit"):
        assert validate(msg("global", action=action)).fields["bytesPerSecond"] is None
    assert code_of(msg("global", action="reboot")) == "badRequest"
    assert code_of(msg("global", action="setSpeedLimit")) == "badRequest"
    assert code_of(msg("global", action="setSpeedLimit", bytesPerSecond=1023)) == "badRequest"
    assert code_of(msg("global", action="setSpeedLimit", bytesPerSecond=10**10 + 1)) == (
        "badRequest"
    )
    assert (
        validate(msg("global", action="setSpeedLimit", bytesPerSecond=1024)).fields[
            "bytesPerSecond"
        ]
        == 1024
    )
    # Ignored for other actions, even if wrong-looking but well-typed.
    assert validate(msg("global", action="get", bytesPerSecond=5)).fields["bytesPerSecond"] is None


def test_fetch_fields():
    request = validate(msg("fetch", jobId=4, offset=10, length=5, etag="abc"))
    assert request.fields == {"jobId": 4, "offset": 10, "length": 5, "etag": "abc"}
    assert code_of(msg("fetch", jobId=4, offset=-1)) == "badRequest"
    assert code_of(msg("fetch", jobId=4, length=0)) == "badRequest"
    assert code_of(msg("fetch", jobId=4, etag="a" * 65)) == "badRequest"
    assert code_of(msg("fetchCancel", streamId=2**32)) == "badRequest"


# ------------------------------------------------------------- base64url


def test_b64url_decode_strictness():
    assert schema.b64url_decode("AQID", "x") == b"\x01\x02\x03"
    assert schema.b64url_decode("", "x") == b""
    assert schema.b64url_decode(b64(b"\xfb\xff\xfe"), "x") == b"\xfb\xff\xfe"
    for bad in (
        "AQID=",  # padding
        "AQ==",
        "AQI+",  # standard alphabet
        "AQI/",
        "A",  # length % 4 == 1
        "AQIDB",
        "AQ ID",
        "AQ\n",
        "ü",
        5,
        None,
    ):
        with pytest.raises(RequestError) as info:
            schema.b64url_decode(bad, "x")
        assert info.value.code == "badRequest", repr(bad)


def test_b64url_rejects_non_canonical_trailing_bits():
    # "AQ" is canonical for b"\x01"; "AR" decodes to the same byte with stray bits.
    assert schema.b64url_decode("AQ", "x") == b"\x01"
    with pytest.raises(RequestError):
        schema.b64url_decode("AR", "x")
    with pytest.raises(RequestError):
        schema.b64url_decode("AQJ", "x")  # b"\x01\x02" is "AQI"


def test_b64url_exact_and_most_lengths():
    assert schema.b64url_decode(b64(b"a" * 32), "x", exact=32)
    with pytest.raises(RequestError):
        schema.b64url_decode(b64(b"a" * 31), "x", exact=32)
    with pytest.raises(RequestError):
        schema.b64url_decode(b64(b"a" * 73), "x", most=72)
    assert schema.b64url_decode(b64(b"a" * 72), "x", most=72)


def test_pair_and_auth_bytes_rules():
    base = {
        "protocolVersion": 6,
        "deviceName": "Pixel",
        "publicKey": PUBLIC_KEY,
        "codeProof": PROOF,
    }
    assert validate(msg("pair", **base)).fields["publicKey"] == bytes(range(91))
    assert code_of(msg("pair", **{**base, "publicKey": PUBLIC_KEY + "="})) == "badRequest"
    assert code_of(msg("pair", **{**base, "publicKey": b64(b"x" * 90)})) == "badRequest"
    assert code_of(msg("pair", **{**base, "codeProof": b64(b"x" * 31)})) == "badRequest"
    assert code_of(msg("pair", **{**base, "deviceName": ""})) == "badRequest"
    assert code_of(msg("pair", **{**base, "deviceName": "x" * 65})) == "badRequest"
    auth = {"protocolVersion": 6, "deviceId": DEVICE, "signature": b64(b"s" * 70)}
    assert validate(msg("auth", **auth))
    assert code_of(msg("auth", **{**auth, "deviceId": DEVICE[:-1]})) == "badRequest"
    assert code_of(msg("auth", **{**auth, "signature": b64(b"s" * 73)})) == "badRequest"
    assert code_of(msg("auth", **{**auth, "protocolVersion": "6"})) == "badRequest"


# ---------------------------------------------------------------- cursors


def test_cursor_round_trip():
    for value in (1, 39, 123456789, 2**53 - 1):
        token = schema.encode_cursor(value)
        assert len(token) <= 64 and "=" not in token
        assert schema.decode_cursor(token) == value


def test_cursor_matches_the_shared_fixture():
    assert schema.decode_cursor("Mzk") == 39
    assert schema.encode_cursor(39) == "Mzk"


def test_cursor_rejects_malformed_tokens():
    for bad in (
        "",
        "j39",
        "Mzk=",
        "!!",
        b64(b"0"),
        b64(b"039"),
        b64(b"-5"),
        b64(b"3.5"),
        b64(b"x" * 17),
        b64(b"9" * 16),  # beyond 2**53 - 1
        "A" * 65,
        39,
        None,
    ):
        with pytest.raises(RequestError) as info:
            schema.decode_cursor(bad)
        assert info.value.code == "badRequest", repr(bad)


def test_cursor_in_jobs_and_files_messages():
    assert validate(msg("jobs", cursor="Mzk")).fields["after"] == 39
    assert validate(msg("files", cursor="Mzk")).fields["after"] == 39
    assert code_of(msg("jobs", cursor="zzz!")) == "badRequest"
    assert code_of(msg("files", cursor=39)) == "badRequest"
    assert validate(msg("jobs", cursor=None)).fields["after"] is None


# ------------------------------------------------- phoneJobs (section 20)


def phone_job(job_id: int = 12, **changes: Any) -> dict[str, Any]:
    job: dict[str, Any] = {
        "id": job_id,
        "name": "ubuntu.iso",
        "url": "https://example.org/ubuntu.iso",
        "status": "downloading",
        "kind": "direct",
        "downloaded": 10,
        "total": 100,
        "speed": 5,
        "eta": 18,
        "createdAt": 1794700100,
        "error": None,
    }
    job.update(changes)
    return job


def test_phone_jobs_happy_path_and_unknown_words():
    fields = validate(
        msg(
            "phoneJobs",
            shared=True,
            jobs=[
                phone_job(1, name="  a.iso  ", futureField=1),
                phone_job(2, status="stuck", kind="smoke signal"),
                phone_job(3, total=None, eta=None, error="  It failed  ", status="failed"),
            ],
        )
    ).fields
    first, second, third = fields["jobs"]
    assert first["name"] == "a.iso" and "futureField" not in first
    assert second["status"] == "unknown" and second["kind"] == "direct"
    assert third["total"] is None and third["error"] == "It failed"
    assert validate(msg("phoneJobs", shared=False, jobs=[])).fields == {"shared": False, "jobs": []}


@pytest.mark.parametrize(
    "changes",
    [
        {"id": 0},
        {"id": True},
        {"id": "1"},
        {"name": ""},
        {"name": " "},
        {"name": "x" * 257},
        {"name": None},
        {"url": "x" * 2049},
        {"url": None},
        {"status": 5},
        {"status": ""},
        {"kind": None},
        {"downloaded": -1},
        {"downloaded": 1.5},
        {"total": -1},
        {"total": "5"},
        {"speed": -1},
        {"eta": -3},
        {"createdAt": None},
        {"error": 5},
        {"error": "x" * 513},
    ],
)
def test_phone_jobs_rejects_bad_job_fields(changes: dict[str, Any]):
    assert code_of(msg("phoneJobs", shared=True, jobs=[phone_job(**changes)])) == "badRequest"


@pytest.mark.parametrize("name", ["name", "total", "eta", "error", "createdAt", "speed", "url"])
def test_phone_jobs_rejects_a_missing_field(name: str):
    job = phone_job()
    del job[name]
    assert code_of(msg("phoneJobs", shared=True, jobs=[job])) == "badRequest"


def test_phone_jobs_rejects_bad_lists():
    assert code_of(msg("phoneJobs", jobs=[])) == "badRequest"  # shared missing
    assert code_of(msg("phoneJobs", shared="yes", jobs=[])) == "badRequest"
    assert code_of(msg("phoneJobs", shared=1, jobs=[])) == "badRequest"
    assert code_of(msg("phoneJobs", shared=True)) == "badRequest"
    assert code_of(msg("phoneJobs", shared=True, jobs={})) == "badRequest"
    assert code_of(msg("phoneJobs", shared=True, jobs=[5])) == "badRequest"
    assert code_of(msg("phoneJobs", shared=True, jobs=[phone_job(1), phone_job(1)])) == "badRequest"
    # Not shared, so the list must be empty.
    assert code_of(msg("phoneJobs", shared=False, jobs=[phone_job()])) == "badRequest"
    # At most 100 jobs.
    assert len(validate(msg("phoneJobs", shared=True, jobs=many(100))).fields["jobs"]) == 100
    assert code_of(msg("phoneJobs", shared=True, jobs=many(101))) == "badRequest"


def many(count: int) -> list[dict[str, Any]]:
    return [phone_job(n) for n in range(1, count + 1)]


def test_phone_jobs_and_results_cost_one_token():
    assert validate(msg("phoneJobs", shared=True, jobs=[])).cost == 1
    assert validate(msg("phoneCommandResult", commandId=1, result="ok", message=None)).cost == 1


def test_phone_command_result_validation():
    ok = validate(
        msg("phoneCommandResult", commandId=2**31 - 1, result="refused", message=" no ")
    ).fields
    assert ok == {"commandId": 2**31 - 1, "result": "refused", "message": "no"}
    base: dict[str, Any] = {"commandId": 3, "result": "ok", "message": None}
    for changes in (
        {"commandId": 0},
        {"commandId": 2**31},
        {"commandId": True},
        {"commandId": "3"},
        {"result": "done"},
        {"result": None},
        {"message": 5},
        {"message": "x" * 257},
    ):
        assert code_of(msg("phoneCommandResult", **{**base, **changes})) == "badRequest", changes
    missing = dict(base)
    del missing["message"]
    assert code_of(msg("phoneCommandResult", **missing)) == "badRequest"
    assert validate(msg("phoneCommandResult", **{**base, "message": "x" * 256})).fields["message"]


def test_subscribe_knows_the_phone_topic():
    fields = validate(msg("subscribe", topics=["phone", "jobs", "nope"])).fields
    assert fields["topics"] == ["jobs", "phone"]
    assert validate(msg("subscribe")).fields["topics"] == ["jobs", "global"]
