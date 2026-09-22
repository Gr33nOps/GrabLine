"""Bulk torrent import and the torrent watch folder.

Real .torrent files throughout (built with the engine's own creator), so the
parsing, the info-hashes and the duplicate keys are the ones the app actually
computes - not fixtures that happen to agree with the code.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from app.core import torrentbatch, torrentwatch
from app.core.manager import DownloadManager
from app.core.models import JobKind
from app.core.settings import Settings
from app.core.torrentbatch import TorrentCandidate
from app.db.database import Database
from app.engines.torrent import (
    create_torrent_file,
    hash_forms,
    info_hash_from_magnet,
    info_hashes_from_magnet,
    magnet_from_torrent,
    parse_torrent,
)

pytest.importorskip("libtorrent")


def _make_torrent(tmp_path: Path, name: str, *, size: int = 40_000) -> Path:
    """A real .torrent for a real payload, written to ``tmp_path/torrents``."""
    payload_dir = tmp_path / "payloads" / name
    payload_dir.mkdir(parents=True, exist_ok=True)
    (payload_dir / "data.bin").write_bytes(os.urandom(size))
    torrents = tmp_path / "torrents"
    torrents.mkdir(exist_ok=True)
    target = torrents / f"{name}.torrent"
    target.write_bytes(create_torrent_file(payload_dir))
    return target


# ------------------------------------------------------------- info hashes


def test_parse_torrent_carries_an_info_hash(tmp_path: Path):
    meta = parse_torrent(_make_torrent(tmp_path, "alpha").read_bytes())
    assert len(meta.info_hash) in (40, 64)
    assert meta.info_hash == meta.info_hash.lower()


def test_magnet_info_hash_forms():
    hex_hash = "abcdef0123456789abcdef0123456789abcdef01"
    assert info_hash_from_magnet(f"magnet:?xt=urn:btih:{hex_hash.upper()}&dn=x") == hex_hash
    # The same hash written base32 must produce the same key, or the two
    # notations of one torrent would both queue.
    import base64
    import binascii

    b32 = base64.b32encode(binascii.unhexlify(hex_hash)).decode()
    assert info_hash_from_magnet(f"magnet:?xt=urn:btih:{b32}") == hex_hash
    assert info_hash_from_magnet("magnet:?dn=no-hash") == ""
    assert info_hash_from_magnet("https://example.com/x.torrent") == ""


def test_magnet_from_torrent_round_trips_to_the_same_hash(tmp_path: Path):
    """A hybrid torrent has a v1 hash and a v2 one, and libtorrent reports the
    truncated v2 on a live handle. Whichever the two sides hold, the sets must
    meet - that overlap is what "already queued" is decided on."""
    data = _make_torrent(tmp_path, "beta").read_bytes()
    meta = parse_torrent(data)
    from_magnet = info_hashes_from_magnet(magnet_from_torrent(data))
    assert set(meta.info_hashes) & set(from_magnet)
    assert info_hash_from_magnet(magnet_from_torrent(data)) == meta.info_hash


def test_hash_forms_normalises():
    v2 = "a" * 64
    assert hash_forms(v2) == (v2, "a" * 40)
    assert hash_forms("  ABCDEF  ") == ("abcdef",)
    assert hash_forms("0" * 40, "") == ()  # the unknown-hash placeholder


# --------------------------------------------------------------- collecting


def test_magnet_links_pulls_links_out_of_pasted_text():
    text = "\n".join(
        [
            "magnet:?xt=urn:btih:aaa",
            "",
            "not a link at all",
            "  magnet:?xt=urn:btih:bbb  ",
            "magnet:?xt=urn:btih:aaa",  # repeated
        ]
    )
    assert torrentbatch.magnet_links(text) == [
        "magnet:?xt=urn:btih:aaa",
        "magnet:?xt=urn:btih:bbb",
    ]


def test_torrent_files_in_folder_is_sorted_and_recursive(tmp_path: Path):
    _make_torrent(tmp_path, "b")
    _make_torrent(tmp_path, "a")
    nested = tmp_path / "torrents" / "more"
    nested.mkdir()
    (nested / "c.torrent").write_bytes(_make_torrent(tmp_path, "c").read_bytes())
    (tmp_path / "torrents" / "notes.txt").write_text("ignore me")

    found = torrentbatch.torrent_files_in(tmp_path / "torrents")
    assert [p.name for p in found] == ["a.torrent", "b.torrent", "c.torrent", "c.torrent"]
    shallow = torrentbatch.torrent_files_in(tmp_path / "torrents", recursive=False)
    assert [p.name for p in shallow] == ["a.torrent", "b.torrent", "c.torrent"]


def test_expand_sources_mixes_folders_magnets_and_junk(tmp_path: Path):
    _make_torrent(tmp_path, "one")
    _make_torrent(tmp_path, "two")
    sources, ignored = torrentbatch.expand_sources(
        [
            str(tmp_path / "torrents"),
            "magnet:?xt=urn:btih:abc",
            "https://example.com/linux.torrent",
            "https://example.com/not-a-torrent.mp4",
            str(tmp_path / "payloads" / "one" / "data.bin"),
            "",
        ]
    )
    assert sum(1 for s in sources if s.endswith(".torrent") and s.startswith(str(tmp_path))) == 2
    assert "magnet:?xt=urn:btih:abc" in sources
    assert "https://example.com/linux.torrent" in sources
    assert ignored == [
        "https://example.com/not-a-torrent.mp4",
        str(tmp_path / "payloads" / "one" / "data.bin"),
    ]


# ------------------------------------------------------------------ reading


def test_load_candidates_reads_many_torrent_files(tmp_path: Path):
    paths = [_make_torrent(tmp_path, f"file{i}") for i in range(5)]
    seen: list[tuple[int, int]] = []
    candidates = torrentbatch.load_candidates(
        [str(p) for p in paths], on_progress=lambda done, total: seen.append((done, total))
    )
    assert len(candidates) == 5
    assert all(c.ok and c.info_hash and c.total_size > 0 for c in candidates)
    assert {c.name for c in candidates} == {f"file{i}" for i in range(5)}
    assert seen[-1] == (5, 5)


def test_load_candidates_mixes_magnets_and_files(tmp_path: Path):
    torrent = _make_torrent(tmp_path, "gamma")
    magnet = magnet_from_torrent(torrent.read_bytes())
    candidates = torrentbatch.load_candidates([str(torrent), magnet])
    assert [c.ok for c in candidates] == [True, True]
    # Both describe the same torrent, so their key sets meet.
    assert candidates[0].keys & candidates[1].keys


def test_one_broken_file_does_not_end_the_batch(tmp_path: Path):
    good = _make_torrent(tmp_path, "good")
    broken = tmp_path / "torrents" / "broken.torrent"
    broken.write_bytes(b"this is not bencoded at all")
    missing = str(tmp_path / "torrents" / "gone.torrent")

    candidates = torrentbatch.load_candidates([str(good), str(broken), missing])
    assert candidates[0].ok
    assert not candidates[1].ok and "not a valid torrent" in candidates[1].error
    assert not candidates[2].ok and "not found" in candidates[2].error


# --------------------------------------------------------------- duplicates


def test_duplicates_inside_the_batch_keep_the_first(tmp_path: Path):
    torrent = _make_torrent(tmp_path, "delta")
    magnet = magnet_from_torrent(torrent.read_bytes())
    other = _make_torrent(tmp_path, "epsilon")
    candidates = torrentbatch.mark_duplicates(
        torrentbatch.load_candidates([str(torrent), magnet, str(other), str(torrent)])
    )
    assert [c.ok for c in candidates] == [True, False, True, False]
    assert candidates[1].duplicate == torrentbatch.DUPLICATE_IN_BATCH
    assert candidates[3].duplicate == torrentbatch.DUPLICATE_IN_BATCH


def test_duplicates_against_the_existing_queue(db: Database, tmp_path: Path):
    manager = DownloadManager(db, settings=Settings(db), max_concurrent=0)
    try:
        torrent = _make_torrent(tmp_path, "zeta")
        info_hash = parse_torrent(torrent.read_bytes()).info_hash
        manager.add_torrent(str(torrent), dest_dir=tmp_path, name="zeta")
        db.update_job_options(db.list_jobs()[0].id, {"info_hash": info_hash})

        keys = torrentbatch.queued_keys([j for j in db.list_jobs() if j.kind is JobKind.TORRENT])
        assert info_hash in keys
        # Added again as a magnet: a different source string, the same torrent.
        magnet = magnet_from_torrent(torrent.read_bytes())
        marked = torrentbatch.mark_duplicates(torrentbatch.load_candidates([magnet]), keys)
        assert marked[0].duplicate == torrentbatch.DUPLICATE_QUEUED
    finally:
        manager.shutdown()


def test_a_magnet_with_no_hash_falls_back_to_its_source():
    candidates = torrentbatch.load_candidates(
        ["magnet:?dn=mystery", "magnet:?dn=mystery", "magnet:?dn=other"]
    )
    marked = torrentbatch.mark_duplicates(candidates)
    assert [c.ok for c in marked] == [True, False, True]


# ------------------------------------------------------------- destinations


def test_validate_destination_rejects_the_unusable(tmp_path: Path):
    with pytest.raises(torrentbatch.DestinationError, match="choose a folder"):
        torrentbatch.validate_destination("   ")
    with pytest.raises(torrentbatch.DestinationError, match="full path"):
        torrentbatch.validate_destination("relative/folder")
    a_file = tmp_path / "not-a-folder"
    a_file.write_text("x")
    with pytest.raises(torrentbatch.DestinationError, match="file, not a folder"):
        torrentbatch.validate_destination(str(a_file))
    assert torrentbatch.validate_destination(str(tmp_path)) == tmp_path.resolve()


def test_ensure_destination_creates_missing_folders(tmp_path: Path):
    target = tmp_path / "deep" / "nested" / "downloads"
    assert torrentbatch.ensure_destination(target) == target
    assert target.is_dir()


def test_subfolder_cannot_escape_the_destination(tmp_path: Path):
    base = tmp_path / "downloads"
    base.mkdir()
    for hostile in (
        "../../etc",
        "../outside",
        "/etc/passwd",
        "C:\\Windows\\System32",
        "..",
        "",
    ):
        placed = torrentbatch.subfolder_for(base, hostile)
        assert placed.is_relative_to(base.resolve()), hostile
        assert placed != base.resolve(), hostile


def test_one_destination_is_applied_to_every_torrent(tmp_path: Path):
    base = tmp_path / "shared"
    base.mkdir()
    candidates = [
        TorrentCandidate(source=f"magnet:?xt=urn:btih:{i:040x}", name=f"t{i}") for i in range(50)
    ]
    placed = torrentbatch.assign_destinations(candidates, base, subfolders=False)
    assert len(placed) == 50
    assert {c.dest_dir for c in placed} == {str(base.resolve())}


def test_subfolders_separate_different_torrents_that_share_a_name(tmp_path: Path):
    base = tmp_path / "shared"
    base.mkdir()
    same_name = [
        TorrentCandidate(source="magnet:?xt=urn:btih:" + "a" * 40, name="Season 1"),
        TorrentCandidate(source="magnet:?xt=urn:btih:" + "b" * 40, name="Season 1"),
    ]
    placed = torrentbatch.assign_destinations(same_name, base, subfolders=True)
    assert placed[0].dest_dir != placed[1].dest_dir
    assert all(Path(c.dest_dir).is_relative_to(base.resolve()) for c in placed)

    # The very same torrent listed twice keeps one folder, so a re-add resumes
    # in place instead of starting a second copy beside it.
    twice = [same_name[0], same_name[0]]
    again = torrentbatch.assign_destinations(twice, base, subfolders=True)
    assert again[0].dest_dir == again[1].dest_dir


# ----------------------------------------------------- queueing the batch


def test_batch_queues_every_torrent_with_the_shared_destination(db: Database, tmp_path: Path):
    """The end the user cares about: 25 torrents, one folder, all queued and
    none of them started past the concurrency limit."""
    manager = DownloadManager(db, settings=Settings(db), max_concurrent=0)
    try:
        paths = [_make_torrent(tmp_path, f"bulk{i}", size=2_000) for i in range(25)]
        base = tmp_path / "shared"
        torrentbatch.ensure_destination(base)
        candidates = torrentbatch.assign_destinations(
            torrentbatch.mark_duplicates(torrentbatch.load_candidates([str(p) for p in paths])),
            base,
            subfolders=False,
        )
        for candidate in candidates:
            manager.add_torrent(
                candidate.source,
                dest_dir=candidate.dest_dir,
                name=candidate.name,
                options={"sequential": True},
            )
        jobs = [j for j in db.list_jobs() if j.kind is JobKind.TORRENT]
        assert len(jobs) == 25
        assert {j.dest_dir for j in jobs} == {str(base.resolve())}
        assert all(j.options.get("sequential") for j in jobs)
        # max_concurrent=0 means the scheduler starts nothing: adding a batch
        # queues work, it does not open 25 swarms.
        assert manager.snapshot() and all(j.status.value != "downloading" for j in jobs)
    finally:
        manager.shutdown()


def test_a_single_torrent_add_still_works(db: Database, tmp_path: Path):
    """The batch path must not have changed what one torrent does."""
    manager = DownloadManager(db, settings=Settings(db), max_concurrent=0)
    try:
        torrent = _make_torrent(tmp_path, "solo")
        job = manager.add_torrent(str(torrent), dest_dir=tmp_path, name="solo")
        assert job.kind is JobKind.TORRENT
        assert job.dest_dir == str(tmp_path)
        assert job.filename == "solo"
    finally:
        manager.shutdown()


# ------------------------------------------------------------ watch folder


def test_watch_folder_waits_for_a_file_to_finish_copying(tmp_path: Path):
    watched = tmp_path / "watch"
    watched.mkdir()
    watcher = torrentwatch.TorrentWatcher()
    assert watcher.scan(watched).ready == ()

    partial = watched / "incoming.torrent"
    partial.write_bytes(b"half a file")
    first = watcher.scan(watched)
    assert first.ready == () and first.settling == (partial,)

    # Still growing: the scan that sees a new size must not hand it over.
    partial.write_bytes(b"half a file plus the rest of it")
    assert watcher.scan(watched).ready == ()
    # Unchanged since the last look: now it is safe to read.
    assert watcher.scan(watched).ready == (partial,)


def test_watch_folder_does_not_import_the_same_file_twice(tmp_path: Path):
    watched = tmp_path / "watch"
    watched.mkdir()
    source = _make_torrent(tmp_path, "watched")
    dropped = watched / "watched.torrent"
    dropped.write_bytes(source.read_bytes())

    watcher = torrentwatch.TorrentWatcher()
    watcher.scan(watched)
    ready = watcher.scan(watched).ready
    assert ready == (dropped,)

    info_hash = parse_torrent(dropped.read_bytes()).info_hash
    seen = torrentwatch.remember([], torrentwatch.watch_keys_for(dropped, info_hash))
    assert info_hash in seen
    # Every later scan, including after a restart (a fresh watcher, the seen
    # list loaded from settings), leaves it alone.
    assert watcher.scan(watched, seen).ready == ()
    assert torrentwatch.TorrentWatcher().scan(watched, seen).ready == ()


def test_watch_folder_survives_a_missing_folder(tmp_path: Path):
    watcher = torrentwatch.TorrentWatcher()
    result = watcher.scan(tmp_path / "never-existed")
    assert result.ready == () and "not available" in result.error
    assert watcher.scan(None).ready == ()  # switched off


def test_watch_folder_forgets_files_that_were_moved_away(tmp_path: Path):
    watched = tmp_path / "watch"
    watched.mkdir()
    dropped = watched / "gone.torrent"
    dropped.write_bytes(b"something")
    watcher = torrentwatch.TorrentWatcher()
    watcher.scan(watched)
    dropped.unlink()
    assert watcher.scan(watched).ready == ()
    # Re-copied later, it starts its stability count again rather than being
    # handed over on the first sight of it.
    dropped.write_bytes(b"something else")
    assert watcher.scan(watched).ready == ()
    assert watcher.scan(watched).ready == (dropped,)


def test_remembered_list_is_deduplicated_and_capped():
    keys = torrentwatch.remember(["a", "b"], ["B", "c", "c"])
    assert keys == ["a", "b", "c"]
    overflowing = torrentwatch.remember([], [f"k{i}" for i in range(700)])
    assert len(overflowing) == torrentwatch.MAX_REMEMBERED
    assert overflowing[-1] == "k699"


# -------------------------------------------------------------- settings


def test_batch_and_watch_settings_persist(db: Database, tmp_path: Path):
    settings = Settings(db)
    assert settings.torrent_batch_dir is None
    assert settings.torrent_batch_remember is True
    assert settings.torrent_watch_enabled is False
    assert settings.torrent_watch_interval_seconds == 20

    settings.torrent_batch_dir = tmp_path / "bulk"
    settings.torrent_batch_subfolders = True
    settings.torrent_batch_remember = False
    settings.torrent_watch_enabled = True
    settings.torrent_watch_dir = tmp_path / "watch"
    settings.torrent_watch_dest = tmp_path / "watched-downloads"
    settings.torrent_watch_subfolders = True
    settings.torrent_watch_interval_seconds = 45
    settings.torrent_watch_seen = ["hash-a", "hash-b"]

    fresh = Settings(db)
    assert fresh.torrent_batch_dir == tmp_path / "bulk"
    assert fresh.torrent_batch_subfolders is True
    assert fresh.torrent_batch_remember is False
    assert fresh.torrent_watch_enabled is True
    assert fresh.torrent_watch_dir == tmp_path / "watch"
    assert fresh.torrent_watch_dest == tmp_path / "watched-downloads"
    assert fresh.torrent_watch_subfolders is True
    assert fresh.torrent_watch_interval_seconds == 45
    assert fresh.torrent_watch_seen == ("hash-a", "hash-b")


def test_watch_interval_is_clamped(db: Database):
    settings = Settings(db)
    settings.torrent_watch_interval_seconds = 1
    assert Settings(db).torrent_watch_interval_seconds == 5
    settings.torrent_watch_interval_seconds = 999_999
    assert Settings(db).torrent_watch_interval_seconds == 3600


def test_watch_seen_list_is_capped_in_settings(db: Database):
    settings = Settings(db)
    settings.torrent_watch_seen = [f"key-{i}" for i in range(600)]
    assert len(Settings(db).torrent_watch_seen) == 500


# --------------------------------------------------------------------- ui


def _qapp() -> Any:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    instance = QApplication.instance()
    return instance if isinstance(instance, QApplication) else QApplication([])


def _row(dialog: Any, index: int) -> Any:
    item = dialog.tree.topLevelItem(index)
    assert item is not None
    return item


def _scanned(dialog: Any) -> None:
    """Wait out the dialog's background scan and let its signals land."""
    scan = dialog._scan
    if scan is not None:
        assert scan.wait(60_000), "the torrent scan did not finish"
    _qapp().processEvents()
    _qapp().processEvents()


