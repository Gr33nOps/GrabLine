"""The phone's unattended add path: every resolution kind queues (or fails)
without a single dialog, duplicates are skipped, and options are re-validated."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QEvent, QThread
from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox

from app.core.manager import AUTO_QUEUE, DownloadManager
from app.core.models import Handoff, JobKind, JobStatus
from app.core.probe import ProbeResult
from app.core.resolver import Resolution
from app.core.settings import Settings
from app.db.database import Database
from app.engines.manifest import HlsVariant
from app.engines.smart import MediaInfo, PlaylistEntry, PlaylistInfo, QualityOption
from app.tests.qt_app import ensure_app
from app.ui import remote_add
from app.ui.remote_add import AddOutcome, UnattendedAdder

URL = "https://example.com/video"


class _Job:
    def __init__(self, job_id: int, status: JobStatus = JobStatus.QUEUED) -> None:
        self.id = job_id
        self.status = status


class FakeManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.existing: _Job | None = None
        self._next = 100

    def find_existing(self, url: str) -> _Job | None:
        return self.existing

    def _record(self, name: str, *args: Any, **kwargs: Any) -> _Job:
        self.calls.append((name, args, kwargs))
        self._next += 1
        return _Job(self._next)

    def add_url(self, *args: Any, **kwargs: Any) -> _Job:
        return self._record("add_url", *args, **kwargs)

    def add_smart(self, *args: Any, **kwargs: Any) -> _Job:
        return self._record("add_smart", *args, **kwargs)

    def add_smart_entry(self, *args: Any, **kwargs: Any) -> _Job:
        return self._record("add_smart_entry", *args, **kwargs)

    def add_hls(self, *args: Any, **kwargs: Any) -> _Job:
        return self._record("add_hls", *args, **kwargs)

    def add_torrent(self, *args: Any, **kwargs: Any) -> _Job:
        return self._record("add_torrent", *args, **kwargs)

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


class FakeResolver:
    def __init__(self, resolution: Resolution | None = None) -> None:
        self.resolution = resolution
        self.calls: list[dict[str, Any]] = []

    def resolve(self, url: str, **kwargs: Any) -> Resolution:
        self.calls.append({"url": url, **kwargs})
        assert self.resolution is not None
        return self.resolution


def _options() -> tuple[QualityOption, ...]:
    return (
        QualityOption("Best", "video", "best"),
        QualityOption("1080p", "video", "f1080", height=1080),
        QualityOption("720p", "video", "f720", height=720),
    )


def _media(options: tuple[QualityOption, ...] | None = None) -> MediaInfo:
    return MediaInfo(
        url=URL,
        id="abc",
        title="A video",
        uploader=None,
        duration=10.0,
        thumbnail_url=None,
        options=_options() if options is None else options,
    )


def _handoff(url: str = URL, **options: Any) -> Handoff:
    return Handoff(
        id=1,
        url=url,
        page_url=None,
        page_title="Page title",
        source="phone",
        options=options,
    )


def _wait(condition: Callable[[], bool], timeout: float = 10.0) -> None:
    ensure_app()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError("condition not met in time")


class Rig:
    def __init__(
        self,
        db: Database,
        resolution: Resolution | None = None,
        *,
        folders: dict[str, Path] | None = None,
        queues: tuple[int, ...] = (),
    ) -> None:
        ensure_app()
        self.manager = FakeManager()
        self.resolver = FakeResolver(resolution)
        self.settings = Settings(db)
        self.outcomes: list[AddOutcome] = []
        known = folders or {}
        self.adder = UnattendedAdder(
            self.manager,  # type: ignore[arg-type]
            self.resolver,  # type: ignore[arg-type]
            self.settings,
            resolve_folder=known.get,
            queue_exists=lambda queue_id: queue_id in queues,
            on_outcome=self.outcomes.append,
        )

    def run(self, handoff: Handoff) -> AddOutcome:
        self.adder.add(handoff)
        _wait(lambda: bool(self.outcomes) and not self.adder.busy())
        for thread in self.adder.findChildren(QThread):
            thread.wait(5000)  # never destroy the adder under a still-exiting thread
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()
        assert len(self.outcomes) == 1
        return self.outcomes[0]


def test_direct_queues_with_probe_filename(db: Database) -> None:
    probe = ProbeResult(URL, True, 10, None, None, "file.bin", None)
    rig = Rig(db, Resolution(url=URL, kind=JobKind.DIRECT, probe=probe))
    outcome = rig.run(_handoff(requestId="r1"))
    assert (outcome.result, outcome.job_ids, outcome.request_id) == ("queued", (101,), "r1")
    name, args, kwargs = rig.manager.calls[0]
    assert name == "add_url" and args == (URL,)
    assert kwargs["filename"] == "file.bin" and kwargs["dest_dir"] is None
    assert kwargs["queue_id"] == AUTO_QUEUE


def test_smart_single_uses_requested_quality(db: Database) -> None:
    rig = Rig(db, Resolution(url=URL, kind=JobKind.SMART, media=_media()))
    outcome = rig.run(_handoff(quality="720p"))
    assert outcome.result == "queued"
    name, args, _kwargs = rig.manager.calls[0]
    assert name == "add_smart" and args[2].label == "720p"


def test_smart_single_without_quality_is_best(db: Database) -> None:
    rig = Rig(db, Resolution(url=URL, kind=JobKind.SMART, media=_media()))
    rig.run(_handoff())
    _name, args, _kwargs = rig.manager.calls[0]
    assert args[2].label == "Best"


def test_smart_single_without_listed_quality_uses_generic_tier_or_best(db: Database) -> None:
    media = _media((QualityOption("Best", "video", "best"),))
    rig = Rig(db, Resolution(url=URL, kind=JobKind.SMART, media=media))
    rig.run(_handoff())
    name, args, _kwargs = rig.manager.calls[0]
    assert name == "add_smart" and args[2].label.lower() == "best"
    # A quality the media doesn't list falls back to the generic tier of that name.
    rig = Rig(db, Resolution(url=URL, kind=JobKind.SMART, media=media))
    rig.run(_handoff(quality="720p"))
    assert rig.manager.calls[0][1][2].label == "720p"


def test_smart_playlist_adds_entries_up_to_cap(db: Database) -> None:
    entries = tuple(PlaylistEntry(f"https://example.com/v{i}", f"v{i}", None, i) for i in range(5))
    playlist = PlaylistInfo(URL, "List", None, entries)
    rig = Rig(db, Resolution(url=URL, kind=JobKind.SMART, playlist=playlist))
    rig.settings.playlist_batch_cap = 3
    outcome = rig.run(_handoff(quality="720p"))
    assert rig.manager.names() == ["add_smart_entry"] * 3
    assert [call[1][0] for call in rig.manager.calls] == [e.url for e in entries[:3]]
    assert all(call[1][2].label == "720p" for call in rig.manager.calls)
    assert outcome.result == "queued" and outcome.job_ids == (101, 102, 103)


def test_hls_picks_matching_variant_else_first(db: Database) -> None:
    variants = (
        HlsVariant("https://x/1080.m3u8", height=1080),
        HlsVariant("https://x/720.m3u8", 1, height=720),
    )
    rig = Rig(db, Resolution(url=URL, kind=JobKind.HLS, variants=variants))
    rig.run(_handoff(quality="720p"))
    assert rig.manager.calls[0][2]["variant"] is variants[1]
    rig = Rig(db, Resolution(url=URL, kind=JobKind.HLS, variants=variants))
    rig.run(_handoff(quality="480p"))
    assert rig.manager.calls[0][2]["variant"] is variants[0]


def test_resolved_torrent_is_added(db: Database) -> None:
    rig = Rig(db, Resolution(url="https://x/a.torrent", kind=JobKind.TORRENT))
    outcome = rig.run(_handoff())
    name, args, _kwargs = rig.manager.calls[0]
    assert name == "add_torrent" and args == ("https://x/a.torrent",)
    assert outcome.result == "queued" and outcome.url == URL


def test_unresolvable_fails_with_resolver_message(db: Database) -> None:
    rig = Rig(db, Resolution(url=URL, kind=None, message="nothing to grab here"))
    outcome = rig.run(_handoff())
    assert (outcome.result, outcome.message) == ("failed", "nothing to grab here")
    assert rig.manager.calls == []


def test_cloud_fails(db: Database) -> None:
    rig = Rig(db, Resolution(url=URL, kind=JobKind.CLOUD))
    outcome = rig.run(_handoff())
    assert outcome.result == "failed" and rig.manager.calls == []


def test_magnet_adds_immediately_without_resolving(db: Database) -> None:
    rig = Rig(db)
    magnet = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
    rig.adder.add(_handoff(magnet, requestId="m"))
    # Synchronous: no thread, no resolve.
    assert rig.manager.names() == ["add_torrent"]
    assert rig.resolver.calls == []
    assert [o.result for o in rig.outcomes] == ["queued"]
    assert not rig.adder.busy()


@pytest.mark.parametrize(
    "status", [JobStatus.QUEUED, JobStatus.DOWNLOADING, JobStatus.PAUSED, JobStatus.COMPLETED]
)
def test_duplicate_is_reported_and_nothing_added(db: Database, status: JobStatus) -> None:
    rig = Rig(db, Resolution(url=URL, kind=JobKind.DIRECT))
    rig.manager.existing = _Job(7, status)
    rig.adder.add(_handoff())
    assert [(o.result, o.job_ids) for o in rig.outcomes] == [("duplicate", (7,))]
    assert rig.manager.calls == [] and rig.resolver.calls == []


def test_failed_earlier_job_is_not_a_duplicate(db: Database) -> None:
    rig = Rig(db, Resolution(url=URL, kind=JobKind.DIRECT))
    rig.manager.existing = _Job(7, JobStatus.FAILED)
    assert rig.run(_handoff()).result == "queued"


def test_folder_id_resolved_or_default(db: Database, tmp_path: Path) -> None:
    resolution = Resolution(url=URL, kind=JobKind.DIRECT)
    rig = Rig(db, resolution, folders={"f1": tmp_path})
    rig.run(_handoff(folderId="f1"))
    assert rig.manager.calls[0][2]["dest_dir"] == str(tmp_path)
    rig = Rig(db, resolution, folders={"f1": tmp_path})
    rig.run(_handoff(folderId="unknown"))
    assert rig.manager.calls[0][2]["dest_dir"] is None


def test_queue_id_validated(db: Database) -> None:
    resolution = Resolution(url=URL, kind=JobKind.DIRECT)
    for value, expected in ((5, 5), (99, AUTO_QUEUE), (True, AUTO_QUEUE), ("5", AUTO_QUEUE)):
        rig = Rig(db, resolution, queues=(1, 5))
        rig.run(_handoff(queueId=value))
        assert rig.manager.calls[0][2]["queue_id"] == expected, value


def test_phone_headers_never_reach_the_resolver(db: Database) -> None:
    rig = Rig(db, Resolution(url=URL, kind=JobKind.DIRECT))
    handoff = _handoff()
    handoff.headers["Cookie"] = "session=secret"
    handoff.headers["Authorization"] = "Bearer x"
    rig.run(handoff)
    assert rig.resolver.calls[0]["headers"] is None
    assert "headers" not in rig.manager.calls[0][2] or not rig.manager.calls[0][2]["headers"]


def test_adder_never_shows_a_dialog(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_dialogs(monkeypatch)
    for resolution in (
        Resolution(url=URL, kind=JobKind.DIRECT),
        Resolution(url=URL, kind=JobKind.SMART, media=_media()),
        Resolution(url=URL, kind=None, message="nope"),
    ):
        rig = Rig(db, resolution)
        rig.run(_handoff())


def _forbid_dialogs(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ui.playlist_panel import PlaylistPanel
    from app.ui.quality_panel import QualityPanel
    from app.ui.torrent_dialog import AddTorrentDialog

    def boom(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("a dialog was opened for a phone link")

    for owner, names in (
        (QMessageBox, ("question", "warning", "information", "critical")),
        (QInputDialog, ("getItem", "getText")),
        (QFileDialog, ("getExistingDirectory", "getOpenFileName", "getSaveFileName")),
        (QualityPanel, ("exec", "open", "show")),
        (PlaylistPanel, ("exec", "open", "show")),
        (AddTorrentDialog, ("exec", "open", "show")),
    ):
        for name in names:
            monkeypatch.setattr(owner, name, boom)


def test_main_window_drain_routes_phone_handoffs_without_dialogs(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ui.main_window import MainWindow

    ensure_app()
    settings = Settings(db)
    settings.download_dir = tmp_path
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    window = MainWindow(manager, settings)
    try:
        resolver = FakeResolver(Resolution(url=URL, kind=JobKind.DIRECT))
        window.resolver = resolver  # type: ignore[assignment]
        added: list[str] = []

        class _Stub:
            id = 42

        def fake_add_url(url: str, *args: object, **kwargs: object) -> _Stub:
            added.append(url)
            return _Stub()

        monkeypatch.setattr(manager, "add_url", fake_add_url)
        outcomes: list[AddOutcome] = []
        monkeypatch.setattr(window, "_on_phone_outcome", outcomes.append)
        _forbid_dialogs(monkeypatch)

        db.add_handoff(URL, source=remote_add.PHONE_SOURCE, options={"requestId": "r9"})
        window._drain_handoffs()
        _wait(lambda: bool(outcomes))
        assert [(o.result, o.job_ids, o.request_id) for o in outcomes] == [("queued", (42,), "r9")]
        assert added == [URL]
        assert resolver.calls[0]["headers"] is None
    finally:
        window.shutdown()
        manager.shutdown()
