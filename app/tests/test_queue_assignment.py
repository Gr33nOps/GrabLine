"""Picking a queue when a download is added, and the scheduler actually
obeying it.

The reported bug was "I add several downloads and they all start at once even
though they are supposed to be in a queue". The scheduler's per-queue limit was
already correct; what was missing was the step *before* it - nothing on any add
path could say which queue a new job belonged to, so every download landed in
the default queue and the only limit that ever applied was the global
``max_concurrent`` (3 by default). Hence: three downloads, three at once.

These tests run against the real scheduler and a real local server. The
concurrency assertions are sampled continuously from a watcher thread, so
"never two at once" means never - not merely "not when we happened to look".
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path

from app.core.manager import AUTO_QUEUE, DownloadManager
from app.core.models import Job, JobStatus, Queue
from app.db.database import Database
from app.tests.conftest import wait_for
from app.tests.media_server import MediaServer, payload

#: A download slow enough that a second one starting alongside it is visible.
_SLOW = {"chunk_size": 32 * 1024, "delay_per_chunk": 0.04}


def _status(db: Database, job_id: int) -> JobStatus:
    job = db.get_job(job_id)
    assert job is not None
    return job.status


def _edit(db: Database, queue: Queue, **changes: object) -> Queue:
    updated = replace(queue, **changes)  # type: ignore[arg-type]
    db.update_queue(updated)
    fresh = db.get_queue(queue.id)
    assert fresh is not None
    return fresh


class _ConcurrencyWatcher:
    """Samples how many of ``job_ids`` are DOWNLOADING, continuously.

    A single assertion after a sleep can miss a transient double-start; this
    keeps the high-water mark and the order jobs were first seen running, which
    is what the sequential requirement is really about.
    """

    def __init__(self, db: Database, job_ids: list[int], interval: float = 0.01) -> None:
        self._db = db
        self._job_ids = job_ids
        self._interval = interval
        self._stop = threading.Event()
        self.peak = 0
        self.start_order: list[int] = []
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self) -> _ConcurrencyWatcher:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            running = [
                job_id
                for job_id in self._job_ids
                if _status(self._db, job_id) is JobStatus.DOWNLOADING
            ]
            self.peak = max(self.peak, len(running))
            for job_id in running:
                if job_id not in self.start_order:
                    self.start_order.append(job_id)
            self._stop.wait(self._interval)


# ============================================================ A + B. assignment


def test_an_explicitly_chosen_queue_reaches_the_job_row(db: Database, dest: Path):
    """The whole point: dialog -> add_url -> database -> Job.queue_id."""
    queue = db.create_queue("Sequential Test")
    manager = DownloadManager(db, max_concurrent=1)
    try:
        manager.settings.auto_start_downloads = False  # keep the scheduler out of it
        job = manager.add_url(
            "http://x.test/a.bin", dest_dir=str(dest), filename="a.bin", queue_id=queue.id
        )
        stored = db.get_job(job.id)
        assert stored is not None and stored.queue_id == queue.id
    finally:
        manager.shutdown()


def test_every_add_path_carries_the_chosen_queue(db: Database, dest: Path):
    """Not just add_url: a stream, a video and a torrent go the same way, so
    no entry path silently drops the choice."""
    from app.engines.smart import QualityOption

    queue = db.create_queue("Everything")
    manager = DownloadManager(db, max_concurrent=1)
    try:
        manager.settings.auto_start_downloads = False
        option = QualityOption(label="Best", kind="video", format_spec="b")
        jobs = [
            manager.add_url(
                "http://x.test/f.bin", dest_dir=str(dest), filename="f.bin", queue_id=queue.id
            ),
            manager.add_hls(
                "http://x.test/s.m3u8", dest_dir=str(dest), title="s", queue_id=queue.id
            ),
            manager.add_smart_entry(
                "http://x.test/v", "v", option, dest_dir=str(dest), queue_id=queue.id
            ),
            manager.add_torrent("magnet:?xt=urn:btih:" + "0" * 40, queue_id=queue.id),
        ]
        for job in jobs:
            stored = db.get_job(job.id)
            assert stored is not None and stored.queue_id == queue.id
    finally:
        manager.shutdown()


def test_default_is_an_explicit_choice_and_beats_the_category_rule(db: Database, dest: Path):
    """Picking "Default" in the dialog must mean Default. Before, a category
    queue would silently claim the download anyway."""
    video_queue = db.create_queue("Videos")
    _edit(db, video_queue, category="Video")
    manager = DownloadManager(db, max_concurrent=1)
    try:
        manager.settings.auto_start_downloads = False
        # No choice made -> the category rule applies, exactly as before.
        auto = manager.add_url("http://x.test/a.mp4", dest_dir=str(dest), filename="a.mp4")
        assert db.get_job(auto.id).queue_id == video_queue.id  # type: ignore[union-attr]
        # "Default" chosen -> no named queue, category rule not applied.
        chosen = manager.add_url(
            "http://x.test/b.mp4", dest_dir=str(dest), filename="b.mp4", queue_id=None
        )
        assert db.get_job(chosen.id).queue_id is None  # type: ignore[union-attr]
    finally:
        manager.shutdown()


def test_a_queue_deleted_between_choosing_and_confirming_falls_back(db: Database, dest: Path):
    manager = DownloadManager(db, max_concurrent=1)
    try:
        manager.settings.auto_start_downloads = False
        gone = db.create_queue("Temporary")
        db.delete_queue(gone.id)
        job = manager.add_url(
            "http://x.test/a.bin", dest_dir=str(dest), filename="a.bin", queue_id=gone.id
        )
        assert db.get_job(job.id).queue_id is None  # type: ignore[union-attr]
    finally:
        manager.shutdown()


def test_the_add_download_dialog_returns_the_selected_queue(db: Database):
    """The UI half of the contract: what the dialog hands back is a real queue
    id the manager can use, and Default comes back as None."""
    from PySide6.QtWidgets import QApplication

    from app.ui.add_download_dialog import AddDownloadDialog, queue_choices

    instance = QApplication.instance()
    if not isinstance(instance, QApplication):
        instance = QApplication([])

    manager = DownloadManager(db, max_concurrent=1)
    try:
        first = manager.create_queue("Queue 1")
        second = manager.create_queue("Queue 2")
        choices = queue_choices(manager)
        assert [name for _id, name in choices][1:] == ["Queue 1", "Queue 2"]
        assert choices[0][0] is None  # Default first

        dialog = AddDownloadDialog(
            "http://x.test/a.bin",
            suggested_name="a.bin",
            category="Documents",
            download_dir="/tmp",
            queues=choices,
        )
        assert dialog.chosen_queue() is None  # defaults to Default
        assert dialog.ignore_certificate_errors() is False

        dialog._queue.setCurrentIndex(2)
        assert dialog.chosen_queue() == second.id
        dialog._queue.setCurrentIndex(1)
        assert dialog.chosen_queue() == first.id
        dialog._insecure.setChecked(True)
        assert dialog.ignore_certificate_errors() is True
        dialog.deleteLater()
    finally:
        manager.shutdown()


# ======================================================== C. really sequential


def test_a_sequential_queue_runs_exactly_one_at_a_time(
    server: MediaServer, db: Database, dest: Path
):
    """The headline scenario: three downloads in a "Downloads at once = 1"
    queue run A, then B, then C - never two together, not even for an instant,
    and not even though the global limit would happily allow three."""
    urls = [server.add(f"/seq{i}.bin", payload(600_000, i), **_SLOW) for i in range(3)]
    queue = db.create_queue("Sequential Test")
    _edit(db, queue, max_concurrent=1)
    jobs = [
        db.create_job(url, str(dest), f"seq{i}.bin", queue_id=queue.id, options={"connections": 1})
        for i, url in enumerate(urls)
    ]
    ids = [job.id for job in jobs]
    # Global room for all three: only the queue's own limit may hold them back.
    manager = DownloadManager(db, max_concurrent=5)
    try:
        with _ConcurrencyWatcher(db, ids) as watcher:
            wait_for(lambda: _status(db, ids[0]) is JobStatus.DOWNLOADING, timeout=30)
            # A is running; B and C must be waiting, not merely "about to".
            assert _status(db, ids[1]) is JobStatus.QUEUED
            assert _status(db, ids[2]) is JobStatus.QUEUED

            wait_for(lambda: _status(db, ids[0]) is JobStatus.COMPLETED, timeout=60)
            wait_for(lambda: _status(db, ids[1]) is JobStatus.DOWNLOADING, timeout=30)
            assert _status(db, ids[2]) is JobStatus.QUEUED  # C still waits

            wait_for(lambda: _status(db, ids[1]) is JobStatus.COMPLETED, timeout=60)
            wait_for(lambda: _status(db, ids[2]) is JobStatus.DOWNLOADING, timeout=30)
            wait_for(
                lambda: all(_status(db, i) is JobStatus.COMPLETED for i in ids),
                timeout=60,
            )

        assert watcher.peak == 1, f"{watcher.peak} downloads ran at once in a sequential queue"
        assert watcher.start_order == ids  # A, then B, then C
    finally:
        manager.shutdown()


def test_many_jobs_added_at_once_cannot_slip_past_the_limit(
    server: MediaServer, db: Database, dest: Path
):
    """The race the report describes: several adds landing in the same instant.
    The scheduler decides under one lock, so a burst cannot outrun it."""
    queue = db.create_queue("Burst")
    _edit(db, queue, max_concurrent=1)
    manager = DownloadManager(db, max_concurrent=8)
    urls = [server.add(f"/burst{i}.bin", payload(300_000, 20 + i), **_SLOW) for i in range(6)]
    try:
        created: list[Job] = []
        lock = threading.Lock()
        barrier = threading.Barrier(len(urls))

        def add(index: int, url: str) -> None:
            barrier.wait(timeout=10)  # all six adds fire together
            job = manager.add_url(
                url,
                dest_dir=str(dest),
                filename=f"burst{index}.bin",
                queue_id=queue.id,
            )
            with lock:
                created.append(job)

        workers = [
            threading.Thread(target=add, args=(i, url), daemon=True) for i, url in enumerate(urls)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)

        ids = [job.id for job in created]
        assert len(ids) == len(urls)
        with _ConcurrencyWatcher(db, ids) as watcher:
            wait_for(
                lambda: all(_status(db, i) is JobStatus.COMPLETED for i in ids),
                timeout=120,
            )
        assert watcher.peak == 1, f"{watcher.peak} ran at once despite max_concurrent=1"
    finally:
        manager.shutdown()


# ============================================================== D. parallel


def test_a_parallel_queue_runs_up_to_its_limit_and_no_further(
    server: MediaServer, db: Database, dest: Path
):
    """Downloads at once = 2: A and B together, C waits for a free slot."""
    urls = [server.add(f"/par{i}.bin", payload(700_000, 30 + i), **_SLOW) for i in range(3)]
    queue = db.create_queue("Parallel")
    _edit(db, queue, max_concurrent=2)
    ids = [
        db.create_job(
            url, str(dest), f"par{i}.bin", queue_id=queue.id, options={"connections": 1}
        ).id
        for i, url in enumerate(urls)
    ]
    manager = DownloadManager(db, max_concurrent=6)
    try:
        with _ConcurrencyWatcher(db, ids) as watcher:
            wait_for(
                lambda: sum(_status(db, i) is JobStatus.DOWNLOADING for i in ids) == 2,
                timeout=30,
            )
            assert _status(db, ids[2]) is JobStatus.QUEUED  # C waits for a slot
            wait_for(
                lambda: all(_status(db, i) is JobStatus.COMPLETED for i in ids),
                timeout=120,
            )
        assert watcher.peak == 2, f"peak was {watcher.peak}, expected at most 2"
        assert set(watcher.start_order) == set(ids)  # C did eventually start
    finally:
        manager.shutdown()


def test_changing_downloads_at_once_takes_effect_without_a_restart(
    server: MediaServer, db: Database, dest: Path
):
    """Queue Manager -> Downloads at once is read by the scheduler on every
    pass, so raising it releases waiting jobs immediately."""
    urls = [server.add(f"/live{i}.bin", payload(800_000, 40 + i), **_SLOW) for i in range(3)]
    queue = db.create_queue("Live")
    queue = _edit(db, queue, max_concurrent=1)
    ids = [
        db.create_job(
            url, str(dest), f"live{i}.bin", queue_id=queue.id, options={"connections": 1}
        ).id
        for i, url in enumerate(urls)
    ]
    manager = DownloadManager(db, max_concurrent=6)
    try:
        wait_for(lambda: _status(db, ids[0]) is JobStatus.DOWNLOADING, timeout=30)
        time.sleep(0.5)
        assert sum(_status(db, i) is JobStatus.DOWNLOADING for i in ids) == 1

        manager.update_queue(replace(queue, max_concurrent=3))
        wait_for(
            lambda: sum(_status(db, i) is JobStatus.DOWNLOADING for i in ids) == 3,
            timeout=30,
        )
        wait_for(lambda: all(_status(db, i) is JobStatus.COMPLETED for i in ids), timeout=120)
    finally:
        manager.shutdown()


# ============================================================== E. pause


def test_a_paused_queue_starts_nothing_and_resuming_continues(
    server: MediaServer, db: Database, dest: Path
):
    urls = [server.add(f"/pause{i}.bin", payload(300_000, 50 + i), **_SLOW) for i in range(2)]
    queue = db.create_queue("Paused")
    queue = _edit(db, queue, max_concurrent=1, paused=True)
    ids = [
        db.create_job(
            url, str(dest), f"pause{i}.bin", queue_id=queue.id, options={"connections": 1}
        ).id
        for i, url in enumerate(urls)
    ]
    manager = DownloadManager(db, max_concurrent=4)
    try:
        time.sleep(1.0)  # every chance to wrongly start something
        assert all(_status(db, i) is JobStatus.QUEUED for i in ids)

        manager.update_queue(replace(queue, paused=False))
        with _ConcurrencyWatcher(db, ids) as watcher:
            wait_for(
                lambda: all(_status(db, i) is JobStatus.COMPLETED for i in ids),
                timeout=120,
            )
        # Resumed, and still sequential on the way through.
        assert watcher.peak == 1
        assert watcher.start_order == ids
    finally:
        manager.shutdown()


# ============================================================== F. order


def test_jobs_start_in_queue_order_and_move_up_changes_it(
    server: MediaServer, db: Database, dest: Path
):
    """A sequential queue starts its jobs top-down; moving one up changes
    which one starts next."""
    urls = [server.add(f"/order{i}.bin", payload(400_000, 60 + i), **_SLOW) for i in range(3)]
    queue = db.create_queue("Ordered")
    _edit(db, queue, max_concurrent=1, paused=True)  # set the order before anything runs
    ids = [
        db.create_job(
            url, str(dest), f"order{i}.bin", queue_id=queue.id, options={"connections": 1}
        ).id
        for i, url in enumerate(urls)
    ]
    manager = DownloadManager(db, max_concurrent=4)
    try:
        # Promote the last one to the top: expected start order becomes 2,0,1.
        manager.move_to_top(ids[2])
        expected = [ids[2], ids[0], ids[1]]

        fresh = db.get_queue(queue.id)
        assert fresh is not None
        with _ConcurrencyWatcher(db, ids) as watcher:
            manager.update_queue(replace(fresh, paused=False))
            wait_for(
                lambda: all(_status(db, i) is JobStatus.COMPLETED for i in ids),
                timeout=150,
            )
        assert watcher.peak == 1
        assert watcher.start_order == expected
    finally:
        manager.shutdown()


# ====================================================== two queues, and defaults


def test_two_queues_obey_their_own_limits_independently(
    server: MediaServer, db: Database, dest: Path
):
    slow_urls = [server.add(f"/qa{i}.bin", payload(700_000, 70 + i), **_SLOW) for i in range(2)]
    fast_urls = [server.add(f"/qb{i}.bin", payload(700_000, 80 + i), **_SLOW) for i in range(2)]
    sequential = db.create_queue("Sequential")
    _edit(db, sequential, max_concurrent=1)
    parallel = db.create_queue("Parallel")
    _edit(db, parallel, max_concurrent=2)

    sequential_ids = [
        db.create_job(
            url, str(dest), f"qa{i}.bin", queue_id=sequential.id, options={"connections": 1}
        ).id
        for i, url in enumerate(slow_urls)
    ]
    parallel_ids = [
        db.create_job(
            url, str(dest), f"qb{i}.bin", queue_id=parallel.id, options={"connections": 1}
        ).id
        for i, url in enumerate(fast_urls)
    ]
    manager = DownloadManager(db, max_concurrent=8)
    try:
        everything = sequential_ids + parallel_ids
        with (
            _ConcurrencyWatcher(db, sequential_ids) as seq_watch,
            _ConcurrencyWatcher(db, parallel_ids) as par_watch,
        ):
            wait_for(
                lambda: all(_status(db, i) is JobStatus.COMPLETED for i in everything),
                timeout=180,
            )
        assert seq_watch.peak == 1
        assert par_watch.peak == 2
    finally:
        manager.shutdown()


def test_downloads_without_a_queue_are_untouched(server: MediaServer, db: Database, dest: Path):
    """The default (no named queue) path must keep behaving as it always has:
    limited only by the global setting, and running in parallel up to it."""
    urls = [server.add(f"/free{i}.bin", payload(600_000, 90 + i), **_SLOW) for i in range(3)]
    ids = [
        db.create_job(url, str(dest), f"free{i}.bin", options={"connections": 1}).id
        for i, url in enumerate(urls)
    ]
    manager = DownloadManager(db, max_concurrent=3)
    try:
        with _ConcurrencyWatcher(db, ids) as watcher:
            wait_for(
                lambda: sum(_status(db, i) is JobStatus.DOWNLOADING for i in ids) == 3,
                timeout=30,
            )
            wait_for(
                lambda: all(_status(db, i) is JobStatus.COMPLETED for i in ids),
                timeout=120,
            )
        assert watcher.peak == 3  # all three together, as before
    finally:
        manager.shutdown()


def test_auto_queue_is_the_default_for_every_add(db: Database, dest: Path):
    """Nothing chosen means AUTO_QUEUE, which keeps the pre-existing rules -
    so an entry path that never learned about the picker is not changed."""
    manager = DownloadManager(db, max_concurrent=1)
    try:
        manager.settings.auto_start_downloads = False
        default_queue = manager.create_queue("Configured default")
        manager.settings.default_queue_id = default_queue.id
        job = manager.add_url("http://x.test/a.bin", dest_dir=str(dest), filename="a.bin")
        assert db.get_job(job.id).queue_id == default_queue.id  # type: ignore[union-attr]
        assert manager._resolve_queue("a.bin", AUTO_QUEUE) == default_queue.id
    finally:
        manager.shutdown()


# ============================ the UI wiring, end to end through the real window


def _window(db: Database, tmp_path: Path):
    """A real MainWindow with an idle scheduler, so an add can be inspected in
    the database without a download ever starting."""
    from PySide6.QtWidgets import QApplication

    from app.core.settings import Settings
    from app.ui.main_window import MainWindow

    if not isinstance(QApplication.instance(), QApplication):
        QApplication([])
    settings = Settings(db)
    settings.download_dir = tmp_path
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    window = MainWindow(manager, settings)
    # _browser_add defers to a background warm-up until yt-dlp's extractor list
    # is built, which would make these adds asynchronous. Declare it warm and
    # claiming nothing, so a plain file URL takes the direct path right here.
    window.resolver.smart.warm_up()
    window.resolver.smart.matches = lambda url: False  # type: ignore[method-assign]
    return window, manager, settings


def test_the_browser_add_dialogs_queue_choice_reaches_the_job(
    db: Database, tmp_path: Path, monkeypatch
):
    """browser handoff -> Add Download dialog -> job row. The full path the
    report is about, with the real window and the real manager."""
    from app.ui import add_download_dialog

    window, manager, settings = _window(db, tmp_path)
    try:
        queue = manager.create_queue("Sequential Test")
        settings.confirm_downloads = True
        chosen: dict[str, object] = {}

        real_dialog = add_download_dialog.AddDownloadDialog

        class _AutoAccept(real_dialog):  # type: ignore[misc, valid-type]
            def exec(self) -> int:
                # Stand in for the user: pick the named queue and tick the
                # per-download certificate override, then press Start.
                index = self._queue.findData(queue.id)
                assert index >= 0, "the queue was not offered in the picker"
                self._queue.setCurrentIndex(index)
                self._insecure.setChecked(True)
                self._start()
                chosen["queue"] = self.chosen_queue()
                chosen["insecure"] = self.ignore_certificate_errors()
                return int(self.DialogCode.Accepted)

        monkeypatch.setattr(add_download_dialog, "AddDownloadDialog", _AutoAccept)
        window._browser_add("http://x.test/big.iso", None, (), None)

        assert chosen == {"queue": queue.id, "insecure": True}
        jobs = db.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].queue_id == queue.id
        assert manager.insecure_for(jobs[0]) is True
        # ...and the global setting was not touched by the per-download tick.
        assert settings.insecure_ssl is False
    finally:
        window.close()
        manager.shutdown()


def test_confirmation_turned_off_keeps_the_old_default_behaviour(
    db: Database, tmp_path: Path
) -> None:
    """Settings -> "start downloads immediately": no dialog appears, and the
    add falls back to the configured default-queue preference rather than
    growing a new prompt."""
    window, manager, settings = _window(db, tmp_path)
    try:
        preferred = manager.create_queue("Configured default")
        settings.default_queue_id = preferred.id
        settings.confirm_downloads = False

        window._browser_add("http://x.test/file.bin", None, (), None)

        jobs = db.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].queue_id == preferred.id
    finally:
        window.close()
        manager.shutdown()


# ================================================ reordering stays in its queue


def _order_in(manager: DownloadManager, queue_id: int | None) -> list[int]:
    return [job.id for job in manager._pending_order(queue_id)]


def test_move_up_and_down_stay_inside_the_jobs_own_queue(db: Database, dest: Path):
    """Priorities are global, so "move to top" used to hoist a job above jobs
    in completely unrelated queues - and the scheduler picks each queue's next
    job from within the queue, so it did not even do what it looked like."""
    manager = DownloadManager(db, max_concurrent=0)  # scheduler idle
    try:
        first = manager.create_queue("First")
        second = manager.create_queue("Second")
        a = [
            db.create_job(f"http://x.test/a{i}.bin", str(dest), f"a{i}.bin", queue_id=first.id).id
            for i in range(3)
        ]
        b = [
            db.create_job(f"http://x.test/b{i}.bin", str(dest), f"b{i}.bin", queue_id=second.id).id
            for i in range(3)
        ]

        manager.move_to_top(a[2])
        assert _order_in(manager, first.id) == [a[2], a[0], a[1]]
        assert _order_in(manager, second.id) == b  # untouched

        manager.move_up(b[2])
        assert _order_in(manager, second.id) == [b[0], b[2], b[1]]
        assert _order_in(manager, first.id) == [a[2], a[0], a[1]]  # still untouched

        manager.move_to_bottom(a[2])
        assert _order_in(manager, first.id) == [a[0], a[1], a[2]]
    finally:
        manager.shutdown()


def test_reordering_one_queue_leaves_the_other_queues_slots_alone(db: Database, dest: Path):
    """The global list interleaves queues; a move inside one must permute only
    the slots that queue already occupied."""
    manager = DownloadManager(db, max_concurrent=0)
    try:
        first = manager.create_queue("First")
        second = manager.create_queue("Second")
        # Interleaved on purpose: a, b, a, b, a, b
        ids: list[int] = []
        for i in range(3):
            ids.append(
                db.create_job(
                    f"http://x.test/a{i}.bin", str(dest), f"a{i}.bin", queue_id=first.id
                ).id
            )
            ids.append(
                db.create_job(
                    f"http://x.test/b{i}.bin", str(dest), f"b{i}.bin", queue_id=second.id
                ).id
            )
        before = [job.queue_id for job in manager._pending_order()]

        manager.move_to_top(ids[4])  # the third job of the first queue

        after = [job.queue_id for job in manager._pending_order()]
        assert after == before, "a move inside one queue shifted the queues against each other"
    finally:
        manager.shutdown()


def test_default_queue_reordering_is_unchanged(db: Database, dest: Path):
    """The common case - no named queues at all - behaves exactly as before."""
    manager = DownloadManager(db, max_concurrent=0)
    try:
        ids = [db.create_job(f"http://x.test/{i}.bin", str(dest), f"{i}.bin").id for i in range(4)]
        manager.move_to_top(ids[3])
        assert _order_in(manager, None) == [ids[3], ids[0], ids[1], ids[2]]
        manager.move_down(ids[3])
        assert _order_in(manager, None) == [ids[0], ids[3], ids[1], ids[2]]
        manager.move_to_bottom(ids[0])
        assert _order_in(manager, None) == [ids[3], ids[1], ids[2], ids[0]]
    finally:
        manager.shutdown()


def test_a_move_at_the_edge_of_its_queue_does_nothing(db: Database, dest: Path):
    manager = DownloadManager(db, max_concurrent=0)
    try:
        queue = manager.create_queue("Edge")
        ids = [
            db.create_job(f"http://x.test/{i}.bin", str(dest), f"{i}.bin", queue_id=queue.id).id
            for i in range(2)
        ]
        manager.move_up(ids[0])  # already first
        assert _order_in(manager, queue.id) == ids
        manager.move_down(ids[1])  # already last
        assert _order_in(manager, queue.id) == ids
    finally:
        manager.shutdown()


def test_a_sequential_queue_starts_jobs_in_the_order_its_own_moves_produced(
    server: MediaServer, db: Database, dest: Path
):
    """End to end: the order the per-queue move produced is the order the
    scheduler actually starts them in, while a second queue runs alongside."""
    urls = [server.add(f"/mv{i}.bin", payload(400_000, 100 + i), **_SLOW) for i in range(3)]
    queue = db.create_queue("Moved")
    _edit(db, queue, max_concurrent=1, paused=True)
    other = db.create_queue("Other")
    _edit(db, other, max_concurrent=1)

    ids = [
        db.create_job(
            url, str(dest), f"mv{i}.bin", queue_id=queue.id, options={"connections": 1}
        ).id
        for i, url in enumerate(urls)
    ]
    db.create_job(
        server.add("/other.bin", payload(200_000, 200), **_SLOW),
        str(dest),
        "other.bin",
        queue_id=other.id,
        options={"connections": 1},
    )

    manager = DownloadManager(db, max_concurrent=6)
    try:
        manager.move_to_top(ids[2])
        manager.move_up(ids[0])  # now second -> first, giving 0, 2, 1
        expected = [ids[0], ids[2], ids[1]]
        assert _order_in(manager, queue.id) == expected

        fresh = db.get_queue(queue.id)
        assert fresh is not None
        with _ConcurrencyWatcher(db, ids) as watcher:
            manager.update_queue(replace(fresh, paused=False))
            wait_for(
                lambda: all(_status(db, i) is JobStatus.COMPLETED for i in ids),
                timeout=150,
            )
        assert watcher.peak == 1
        assert watcher.start_order == expected
    finally:
        manager.shutdown()


# ============================================ the live view the manager shows


def test_queue_stats_count_every_state_and_include_the_default_queue(db: Database, dest: Path):
    """The Queue Manager page reads this. It has to cover the default queue
    too, or downloads in no named queue are invisible on the page that is
    supposed to explain where downloads are."""
    manager = DownloadManager(db, max_concurrent=0)  # scheduler idle
    try:
        queue = manager.create_queue("Watched")
        empty = manager.create_queue("Empty")
        jobs = [
            db.create_job(f"http://x.test/{i}.bin", str(dest), f"{i}.bin", queue_id=queue.id)
            for i in range(4)
        ]
        db.set_job_status(jobs[0].id, JobStatus.DOWNLOADING)
        db.set_job_status(jobs[1].id, JobStatus.PAUSED)
        db.set_job_status(jobs[2].id, JobStatus.COMPLETED)
        db.set_job_status(jobs[3].id, JobStatus.FAILED, "nope")
        db.create_job("http://x.test/loose.bin", str(dest), "loose.bin")  # no queue

        stats = manager.queue_stats()
        watched = stats[queue.id]
        assert watched.downloading == 1
        assert watched.paused == 1
        assert watched.completed == 1
        assert watched.failed == 1
        assert watched.queued == 0
        assert watched.total == 4

        assert stats[None].queued == 1  # the default queue is present...
        assert stats[empty.id].total == 0  # ...and so is a queue with nothing in it
    finally:
        manager.shutdown()


def test_queue_stats_progress_covers_only_unfinished_work(db: Database, dest: Path):
    manager = DownloadManager(db, max_concurrent=0)
    try:
        queue = manager.create_queue("Progress")
        running = db.create_job("http://x.test/a.bin", str(dest), "a.bin", queue_id=queue.id)
        db.set_job_status(running.id, JobStatus.DOWNLOADING)
        db.update_job_total(running.id, 1000)
        db.update_job_downloaded(running.id, 250)

        stats = manager.queue_stats()[queue.id]
        assert stats.total_bytes == 1000
        assert stats.downloaded_bytes == 250
        assert stats.percent == 25
        assert stats.active == ("a.bin",)
    finally:
        manager.shutdown()


def test_queue_stats_percent_is_zero_rather_than_misleading_when_sizes_are_unknown(
    db: Database, dest: Path
):
    manager = DownloadManager(db, max_concurrent=0)
    try:
        queue = manager.create_queue("Unknown")
        job = db.create_job("http://x.test/a.bin", str(dest), "a.bin", queue_id=queue.id)
        db.set_job_status(job.id, JobStatus.DOWNLOADING)
        assert manager.queue_stats()[queue.id].percent == 0
    finally:
        manager.shutdown()


def test_pausing_a_queue_from_the_card_stops_new_jobs_and_resuming_releases_them(
    server: MediaServer, db: Database, dest: Path
):
    """The one-click toggle is a real scheduler control, not a UI flag."""
    urls = [server.add(f"/tog{i}.bin", payload(300_000, 110 + i), **_SLOW) for i in range(2)]
    queue = db.create_queue("Toggle")
    _edit(db, queue, max_concurrent=1, paused=True)
    ids = [
        db.create_job(
            url, str(dest), f"tog{i}.bin", queue_id=queue.id, options={"connections": 1}
        ).id
        for i, url in enumerate(urls)
    ]
    manager = DownloadManager(db, max_concurrent=4)
    try:
        time.sleep(0.8)
        assert all(_status(db, i) is JobStatus.QUEUED for i in ids)

        manager.set_queue_paused(queue.id, False)
        wait_for(lambda: _status(db, ids[0]) is JobStatus.DOWNLOADING, timeout=30)

        manager.set_queue_paused(queue.id, True)  # pause again mid-flight
        wait_for(lambda: _status(db, ids[0]) is JobStatus.COMPLETED, timeout=60)
        time.sleep(0.8)
        # The running one was allowed to finish; the next one does not start.
        assert _status(db, ids[1]) is JobStatus.QUEUED

        manager.set_queue_paused(queue.id, False)
        wait_for(lambda: _status(db, ids[1]) is JobStatus.COMPLETED, timeout=60)
    finally:
        manager.shutdown()


def test_moving_a_queue_changes_which_queue_gets_the_next_free_slot(
    server: MediaServer, db: Database, dest: Path
):
    """Queue position is a scheduling control: with one global slot free, the
    queue nearer the top runs first. Moving it must change that."""
    slow = server.add("/first.bin", payload(400_000, 120), **_SLOW)
    other = server.add("/second.bin", payload(400_000, 121), **_SLOW)
    first = db.create_queue("First")
    second = db.create_queue("Second")
    a = db.create_job(slow, str(dest), "first.bin", queue_id=first.id, options={"connections": 1})
    b = db.create_job(
        other, str(dest), "second.bin", queue_id=second.id, options={"connections": 1}
    )

    # Reordered before any scheduler exists, so the very first pass sees both
    # queues eligible and has to choose between them on position alone.
    # (Unpausing them one after the other instead would just hand the slot to
    # whichever was released first, and prove nothing.)
    idle = DownloadManager(db, max_concurrent=0)
    try:
        assert [q.name for q in idle.list_queues()] == ["First", "Second"]
        idle.move_queue(second.id, -1)
        assert [q.name for q in idle.list_queues()] == ["Second", "First"]
    finally:
        idle.shutdown()

    manager = DownloadManager(db, max_concurrent=1)  # exactly one slot
    try:
        with _ConcurrencyWatcher(db, [a.id, b.id]) as watcher:
            wait_for(
                lambda: all(_status(db, i) is JobStatus.COMPLETED for i in (a.id, b.id)),
                timeout=120,
            )
        assert watcher.peak == 1
        assert watcher.start_order == [b.id, a.id]  # the promoted queue went first
    finally:
        manager.shutdown()


def test_moving_a_queue_at_the_edge_does_nothing(db: Database):
    manager = DownloadManager(db, max_concurrent=0)
    try:
        first = manager.create_queue("A")
        manager.create_queue("B")
        manager.move_queue(first.id, -1)
        assert [q.name for q in manager.list_queues()] == ["A", "B"]
        manager.move_queue(first.id, 5)
        assert [q.name for q in manager.list_queues()] == ["A", "B"]
    finally:
        manager.shutdown()


def test_the_queue_page_shows_a_card_per_queue_plus_the_default_one(db: Database, dest: Path):
    from PySide6.QtWidgets import QApplication

    from app.ui.queue_view import QueueView

    if not isinstance(QApplication.instance(), QApplication):
        QApplication([])
    manager = DownloadManager(db, max_concurrent=0)
    try:
        first = manager.create_queue("Sequential Test")
        second = manager.create_queue("Big files")
        db.create_job("http://x.test/a.bin", str(dest), "a.bin", queue_id=first.id)
        db.create_job("http://x.test/loose.bin", str(dest), "loose.bin")

        view = QueueView(manager)
        view.show()
        # A live slot for each named queue AND for the default queue.
        assert set(view._live) == {first.id, second.id, None}
        assert view._timer.isActive()  # live while visible
        view._tick()  # must not raise with real stats

        view.hide()
        assert not view._timer.isActive()  # and idle behind another page
        view.deleteLater()
    finally:
        manager.shutdown()


def test_a_long_job_name_never_widens_the_queue_page(db: Database, dest: Path):
    """The reported bug: one long release name in the 'what is running' line
    made a card wider than the window, so the whole page grew a horizontal
    scrollbar and the text ran under the progress bar. Every line on a card
    elides now, so the page is as wide as the window gives it and no wider."""
    from PySide6.QtWidgets import QApplication

    from app.core.manager import QueueStats
    from app.ui.queue_view import QueueView, _active_text, _live_text

    if not isinstance(QApplication.instance(), QApplication):
        QApplication([])
    manager = DownloadManager(db, max_concurrent=0)
    try:
        queue = manager.create_queue("movies")
        view = QueueView(manager)
        view.resize(420, 600)
        view.show()

        enormous = (
            "The Lord Of The Rings The Fellowship Of The Ring (2001) "
            "[EXTENDED] [2160p] [4K] [BluRay] [5.1] [YTS.MX]"
        )
        stats = QueueStats(
            queue_id=queue.id,
            downloading=1,
            queued=2,
            completed=5,
            active=(enormous,),
            total_bytes=10_800_000_000,
            downloaded_bytes=132_200_000,
        )
        # The counts and the names are separate lines: the counts survive
        # whatever the running job happens to be called.
        assert _live_text(stats) == "1 downloading  ·  2 waiting  ·  5 done"
        assert _active_text(stats) == enormous

        widgets = view._live[queue.id]
        view._apply_stats(widgets, stats)
        QApplication.processEvents()
        assert widgets.active.full_text() == enormous
        assert widgets.active.text() != enormous  # painted elided at 420px
        assert widgets.active.toolTip() == enormous
        assert not widgets.bar.isTextVisible()  # a 6px bar has no room for text
        assert widgets.readout.isVisible()  # the figures get their own line

        # Nothing on the page demands more width than the window offers.
        assert view._body_holder.minimumSizeHint().width() <= view.width()

        view.resize(1200, 600)
        QApplication.processEvents()
        assert widgets.active.text() == enormous  # room again: shown in full
        view.hide()
        view.deleteLater()
    finally:
        manager.shutdown()


def test_the_torrent_dialog_offers_and_returns_a_queue(db: Database, tmp_path: Path):
    """The reported gap: no way to choose a queue before a torrent starts."""
    from PySide6.QtWidgets import QApplication

    from app.ui.add_download_dialog import queue_choices
    from app.ui.torrent_dialog import AddTorrentDialog

    if not isinstance(QApplication.instance(), QApplication):
        QApplication([])
    manager = DownloadManager(db, max_concurrent=0)
    try:
        queue = manager.create_queue("Torrents")
        dialog = AddTorrentDialog("ubuntu.iso", None, tmp_path, queues=queue_choices(manager))
        assert dialog.chosen_queue() is None  # Default until chosen
        dialog._queue.setCurrentIndex(dialog._queue.findData(queue.id))
        assert dialog.chosen_queue() == queue.id

        # A queue chosen earlier in the flow opens preselected, not reset.
        preselected = AddTorrentDialog(
            "ubuntu.iso", None, tmp_path, queues=queue_choices(manager), selected_queue=queue.id
        )
        assert preselected.chosen_queue() == queue.id
        dialog.deleteLater()
        preselected.deleteLater()
    finally:
        manager.shutdown()