def test_batch_dialog_lists_a_dropped_folder(tmp_path: Path):
    app = _qapp()
    from app.ui.torrent_batch_dialog import TorrentBatchDialog

    for index in range(12):
        _make_torrent(tmp_path, f"drop{index}", size=2_000)
    dest = tmp_path / "shared"

    dialog = TorrentBatchDialog(default_dir=dest)
    try:
        dialog.add_sources([str(tmp_path / "torrents")])
        _scanned(dialog)
        assert dialog.tree.topLevelItemCount() == 12
        assert dialog._ok_button.text() == "Download All (12)"

        # Removing a row takes it out of the batch and off the button.
        _row(dialog, 0).setSelected(True)
        dialog._remove_selected()
        assert dialog.tree.topLevelItemCount() == 11
        assert dialog._ok_button.text() == "Download All (11)"

        dialog.dir_edit.setText(str(dest))
        candidates = dialog.selected_candidates()
        assert len(candidates) == 11
        assert {c.dest_dir for c in candidates} == {str(dest.resolve())}
    finally:
        dialog.deleteLater()
        app.processEvents()


def test_batch_dialog_marks_duplicates_and_unreadable_files(tmp_path: Path):
    app = _qapp()
    from app.ui.torrent_batch_dialog import TorrentBatchDialog

    good = _make_torrent(tmp_path, "real", size=2_000)
    broken = tmp_path / "torrents" / "junk.torrent"
    broken.write_bytes(b"not bencoded")
    magnet = magnet_from_torrent(good.read_bytes())

    dialog = TorrentBatchDialog(default_dir=tmp_path / "shared")
    try:
        dialog.add_sources([str(good), str(broken), magnet])
        _scanned(dialog)
        assert dialog.tree.topLevelItemCount() == 3
        statuses = [_row(dialog, row).text(3) for row in range(dialog.tree.topLevelItemCount())]
        assert statuses == ["Ready", "Unreadable", "Duplicate"]
        assert dialog._ok_button.text() == "Download All (1)"
        assert dialog.skipped_counts() == (1, 1)
    finally:
        dialog.deleteLater()
        app.processEvents()


def test_batch_dialog_refuses_a_destination_it_cannot_use(tmp_path: Path):
    app = _qapp()
    from app.ui.torrent_batch_dialog import TorrentBatchDialog

    dialog = TorrentBatchDialog(default_dir=tmp_path)
    try:
        dialog.dir_edit.setText("not/an/absolute/path")
        with pytest.raises(torrentbatch.DestinationError):
            dialog.selected_candidates()
    finally:
        dialog.deleteLater()
        app.processEvents()


def test_window_queues_a_batch_and_reports_it(db: Database, tmp_path: Path):
    """The whole path the user takes: pick a folder of torrents, one
    destination, Download All - and the jobs land in the existing queue."""
    app = _qapp()
    from app.ui.main_window import MainWindow
    from app.ui.torrent_batch_dialog import TorrentBatchDialog

    settings = Settings(db)
    settings.download_dir = tmp_path / "downloads"
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    window = MainWindow(manager, settings)
    dest = tmp_path / "shared"
    try:
        for index in range(8):
            _make_torrent(tmp_path, f"batch{index}", size=2_000)
        dialog = TorrentBatchDialog(default_dir=dest, parent=window)
        dialog.add_sources([str(tmp_path / "torrents")])
        _scanned(dialog)
        dialog.dir_edit.setText(str(dest))
        added, failed = window._queue_torrent_batch(
            dialog.selected_candidates(), options={"sequential": True}, queue_id=None
        )
        assert (added, failed) == (8, 0)

        jobs = [j for j in db.list_jobs() if j.kind is JobKind.TORRENT]
        assert len(jobs) == 8
        assert {j.dest_dir for j in jobs} == {str(dest.resolve())}
        assert all(j.options.get("sequential") for j in jobs)
        assert window._batch_summary(8, 2, 0) == (
            "8 torrent(s) added successfully. 2 duplicate(s) skipped."
        )

        # A second import of the very same folder is all duplicates, and so is
        # the same torrent offered as a magnet link.
        keys = window._queued_torrent_keys()
        again = torrentbatch.mark_duplicates(
            torrentbatch.load_candidates(
                [str(p) for p in torrentbatch.torrent_files_in(tmp_path / "torrents")]
            ),
            keys,
        )
        assert not any(c.ok for c in again)
        as_magnet = torrentbatch.mark_duplicates(
            torrentbatch.load_candidates(
                [magnet_from_torrent((tmp_path / "torrents" / "batch0.torrent").read_bytes())]
            ),
            keys,
        )
        assert as_magnet[0].duplicate == torrentbatch.DUPLICATE_QUEUED
    finally:
        window.shutdown()
        manager.shutdown()
        window.deleteLater()
        app.processEvents()


def test_window_imports_from_the_watch_folder(db: Database, tmp_path: Path):
    """Drop a .torrent into the watched folder and it queues itself - once."""
    app = _qapp()
    from app.ui.main_window import MainWindow

    watched = tmp_path / "watch"
    watched.mkdir()
    settings = Settings(db)
    settings.download_dir = tmp_path / "downloads"
    settings.torrent_watch_enabled = True
    settings.torrent_watch_dir = watched
    settings.torrent_watch_dest = tmp_path / "watched-downloads"
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    window = MainWindow(manager, settings)
    try:
        source = _make_torrent(tmp_path, "auto", size=2_000)
        dropped = watched / "auto.torrent"
        dropped.write_bytes(source.read_bytes())

        # Two scans: the first sees it arrive, the second confirms it settled.
        assert window._watcher.scan(watched, settings.torrent_watch_seen).ready == ()
        ready = window._watcher.scan(watched, settings.torrent_watch_seen).ready
        assert ready == (dropped,)

        window._import_watched(ready, list(settings.torrent_watch_seen))
        jobs = [j for j in db.list_jobs() if j.kind is JobKind.TORRENT]
        assert len(jobs) == 1
        assert jobs[0].dest_dir == str((tmp_path / "watched-downloads").resolve())
        assert settings.torrent_watch_seen  # remembered across restarts
        assert dropped.exists()  # never moved, never deleted

        # Every later scan leaves it alone, so it is not queued a second time.
        assert window._watcher.scan(watched, settings.torrent_watch_seen).ready == ()
    finally:
        window.shutdown()
        manager.shutdown()
        window.deleteLater()
        app.processEvents()


def test_watch_folder_poll_runs_end_to_end(db: Database, tmp_path: Path):
    """The timer's own entry point, not just the pieces under it."""
    app = _qapp()
    from app.ui.main_window import MainWindow

    watched = tmp_path / "watch"
    watched.mkdir()
    settings = Settings(db)
    settings.download_dir = tmp_path / "downloads"
    settings.torrent_watch_enabled = True
    settings.torrent_watch_dir = watched
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    window = MainWindow(manager, settings)
    try:
        (watched / "auto.torrent").write_bytes(
            _make_torrent(tmp_path, "polled", size=2_000).read_bytes()
        )

        def poll_once() -> None:
            window._poll_torrent_watch()
            for worker in list(window._file_ops):
                worker.wait(30_000)
            app.processEvents()
            app.processEvents()

        poll_once()  # first sight of the file: still settling
        assert not [j for j in db.list_jobs() if j.kind is JobKind.TORRENT]
        poll_once()  # unchanged since: imported
        assert len([j for j in db.list_jobs() if j.kind is JobKind.TORRENT]) == 1
        poll_once()  # remembered: not imported again
        assert len([j for j in db.list_jobs() if j.kind is JobKind.TORRENT]) == 1

        # Switched off in Settings, the poll is a no-op even with a new file.
        settings.torrent_watch_enabled = False
        (watched / "second.torrent").write_bytes(
            _make_torrent(tmp_path, "ignored", size=2_000).read_bytes()
        )
        poll_once()
        poll_once()
        assert len([j for j in db.list_jobs() if j.kind is JobKind.TORRENT]) == 1
    finally:
        window.shutdown()
        manager.shutdown()
        window.deleteLater()
        app.processEvents()
