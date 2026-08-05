"""Offscreen smoke tests: widgets build, render job rows, and validate input."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

if TYPE_CHECKING:
    from app.ui.detail_drawer import DetailDrawer

from PySide6.QtWidgets import QApplication

from app.core.manager import DownloadManager
from app.core.settings import Settings
from app.db.database import Database
from app.engines.smart import MediaInfo, QualityOption
from app.ui.clipboard import is_probable_url
from app.ui.format import duration_text, human_bytes
from app.ui.main_window import MainWindow
from app.ui.quality_panel import QualityPanel, parse_timestamp


def _qapp() -> QApplication:
    instance = QApplication.instance()
    return instance if isinstance(instance, QApplication) else QApplication([])


def test_human_bytes():
    assert human_bytes(512) == "512 B"
    assert human_bytes(2048) == "2.0 KB"
    assert human_bytes(5 * 1024 * 1024) == "5.0 MB"


def test_duration_text():
    assert duration_text(None) == ""
    assert duration_text(75) == "1:15"
    assert duration_text(3671) == "1:01:11"


def test_parse_timestamp():
    assert parse_timestamp("") is None
    assert parse_timestamp("90") == 90.0
    assert parse_timestamp("1:30") == 90.0
    assert parse_timestamp("1:02:03") == 3723.0
    with pytest.raises(ValueError):
        parse_timestamp("abc")


def test_is_probable_url():
    assert is_probable_url("https://example.com/video.mp4")
    assert not is_probable_url("just some text")
    assert not is_probable_url("https://example.com/with space")
    assert not is_probable_url("ftp://example.com/f")


def test_clipboard_off_by_default_and_suppress(db: Database):
    from app.ui.clipboard import ClipboardWatcher

    app = _qapp()
    settings = Settings(db)
    assert settings.clipboard_watcher is False  # no auto-offer on copy
    watcher = ClipboardWatcher(app, settings)
    fired: list[str] = []
    watcher.url_copied.connect(fired.append)
    # Even enabled, a suppressed URL (our own Copy URL) must not bounce back.
    settings.clipboard_watcher = True
    watcher.suppress("https://example.com/mine.zip")
    app.clipboard().setText("https://example.com/mine.zip")
    assert fired == []


def test_main_window_lists_jobs(db: Database, tmp_path: Path):
    _qapp()
    settings = Settings(db)
    settings.download_dir = tmp_path
    # max_concurrent=0 keeps the scheduler idle: the row renders without any
    # network activity.
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        from app.ui.motion import SmoothProgressBar

        job = db.create_job("http://example.invalid/x.bin", str(tmp_path), "x.bin")
        window = MainWindow(manager, settings)
        window.show()  # refresh() only updates a window a user can see
        window.refresh()
        assert window.table.rowCount() == 1
        # Name lives in column 1 (column 0 is the type icon).
        name_item = window.table.item(0, 1)
        assert name_item is not None and name_item.text() == "x.bin"
        # Progress is a SmoothProgressBar and status a StatusPill (widget cells).
        assert isinstance(window._progress_bars[job.id], SmoothProgressBar)
        # The pill text carries a leading status dot ("●  Queued").
        assert window._pills[job.id].text().endswith("Queued")
    finally:
        manager.shutdown()


def test_settings_sections_and_new_fields(db: Database, monkeypatch):
    """The Settings page shows the agreed sections, in order, and the fields new
    to the restructure (playlist cap, FFmpeg override) persist."""
    from app.core import launcher
    from app.ui.settings_dialog import SettingsDialog

    _qapp()
    settings = Settings(db)
    dialog = SettingsDialog(settings)
    titles = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
    assert titles == [
        "General",
        "Downloads",
        "Download Engine",
        "Browser Integration",
        "Video Downloader",
        "Torrent",
        "Cloud Downloads",
        "Archive Manager",
        "File Management",
        "Queue Manager",
        "Scheduler",
        "Network",
        "Security",
        "Notifications",
        "Statistics",
        "Appearance",
        "Advanced",
        "Shortcuts",
        "About",
    ]
    monkeypatch.setattr(launcher, "set_autostart", lambda enabled: None)
    dialog.playlist_cap_spin.setValue(55)
    dialog.ffmpeg_override_edit.setText("/opt/ffmpeg/bin/ffmpeg")
    assert dialog.apply()
    assert settings.playlist_batch_cap == 55
    assert settings.ffmpeg_path == "/opt/ffmpeg/bin/ffmpeg"


def test_settings_video_page_reflows_when_narrow(db: Database):
    """Video Downloader (free-form) must wrap like About, not clip on resize."""
    from PySide6.QtCore import QSize
    from PySide6.QtWidgets import QLabel, QSizePolicy

    from app.ui.settings_view import SettingsView

    _qapp()
    view = SettingsView(Settings(db), lambda: None)
    view.resize(720, 640)
    # Pages are lifted out of the dialog tabs into the embedded stack.
    view._list.setCurrentRow(view._titles.index("Video Downloader"))
    page = view._dialog.hq_first_check.parentWidget()
    assert page is not None
    assert page.minimumWidth() == 0
    assert page.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Preferred

    notes = [w for w in page.findChildren(QLabel) if w.wordWrap()]
    assert notes
    narrow = 280
    assert any(n.heightForWidth(narrow) > n.fontMetrics().height() * 2 for n in notes)
    page.resize(QSize(narrow, 800))
    assert page.width() <= narrow + 8


def test_speed_smoother_is_steady_despite_checkpoint_aliasing():
    """A constant download must read as a constant speed.

    The engine flushes progress to SQLite every 0.3s but the UI polls every
    0.5s, so the byte count the UI sees is quantised to flush boundaries.
    Differencing two consecutive polls aliases that into a strobing readout;
    measuring across a window must not.
    """
    from app.ui.motion import SpeedSmoother

    rate = 10 * 1024 * 1024  # a steady 10 MB/s
    flush, poll = 0.3, 0.5
    smoother = SpeedSmoother()
    reads = [
        smoother.push_total(i * poll, int(rate * (int((i * poll) / flush) * flush)))
        for i in range(1, 40)
    ]
    settled = reads[8:]
    assert min(settled) > 0  # never strobes to zero
    assert all(abs(r - rate) < rate * 0.1 for r in settled)  # within 10% of the truth


def test_duplicate_prompt_yes_starts_a_new_download(db: Database, tmp_path: Path, monkeypatch):
    """Answering Yes to 'Download it again?' must actually queue it.

    PySide6's QMessageBox.question returns a plain int (16384), never the
    StandardButton member - so an `is` comparison silently takes the No branch
    and the click does nothing. Return what real Qt returns, so this test fails
    if anyone reaches for `is` again.
    """
    from PySide6.QtWidgets import QMessageBox

    _qapp()
    settings = Settings(db)
    settings.download_dir = tmp_path
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        url = "https://example.invalid/dup.bin"
        db.create_job(url, str(tmp_path), "dup.bin")  # the existing duplicate
        window = MainWindow(manager, settings)
        window.show()  # refresh() only updates a window a user can see

        monkeypatch.setattr(
            QMessageBox,
            "question",
            staticmethod(lambda *a, **k: int(QMessageBox.StandardButton.Yes)),
        )
        resolved: list[str] = []
        monkeypatch.setattr(window, "_resolve_and_queue", lambda u, *a, **k: resolved.append(u))

        window.begin_add_url(url)
        assert resolved == [url]  # Yes proceeded instead of silently returning
    finally:
        manager.shutdown()


def test_playlist_panel_selection(db: Database):
    from PySide6.QtCore import Qt

    from app.engines.smart import PlaylistEntry, PlaylistInfo
    from app.ui.playlist_panel import PlaylistPanel

    _qapp()
    playlist = PlaylistInfo(
        url="https://tube.example/playlist?list=1",
        title="Big Course",
        uploader="Prof",
        entries=tuple(
            PlaylistEntry(
                url=f"https://tube.example/watch?v={i}",
                title=f"Lesson {i}",
                duration=60,
                index=i,
            )
            for i in range(1, 41)
        ),
    )
    panel = PlaylistPanel(playlist, preselect_cap=30)
    assert panel.entry_list.count() == 40
    assert len(panel.selected_entries()) == 30  # cap preselects the first 30
    panel._set_all(Qt.CheckState.Checked)
    assert len(panel.selected_entries()) == 40
    panel._set_all(Qt.CheckState.Unchecked)
    panel.entry_list.item(2).setCheckState(Qt.CheckState.Checked)
    selected = panel.selected_entries()
    assert [entry.title for entry in selected] == ["Lesson 3"]
    assert panel.selected_option().label == "Best"


def test_link_panel_selection_and_filter(db: Database):
    from app.ui.link_panel import LinkPanel

    _qapp()
    urls = [
        "https://x.test/movie.mp4",
        "https://x.test/song.mp3",
        "https://x.test/notes.pdf",
        "https://x.test/page",
    ]
    panel = LinkPanel(urls)
    assert panel.list.count() == 4
    assert panel.selected_urls() == []  # nothing checked by default
    panel._select_by_ext((".mp4", ".mp3"))
    assert set(panel.selected_urls()) == {urls[0], urls[1]}
    panel.filter_box.setText("notes")
    assert panel.list.item(0).isHidden() and not panel.list.item(2).isHidden()


def test_remove_selected_and_clear_completed(db: Database, tmp_path: Path):
    from app.core.models import JobStatus

    _qapp()
    settings = Settings(db)
    settings.download_dir = tmp_path
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        ids = [db.create_job(f"http://x/{i}.bin", str(tmp_path), f"{i}.bin").id for i in range(3)]
        db.set_job_status(ids[2], JobStatus.COMPLETED)
        window = MainWindow(manager, settings)
        window.show()  # refresh() only updates a window a user can see
        window.refresh()
        assert window.table.rowCount() == 3

        # Remove two at once via the remembered multi-selection.
        window._selected_ids = {ids[0], ids[1]}
        window._remove_selected()
        assert {v.id for v in manager.snapshot()} == {ids[2]}

        # Clear the completed one.
        window._clear_completed()
        assert manager.snapshot() == []
    finally:
        manager.shutdown()


def test_main_window_sidebar_is_pure_navigation(db: Database, tmp_path: Path):
    _qapp()
    settings = Settings(db)
    settings.download_dir = tmp_path
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        window = MainWindow(manager, settings)
        window.show()  # refresh() only updates a window a user can see
        # The rail is navigation only; every tool moved to the downloads
        # toolbar, so there is no Tools page and no overflow menu.
        assert set(window._nav) == {"downloads", "dashboard", "queue", "settings"}
        assert not hasattr(window, "_overflow_menu")
        assert not hasattr(window, "_tools_view")
        window._switch_view("settings")
        assert window._page_index.get("settings") is not None
        assert window._pages.currentIndex() == window._page_index["settings"]
    finally:
        manager.shutdown()


def test_setup_dialog_builds_and_stages_extension(tmp_path, monkeypatch):
    from app.ui.setup_dialog import SetupDialog

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    _qapp()
    dialog = SetupDialog()
    # The wizard stages the extension and shows its stable path.
    from app.core import browser_setup

    assert dialog._folder_edit.text() == str(browser_setup.stable_extension_dir())
    assert (browser_setup.stable_extension_dir() / "manifest.json").is_file()


def test_theme_apply_switches_palette():
    from app.ui import theme

    app = _qapp()
    theme.remember_default(app)
    theme.apply_theme(app, "dark")
    dark_window = app.palette().color(app.palette().ColorRole.Window)
    theme.apply_theme(app, "light")
    light_window = app.palette().color(app.palette().ColorRole.Window)
    assert dark_window != light_window
    theme.apply_theme(app, "system")  # restores default without error


def test_main_window_search_filter(db: Database, tmp_path: Path):
    _qapp()
    settings = Settings(db)
    settings.download_dir = tmp_path
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        db.create_job("http://example.invalid/report.pdf", str(tmp_path), "report.pdf")
        db.create_job("http://example.invalid/movie.mkv", str(tmp_path), "movie.mkv")
        window = MainWindow(manager, settings)
        window.show()  # refresh() only updates a window a user can see
        window.refresh()
        assert window.table.rowCount() == 2
        window.search_box.setText("movie")
        assert window.table.isRowHidden(0)
        assert not window.table.isRowHidden(1)
        window.search_box.setText("")
        assert not window.table.isRowHidden(0)
    finally:
        manager.shutdown()


def test_quality_panel_selection_and_trim(db: Database):
    _qapp()
    media = MediaInfo(
        url="https://tube.example/watch?v=1",
        id="1",
        title="Panel Video",
        uploader="Someone",
        duration=125.0,
        thumbnail_url=None,
        options=(
            QualityOption(label="Best", kind="video", format_spec="bv*+ba/b"),
            QualityOption(
                label="1080p",
                kind="video",
                format_spec="bv*[height<=1080]+ba/b[height<=1080]",
                estimated_size=84 * 1024 * 1024,
            ),
            QualityOption(label="MP3", kind="audio", format_spec="ba/b", audio_format="mp3"),
        ),
        subtitle_languages=("en",),
        auto_caption_languages=("en", "de"),
    )
    panel = QualityPanel(media)
    assert panel.options_list.count() == 3
    assert panel.selected_option() is media.options[0]
    panel.options_list.setCurrentRow(2)
    selected = panel.selected_option()
    assert selected is not None and selected.audio_format == "mp3"

    # subtitles: None + en + de (auto); en (manual) wins over its auto twin
    assert panel.subtitle_combo.count() == 3
    panel.subtitle_combo.setCurrentIndex(1)
    config = panel.subtitles_config()
    assert config == {"lang": "en", "auto": False, "embed": False}

    panel.trim_start.setText("1:00")
    panel.trim_end.setText("2:05")
    assert panel.trim_range() == (60.0, 125.0)
    panel.trim_start.setText("")
    panel.trim_end.setText("")
    assert panel.trim_range() is None


def test_quality_panel_opens_before_analysis_and_upgrades_in_place(db: Database):
    """The picker is usable immediately on provisional tiers, and the analysed
    metadata replaces them without moving what the user already chose."""
    from app.engines.smart import provisional_media

    _qapp()
    url = "https://tube.example/watch?v=7"
    panel = QualityPanel(provisional_media(url, "Page Title"), analyzing=True)

    # Every tier is offered up front, and a choice made now is a real one.
    labels = [panel.options_list.item(i).text() for i in range(panel.options_list.count())]
    assert any(label.startswith("1080p") for label in labels)
    panel.options_list.setCurrentRow(labels.index(next(x for x in labels if x.startswith("720p"))))
    assert (chosen := panel.selected_option()) is not None and chosen.label == "720p"
    assert panel.subtitle_combo.count() == 1  # "None" only until analysis lands

    analysed = MediaInfo(
        url=url,
        id="7",
        title="The Real Title",
        uploader="Someone",
        duration=125.0,
        thumbnail_url=None,
        options=(
            QualityOption(label="Best", kind="video", format_spec="bv*+ba/b"),
            QualityOption(
                label="720p",
                kind="video",
                format_spec="bv*[height<=720]+ba/b[height<=720]",
                estimated_size=30 * 1024 * 1024,
            ),
        ),
        subtitle_languages=("en",),
    )
    panel.apply_media(analysed)

    assert panel.media is analysed
    assert panel.options_list.count() == 2
    # The user picked 720p before the sizes arrived; it stays picked, now sized.
    still = panel.selected_option()
    assert still is not None and still.label == "720p"
    assert still.estimated_size == 30 * 1024 * 1024
    assert "720p" in panel.options_list.currentItem().text()
    assert panel.subtitle_combo.count() == 2  # None + en


def test_quality_panel_extras_config(db: Database):
    _qapp()
    media = MediaInfo(
        url="https://tube.example/watch?v=1",
        id="1",
        title="Extras Video",
        uploader=None,
        duration=None,
        thumbnail_url=None,
        options=(QualityOption(label="Best", kind="video", format_spec="bv*+ba/b"),),
    )
    panel = QualityPanel(media)
    assert panel.extras_config() == {"chapters": True}  # chapters kept by default
    panel.save_thumbnail.setChecked(True)
    panel.save_metadata.setChecked(True)
    panel.keep_chapters.setChecked(False)
    panel.sponsorblock.setCurrentIndex(2)  # "Remove sponsor segments"
    assert panel.extras_config() == {
        "save_thumbnail": True,
        "save_metadata": True,
        "sponsorblock": "remove",
    }


def test_archive_dialog_selection(db: Database):
    from app.core.archive import ArchiveEntry
    from app.ui.archive_dialog import ArchiveDialog

    _qapp()
    entries = (
        ArchiveEntry("docs", None, True),
        ArchiveEntry("docs/a.txt", 42),
        ArchiveEntry("b.txt", 7),
    )
    dialog = ArchiveDialog("bundle.zip", entries)
    assert dialog.tree.topLevelItemCount() == 2  # dirs are not listed as rows
    assert dialog.selected_members() is None  # everything checked = extract all

    from PySide6.QtCore import Qt

    item = dialog.tree.topLevelItem(1)
    assert item is not None
    item.setCheckState(0, Qt.CheckState.Unchecked)
    assert dialog.selected_members() == ["docs/a.txt"]


def test_dupes_dialog_keeps_one_copy(db: Database, tmp_path: Path):
    from PySide6.QtCore import Qt

    from app.ui.dupes_dialog import DupesDialog

    _qapp()
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    c = tmp_path / "c.bin"
    for f in (a, b, c):
        f.write_bytes(b"same")
    dialog = DupesDialog([[a, b, c]])
    # Extras pre-checked, the first copy kept.
    assert dialog.selected_paths() == [b, c]
    # Even if every row gets checked, one copy always survives.
    top = dialog.tree.topLevelItem(0)
    assert top is not None
    top.child(0).setCheckState(0, Qt.CheckState.Checked)
    assert dialog.selected_paths() == [b, c]


def test_add_torrent_dialog_files_and_options(db: Database, tmp_path: Path):
    from PySide6.QtCore import Qt

    from app.engines.torrent import TorrentFileEntry, TorrentMeta
    from app.ui.torrent_dialog import AddTorrentDialog

    _qapp()
    meta = TorrentMeta(
        name="bundle",
        total_size=80_000,
        files=(
            TorrentFileEntry(0, "bundle/a.bin", 50_000),
            TorrentFileEntry(2, "bundle/b.bin", 30_000),  # pad file sits at index 1
        ),
        num_raw_files=3,
    )
    dialog = AddTorrentDialog("bundle", meta, tmp_path, sequential_default=True)
    assert dialog.dest_dir() == str(tmp_path)
    options = dialog.options()
    assert options["sequential"] is True and options["first_last"] is True
    assert "file_priorities" not in options  # everything checked

    assert dialog.tree is not None
    item = dialog.tree.topLevelItem(1)
    assert item is not None
    item.setCheckState(0, Qt.CheckState.Unchecked)
    priorities = dialog.options()["file_priorities"]
    assert priorities[2] == 0  # the real libtorrent index, not the row
    assert priorities[0] == 4 and priorities[1] == 4


def test_create_torrent_dialog_fields(db: Database):
    from app.ui.torrent_dialog import CreateTorrentDialog

    _qapp()
    dialog = CreateTorrentDialog()
    dialog.source_edit.setText("/data/share")
    dialog.trackers_edit.setPlainText("http://tr1/announce\n\nhttp://tr2/announce")
    dialog.webseeds_edit.setPlainText("https://mirror.example/share/")
    dialog.private_check.setChecked(True)
    assert str(dialog.source()) == "/data/share"
    assert dialog.trackers() == ("http://tr1/announce", "http://tr2/announce")
    assert dialog.web_seeds() == ("https://mirror.example/share/",)
    assert dialog.private() is True


def test_cloud_folder_dialog_selection(db: Database):
    from PySide6.QtCore import Qt

    from app.engines.cloud import RemoteFile
    from app.ui.cloud_dialog import CloudFolderDialog

    _qapp()
    files = [
        RemoteFile("sftp://host/dir/a.bin", "a.bin", 100),
        RemoteFile("sftp://host/dir/b.bin", "b.bin", 200),
    ]
    dialog = CloudFolderDialog("sftp://host/dir/", files)
    assert dialog.selected_urls() == ["sftp://host/dir/a.bin", "sftp://host/dir/b.bin"]
    item = dialog.tree.topLevelItem(0)
    assert item is not None
    item.setCheckState(0, Qt.CheckState.Unchecked)
    assert dialog.selected_urls() == ["sftp://host/dir/b.bin"]


def test_cloud_account_editor_builds_account(db: Database):
    from app.ui.cloud_dialog import _AccountEditor

    _qapp()
    editor = _AccountEditor()
    editor.service.setCurrentText("sftp")
    editor.host.setText("box.example")
    editor.username.setText("alice")
    editor.port.setText("2222")
    editor.secret.setText("s3cret")
    account, secret = editor.result_account()
    assert account.service == "sftp" and account.host == "box.example"
    assert account.username == "alice" and account.port == 2222
    assert secret == "s3cret"


def test_queue_cycle_guard(db: Database):
    from app.core.models import Queue
    from app.ui.queue_view import _would_cycle

    a = db.create_queue("A")
    b = db.create_queue("B")

    # Cycle detection: A -> B -> A would deadlock.
    db.update_queue(Queue(id=b.id, name="B", position=b.position, depends_on=a.id))
    queues = {q.id: q for q in db.list_queues()}
    assert _would_cycle(queues, a.id, b.id)
    assert not _would_cycle(queues, a.id, None)


def test_inspector_render_sections(db: Database):
    from app.core.inspector import InspectionReport, TlsInfo
    from app.ui.inspector_dialog import _render

    report = InspectionReport(
        url="https://example.com/f.bin",
        final_url="https://cdn.example.com/f.bin",
        status=200,
        ip_addresses=("93.184.216.34",),
        reverse_dns="edge.example.com",
        cdn="Cloudflare",
        server="nginx",
        mime_type="application/zip",
        content_length=1024,
        response_ms=42,
        headers=(("content-type", "application/zip"),),
        cookies=("sid=1",),
        redirect_chain=((301, "https://example.com/f.bin"),),
        tls=TlsInfo("TLSv1.3", "TLS_AES_256_GCM_SHA384", "example.com", "R3", "a", "b"),
        mirrors=("https://mirror.example/f.bin",),
        checksum="deadbeef",
    )
    text = _render(report)
    for needle in (
        "Cloudflare",
        "93.184.216.34",
        "TLSv1.3",
        "application/zip",
        "sid=1",
        "301",
        "mirror.example",
        "deadbeef",
        "42 ms",
    ):
        assert needle in text

    unreachable = InspectionReport(url="x", final_url="x", reachable=False, error="boom")
    assert "boom" in _render(unreachable)


def test_time_graph_pushes_samples(db: Database):
    from PySide6.QtGui import QColor

    from app.ui.graph import Series, TimeGraph

    _qapp()
    graph = TimeGraph("Test", [Series("a", QColor(1, 2, 3))], lambda v: f"{v:.0f}")
    for value in range(5):
        graph.push([float(value)])
    assert list(graph.series[0].samples) == [0.0, 1.0, 2.0, 3.0, 4.0]


def test_security_dialog_render(db: Database):
    from app.core.reputation import VirusTotalResult
    from app.core.security import Risk, SecurityReport
    from app.ui.security_dialog import _render

    _qapp()
    report = SecurityReport(
        path="/tmp/setup.exe",
        level=Risk.WARNING,
        findings=["This is an executable or installer.", "VirusTotal: 5 of 70 engines flagged."],
        checksums={"md5": "abc", "sha256": "def"},
        virustotal=VirusTotalResult(malicious=5, suspicious=0, total=70, known=True),
    )
    text = _render(report)
    assert "Warning" in text
    assert "executable" in text.lower()
    assert "VirusTotal" in text
    assert "MD5" in text and "SHA256" in text
    assert Risk.WARNING.label == "Warning"


def test_quality_label_add_skips_analysis(db: Database, tmp_path: Path, monkeypatch):
    """The in-page quality panel already chose - the add must queue straight to
    the download's single extraction (reels-fast), never analyze first."""
    _qapp()
    settings = Settings(db)
    settings.download_dir = tmp_path
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        window = MainWindow(manager, settings)
        window.show()  # refresh() only updates a window a user can see
        queued: list[tuple[str, str, str, dict[str, object]]] = []

        class _JobStub:
            id = 1

        def fake_add(url, title, option, **kw):
            queued.append((url, title, option.label, kw))
            return _JobStub()

        monkeypatch.setattr(manager, "add_smart_entry", fake_add)
        monkeypatch.setattr(window, "_fetch_quick_title", lambda *a, **k: None)
        resolved: list[str] = []
        monkeypatch.setattr(window, "_on_resolved", lambda *a, **k: resolved.append("x"))

        # A cold extractor list is built off-thread (it would freeze the GUI
        # here); warm it first so the fast path runs inline, as it does in a
        # running app once startup's warm-up has finished.
        window.resolver.smart.warm_up()
        window._resolve_and_queue(
            "https://www.youtube.com/watch?v=abc", "Video page", "1080p", (), None
        )
        assert len(queued) == 1 and queued[0][2] == "1080p"
        assert queued[0][3]["extras"] == {"name_from_metadata": True}
        assert window._resolve_threads == []  # no analysis thread was spawned
    finally:
        manager.shutdown()


def test_expanded_settings_roundtrip(db: Database, monkeypatch):
    """A sample from every new section persists through apply()."""
    from app.core import launcher
    from app.ui.settings_dialog import SettingsDialog

    _qapp()
    settings = Settings(db)
    dialog = SettingsDialog(settings)
    monkeypatch.setattr(launcher, "set_autostart", lambda enabled: None)

    dialog.tray_min_check.setChecked(True)
    dialog.new_dl_combo.setCurrentIndex(1)  # add paused
    dialog.ask_save_check.setChecked(True)
    dialog.free_mb_spin.setValue(1000)
    dialog.default_quality_combo.setCurrentText("1080p")
    dialog.bitrate_combo.setCurrentIndex(dialog.bitrate_combo.findData("320"))
    dialog.encryption_combo.setCurrentIndex(dialog.encryption_combo.findData("require"))
    dialog.seed_minutes_spin.setValue(90)
    dialog.extract_subfolder_check.setChecked(True)
    dialog.default_tags_edit.setText("new, fresh")
    dialog.battery_pct_spin.setValue(30)
    dialog.bypass_edit.setText("intranet.local, nas.home")
    dialog.ua_edit.setText("Grabline/UA")
    dialog.scanner_combo.setCurrentIndex(dialog.scanner_combo.findData("clamav"))
    dialog.notify_queue_check.setChecked(True)
    dialog.toast_spin.setValue(9)
    dialog.quiet_check.setChecked(True)
    dialog.retention_spin.setValue(30)
    dialog.accent_combo.setCurrentIndex(1)  # Violet
    dialog.density_combo.setCurrentIndex(dialog.density_combo.findData("compact"))
    dialog.column_checks["eta"].setChecked(False)
    dialog.log_combo.setCurrentIndex(dialog.log_combo.findData("debug"))
    assert dialog.apply()

    assert settings.minimize_to_tray is True
    assert settings.auto_start_downloads is False
    assert settings.ask_save_dir is True and settings.min_free_mb == 1000
    assert settings.video_default_quality == "1080p" and settings.audio_bitrate == "320"
    assert settings.torrent_encryption == "require" and settings.torrent_seed_minutes == 90
    assert settings.extract_to_subfolder is True
    assert settings.default_tags == "new, fresh"
    assert settings.battery_min_percent == 30
    assert settings.proxy_bypass == ("intranet.local", "nas.home")
    assert settings.user_agent == "Grabline/UA"
    assert settings.scanner_pref == "clamav"
    assert settings.notify_queue_done is True and settings.toast_seconds == 9
    assert settings.quiet_enabled is True
    assert settings.stats_retention_days == 30
    assert settings.accent_color == "#7c3aed"
    assert settings.ui_density == "compact"
    assert settings.hidden_columns == ("eta",)
    assert settings.log_level == "debug"


def test_paused_add_and_default_tags(db: Database, tmp_path: Path):
    """Settings → General 'Add paused' + File Management default tags apply to
    every new download."""
    from app.core.models import JobStatus

    settings = Settings(db)
    settings.download_dir = tmp_path
    settings.auto_start_downloads = False
    settings.default_tags = "inbox"
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        job = manager.add_url("https://example.invalid/held.bin")
        assert job.status is JobStatus.PAUSED
        fresh = db.get_job(job.id)
        assert fresh is not None and fresh.status is JobStatus.PAUSED
        assert fresh.options.get("tags") == "inbox"
    finally:
        manager.shutdown()


def test_accent_override_palette():
    from app.ui import design

    tinted = design.with_accent(design.DARK, "#7c3aed")
    assert tinted.accent == "#7c3aed"
    assert tinted.st_downloading == "#7c3aed" and tinted.g_dl == "#7c3aed"
    assert tinted.accent_h != tinted.accent  # hover derived, not identical
    assert design.DARK.accent != "#7c3aed"  # the base palette is untouched


def test_quiet_hours_window(db: Database):
    settings = Settings(db)
    assert settings.in_quiet_hours() is False  # off by default
    settings.quiet_enabled = True
    settings.quiet_from = "00:00"
    settings.quiet_to = "24:00"  # the whole day - always inside
    assert settings.in_quiet_hours() is True
    settings.quiet_enabled = False
    assert settings.in_quiet_hours() is False


def test_global_busy_indicator_tracks_background_work(db: Database, tmp_path: Path):
    """Anything that makes the user wait must show the activity shimmer: it
    appears when background work starts and disappears when the last finishes."""
    import time as _time

    _qapp()
    settings = Settings(db)
    settings.download_dir = tmp_path
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        window = MainWindow(manager, settings)
        window.show()  # refresh() only updates a window a user can see
        assert window._busy_bar.isHidden()

        window._busy_begin()
        assert not window._busy_bar.isHidden()
        window._busy_begin()
        assert window._busy_count.text() == "2 tasks"
        window._busy_end()
        assert not window._busy_bar.isHidden()  # one op still running
        window._busy_end()
        assert window._busy_bar.isHidden()

        # And the real funnel: a file op flips it on, completion flips it off.
        done: list[object] = []

        def slow_work() -> str:
            _time.sleep(0.05)
            return "x"

        window._run_file_op(slow_work, done.append)
        assert not window._busy_bar.isHidden()
        deadline = _time.monotonic() + 5
        while not done and _time.monotonic() < deadline:
            QApplication.processEvents()
        assert done == ["x"]
        assert window._busy_bar.isHidden()
    finally:
        manager.shutdown()


def test_detail_graph_history_survives_switching(db: Database, tmp_path: Path):
    from app.core.models import JobStatus

    _qapp()
    settings = Settings(db)
    settings.download_dir = tmp_path
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        window = MainWindow(manager, settings)
        window.show()  # refresh() only updates a window a user can see
        a = manager.add_url("https://example.com/a.zip", dest_dir=tmp_path)
        b = manager.add_url("https://example.com/b.zip", dest_dir=tmp_path)
        for job in (a, b):
            db.update_job_total(job.id, 100_000)
            db.set_job_status(job.id, JobStatus.DOWNLOADING)
        # several polls so both accumulate their own speed trail
        progress = {a.id: 0, b.id: 0}
        for _ in range(8):
            for job in (a, b):
                progress[job.id] += 1000
                db.update_job_downloaded(job.id, progress[job.id])
            window.refresh()

        def open_row(job_id: int) -> int:
            window.table.selectRow(window._row_job_ids.index(job_id))
            QApplication.processEvents()
            return len(window._drawer._spark._samples)

        opened = open_row(a.id)
        assert opened == len(window._spark_history[a.id]) > 3  # restored, not empty
        open_row(b.id)  # switch away
        back = open_row(a.id)  # and back
        assert back == len(window._spark_history[a.id]) > 3  # still preserved, not reset
    finally:
        manager.shutdown()


def test_resize_overlay_masks_border_and_hit_tests_edges(db: Database, tmp_path: Path):
    from PySide6.QtCore import QPoint, Qt

    _qapp()
    settings = Settings(db)
    settings.download_dir = tmp_path
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        window = MainWindow(manager, settings)
        window.show()  # refresh() only updates a window a user can see
        window.resize(1000, 600)
        window.show()
        QApplication.processEvents()
        r = window._resizer
        assert r.geometry() == window.rect()
        mask = r.mask()
        assert not mask.contains(QPoint(500, 300))  # center passes through to content
        assert mask.contains(QPoint(1, 300))  # left border is interactive
        E = Qt.Edge
        assert r._edges(QPoint(2, 300)) == E.LeftEdge
        assert r._edges(QPoint(2, 2)) == (E.LeftEdge | E.TopEdge)
        assert r._edges(QPoint(500, 300)) == E(0)
        window.showMaximized()
        QApplication.processEvents()
        r._sync()
        assert not r.isVisible()  # nothing to resize while maximized
    finally:
        manager.shutdown()


def test_caption_restore_works_when_qt_lies_about_maximized(db: Database, tmp_path: Path):
    """Windows frameless Qt6 can leave isMaximized() false after maximize.

    The first Restore click used to take the Maximize branch again; chrome must
    restore the saved geometry in one toggle regardless.
    """
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QGuiApplication

    _qapp()
    settings = Settings(db)
    settings.download_dir = tmp_path
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        window = MainWindow(manager, settings)
        window.show()
        normal = QRect(120, 80, 1000, 600)
        window.setGeometry(normal)
        QApplication.processEvents()
        bar = window._title_bar
        bar._maximize_window()
        QApplication.processEvents()
        # Simulate the Qt desync: visually filled, flag false.
        if window.isMaximized():
            window.showNormal()
            QApplication.processEvents()
        screen = window.screen() or QGuiApplication.primaryScreen()
        assert screen is not None
        window.setGeometry(screen.availableGeometry())
        bar._filled_screen = True
        bar._restore_geometry = QRect(normal)
        bar._sync_max_button()
        assert not window.isMaximized()
        assert bar.fills_screen()
        assert not window._resizer.isVisible()

        bar._toggle_maximized()  # one Restore click
        QApplication.processEvents()
        assert not bar.fills_screen()
        assert window.geometry() == normal
        assert window._resizer.isVisible()
    finally:
        manager.shutdown()


def test_toolbar_has_one_torrent_dropdown(db: Database, tmp_path: Path):
    from PySide6.QtWidgets import QMenu

    _qapp()
    settings = Settings(db)
    settings.download_dir = tmp_path
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        window = MainWindow(manager, settings)
        window.show()  # refresh() only updates a window a user can see
        labels = {a.text() for menu in window.findChildren(QMenu) for a in menu.actions()}
        # All three torrent actions are grouped under one dropdown.
        assert {"Add torrent…", "Search torrents…", "Create torrent…"} <= labels
    finally:
        manager.shutdown()


def test_reset_reloads_every_field_in_place(db: Database, monkeypatch):
    """Reset wipes the stored preferences and the open page shows the defaults
    straight away - no reopen, no restart."""
    from PySide6.QtWidgets import QMessageBox

    from app.core import launcher
    from app.ui.settings_dialog import SettingsDialog

    _qapp()
    settings = Settings(db)
    dialog = SettingsDialog(settings)
    monkeypatch.setattr(launcher, "set_autostart", lambda enabled: None)
    monkeypatch.setattr(launcher, "autostart_enabled", lambda: False)

    # Move a field of each kind away from its default, then save.
    dialog.concurrent_spin.setValue(9)  # spin
    dialog.tray_close_check.setChecked(False)  # check
    dialog.theme_combo.setCurrentIndex(dialog.theme_combo.findData("dark"))  # combo
    dialog.ua_edit.setText("Grabline/UA")  # line edit
    dialog.host_limits_edit.setPlainText("cdn.example.com = 500")  # plain text
    dialog.toast_spin.setValue(20)
    assert dialog.apply()
    assert Settings(db).max_concurrent == 9

    emitted: list[int] = []
    dialog.settings_reset.connect(lambda: emitted.append(1))
    monkeypatch.setattr(
        "app.ui.settings_dialog.QMessageBox.question",
        lambda *a, **k: QMessageBox.StandardButton.Yes,
    )
    dialog._reset_settings()

    assert emitted == [1]  # the window is told to re-read the store
    assert dialog.concurrent_spin.value() == 3
    assert dialog.tray_close_check.isChecked() is True
    assert dialog.theme_combo.currentData() == "system"
    assert dialog.ua_edit.text() == ""
    assert dialog.host_limits_edit.toPlainText() == ""
    assert dialog.toast_spin.value() == 4


def test_reset_is_cancelable(db: Database, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from app.core import launcher
    from app.ui.settings_dialog import SettingsDialog

    _qapp()
    settings = Settings(db)
    dialog = SettingsDialog(settings)
    monkeypatch.setattr(launcher, "set_autostart", lambda enabled: None)
    dialog.concurrent_spin.setValue(9)
    assert dialog.apply()

    monkeypatch.setattr(
        "app.ui.settings_dialog.QMessageBox.question",
        lambda *a, **k: QMessageBox.StandardButton.No,
    )
    dialog._reset_settings()

    assert Settings(db).max_concurrent == 9
    assert dialog.concurrent_spin.value() == 9


def test_close_to_tray_explains_itself_once(db: Database, tmp_path: Path):
    """Closing the window hides it. Say so the first time, or Grabline looks
    like it quit and the running downloads look lost."""
    from PySide6.QtGui import QCloseEvent

    _qapp()
    settings = Settings(db)
    settings.download_dir = tmp_path
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        window = MainWindow(manager, settings)
        window.show()  # refresh() only updates a window a user can see
        shown: list[tuple[str, str]] = []

        class FakeTray:
            def showMessage(self, title, body, icon, msecs):
                shown.append((title, body))

        window.tray = FakeTray()  # type: ignore[assignment]  # duck-typed tray stub
        window.close_to_tray = True  # a tray exists
        window.show()

        window.closeEvent(QCloseEvent())
        assert window.isHidden()
        assert len(shown) == 1
        assert "still running" in shown[0][0]

        window.show()
        window.closeEvent(QCloseEvent())
        assert len(shown) == 1  # said once, never nagged again
        assert settings.tray_hint_shown is True
    finally:
        manager.shutdown()


def test_close_to_tray_off_quits_normally(db: Database, tmp_path: Path):
    from PySide6.QtGui import QCloseEvent

    _qapp()
    settings = Settings(db)
    settings.download_dir = tmp_path
    settings.close_to_tray = False
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        window = MainWindow(manager, settings)
        window.show()  # refresh() only updates a window a user can see
        window.close_to_tray = True
        window.show()
        event = QCloseEvent()
        window.closeEvent(event)
        assert event.isAccepted()
        assert settings.tray_hint_shown is False  # nothing was hidden
    finally:
        manager.shutdown()


def _graph_x_extents(
    widget, plot_left: float, plot_width: float, frac: float
) -> tuple[float, float]:
    """Leftmost and rightmost point x the graph would paint at animation
    phase ``frac`` (0 = a sample just landed, 1 = the next is due)."""
    from app.ui.graph import _HISTORY

    step = plot_width / (_HISTORY - 1)
    newest = plot_left + plot_width + (1.0 - frac) * step
    last = len(widget.series[0].samples) - 1
    return newest - last * step, newest


def test_graph_curve_always_spans_its_plot(db: Database):
    """The scrolling curve must overhang both edges at every phase of the
    animation. If it falls short on the left, a gap opens and closes there on
    every push - which reads as a flicker, not a scroll."""
    from PySide6.QtGui import QColor

    from app.ui.graph import _HISTORY, Series, TimeGraph

    _qapp()
    serie = Series("Download", QColor("#3d8dfd"))
    graph = TimeGraph("Download", [serie], lambda v: f"{v:.0f}")
    for i in range(_HISTORY * 2):  # well past full, so the deque is rolling
        graph.push([float(i % 17)])

    assert len(serie.samples) == _HISTORY + 1  # one held off-screen on purpose

    left, width = 7.0, 300.0
    for k in range(41):
        frac = k / 40
        first_x, last_x = _graph_x_extents(graph, left, width, frac)
        assert first_x <= left, f"gap on the left at frac={frac:.2f} ({first_x:.2f} > {left})"
        assert last_x >= left + width, f"gap on the right at frac={frac:.2f}"


def test_dashboard_graph_card_scrolls_off_left_without_a_gap(db: Database):
    """Dashboard GraphCards used to pad from the left and drop the oldest
    sample on-screen, so the left edge blinked as points left. Match the
    detail sparkline: one off-screen sample + right-anchored scroll."""
    from app.ui import components

    _qapp()
    card = components.GraphCard("Download", ["#3d8dfd"], lambda v: f"{v:.0f}")
    history = components._GRAPH_HISTORY
    for i in range(history * 2):
        card.push([float(i % 17)])

    assert len(card._series[0]) == history + 1

    left, width = 12.0, 300.0
    step = width / (history - 1)
    last = len(card._series[0]) - 1
    for k in range(41):
        frac = k / 40
        newest = left + width + (1.0 - frac) * step
        first_x, last_x = newest - last * step, newest
        assert first_x <= left, f"gap on the left at frac={frac:.2f} ({first_x:.2f} > {left})"
        assert last_x >= left + width, f"gap on the right at frac={frac:.2f}"


def test_graph_scale_eases_down_but_snaps_up():
    """A peak scrolling out of the window must not rescale the whole curve in
    one frame; a peak arriving must be on screen the frame it lands."""
    from app.ui.motion import ease_scale

    assert ease_scale(0.0, 500.0) == 500.0  # first sample: adopt it
    assert ease_scale(100.0, 900.0) == 900.0  # rising snaps, never clips
    eased = ease_scale(1000.0, 100.0)
    assert 100.0 < eased < 1000.0  # falling eases
    for _ in range(400):  # and still gets there
        eased = ease_scale(eased, 100.0)
    assert abs(eased - 100.0) < 1.0


def test_idle_widgets_hold_no_ticker_subscription(db: Database, tmp_path: Path):
    """The 60fps heartbeat must have zero subscribers when nothing moves.

    A sparkline of nothing but zeros paints no line at all, and an idle
    Grabline used to scroll exactly that - forever, on every page, even hidden
    to the tray. Measured 13% of a core doing it."""
    from PySide6.QtGui import QColor

    from app.ui import motion
    from app.ui.graph import Series, TimeGraph

    _qapp()
    ticker = motion.ticker()
    before = ticker._subs

    spark = motion.Sparkline()
    graph = TimeGraph("CPU", [Series("cpu", QColor("#ff0000"))], lambda v: "")
    spark.show()
    graph.show()
    assert ticker._subs == before  # nothing to draw yet

    for _ in range(4):  # zeros are still nothing to draw
        spark.push(0.0)
        graph.push([0.0])
    assert ticker._subs == before
    assert spark._animating is False
    assert graph._animating is False

    spark.push(1024.0)  # real data -> animate
    graph.push([37.0])
    assert spark._animating is True
    assert graph._animating is True
    assert ticker._subs == before + 2

    spark.hide()  # off screen -> stop, even with data
    graph.hide()
    assert ticker._subs == before
    assert ticker._timer.isActive() is (before > 0)

    spark.deleteLater()
    graph.deleteLater()


def test_rebuilding_rows_gives_the_ticker_back(db: Database, tmp_path: Path):
    """Rows are rebuilt whenever the download list changes, and a bar thrown
    away mid-shimmer (any magnet resolving its metadata) used to keep its 30fps
    subscription forever - the whole app repainting for nobody. Qt runs no
    Python slot for a widget it is destroying, so the rebuild must release it.
    """
    from app.ui import motion

    _qapp()
    settings = Settings(db)
    settings.download_dir = tmp_path
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    ticker = motion.ticker()
    before = ticker._subs
    try:
        db.create_job("http://example.invalid/a.bin", str(tmp_path), "a.bin")
        window = MainWindow(manager, settings)
        window.show()
        window.refresh()
        for bar in window._progress_bars.values():
            bar.set_indeterminate(True)  # as a resolving magnet would
        assert ticker._subs > before

        db.create_job("http://example.invalid/b.bin", str(tmp_path), "b.bin")
        window.refresh()  # the row set changed -> rebuild
        assert ticker._subs == before
        assert ticker._timer.isActive() is (before > 0)
    finally:
        manager.shutdown()


def test_hidden_window_skips_refresh(db: Database, tmp_path: Path):
    """Hidden to the tray, refresh() writes into widgets nobody can see."""
    _qapp()
    settings = Settings(db)
    settings.download_dir = tmp_path
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        db.create_job("http://example.invalid/x.bin", str(tmp_path), "x.bin")
        window = MainWindow(manager, settings)
        window.hide()
        window.refresh()
        assert window.table.rowCount() == 0  # skipped entirely

        window.show()  # showEvent catches the list up
        assert window.table.rowCount() == 1
    finally:
        manager.shutdown()


def test_heavy_pages_are_built_on_first_visit(db: Database, tmp_path: Path):
    """Settings is ~500ms of widget construction and most sessions never open
    it, so it must not be on the path to the first painted window."""
    _qapp()
    settings = Settings(db)
    settings.download_dir = tmp_path
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        window = MainWindow(manager, settings)
        window.show()
        # Downloads and Dashboard exist up front (the dashboard samples history).
        assert set(window._page_index) == {"downloads", "dashboard"}
        assert "settings" not in window._page_index  # not built yet

        window._switch_view("settings")
        settings_page = window._pages.currentWidget()
        assert "settings" in window._page_index

        # Built once, then reused - switching away and back keeps the same page
        # (and any half-typed field on it).
        window._switch_view("downloads")
        window._switch_view("settings")
        assert window._pages.currentWidget() is settings_page

        window._switch_view("queue")
        queue_page = window._pages.currentWidget()
        window._switch_view("nonsense")  # unknown key stays a no-op
        assert window._pages.currentWidget() is queue_page
    finally:
        manager.shutdown()


def test_pause_shows_instantly_before_the_worker_settles(db: Database, tmp_path, monkeypatch):
    """Pausing an active download flips the row to Paused at once, even though
    the manager keeps the DB status on Downloading until the worker winds
    down - otherwise the button feels dead (the reported symptom)."""
    from dataclasses import replace

    from app.core.manager import JobView
    from app.core.models import JobKind, JobStatus

    _qapp()
    settings = Settings(db)
    settings.download_dir = tmp_path
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        window = MainWindow(manager, settings)
        window.show()
        job = db.create_job("http://x.invalid/a.bin", str(tmp_path), "a.bin")
        db.set_job_status(job.id, JobStatus.DOWNLOADING)

        paused: list[int] = []
        monkeypatch.setattr(manager, "pause", lambda jid: paused.append(jid))

        window._pause_job(job.id)
        assert paused == [job.id]  # the worker was signalled
        assert window._optimistic_status[job.id] is JobStatus.PAUSED

        # The manager still reports DOWNLOADING; the mask must show Paused.
        downloading_view = JobView(
            id=job.id,
            url="http://x.invalid/a.bin",
            filename="a.bin",
            dest_dir=str(tmp_path),
            status=JobStatus.DOWNLOADING,
            total_size=None,
            downloaded=0,
            error=None,
            kind=JobKind.DIRECT,
        )
        masked = window._apply_optimistic_status([downloading_view], {job.id})
        assert masked[0].status is JobStatus.PAUSED

        # Once the worker settles to the real Paused, the override is dropped.
        settled = replace(downloading_view, status=JobStatus.PAUSED)
        masked = window._apply_optimistic_status([settled], {job.id})
        assert masked[0].status is JobStatus.PAUSED
        assert job.id not in window._optimistic_status
    finally:
        manager.shutdown()


def test_add_download_dialog_fields_category_and_outcomes(db: Database):
    _qapp()
    from app.ui.add_download_dialog import AddDownloadDialog

    dialog = AddDownloadDialog(
        "https://cdn.example/movie/index.m3u8",
        suggested_name="The Odyssey",
        category="Video",
        download_dir=str(Path("/home/u/Downloads")),
        with_quality=True,
    )
    assert dialog.chosen_name() == "The Odyssey"
    assert dialog.chosen_directory() == str(Path("/home/u/Downloads/Video"))
    assert dialog.outcome() is None

    # The save folder follows the category until the user edits it.
    dialog._category.setCurrentText("Music")
    assert dialog.chosen_directory() == str(Path("/home/u/Downloads/Music"))

    # Start and Later are distinct outcomes; both accept the dialog.
    dialog._start()
    assert dialog.outcome() == "start"
    dialog._later()
    assert dialog.outcome() == "later"


def test_add_download_dialog_file_has_no_quality(db: Database):
    _qapp()
    from app.ui.add_download_dialog import AddDownloadDialog

    dialog = AddDownloadDialog(
        "https://cdn.example/archive.zip",
        suggested_name="archive.zip",
        category="Archives",
        download_dir=str(Path("/home/u/Downloads")),
        with_quality=False,
    )
    assert dialog._quality is None
    assert not dialog.dont_ask_again()


def test_shutdown_cancels_in_flight_file_ops(db: Database, tmp_path: Path):
    """An unbounded op (an installer download) must be cancelled on shutdown, so
    its worker returns and the exit wait doesn't time out on a running QThread -
    the abort in the update-download crash report."""
    import threading as _threading

    _qapp()
    settings = Settings(db)
    settings.download_dir = tmp_path
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        window = MainWindow(manager, settings)
        cancel = _threading.Event()
        window._op_cancels.add(cancel)
        assert not cancel.is_set()

        window.shutdown()

        assert cancel.is_set()  # shutdown asked the in-flight download to stop
    finally:
        manager.shutdown()


def test_dialog_buttons_are_stripped_of_icons():
    """A Linux GTK/Cinnamon platform theme paints a check on OK and a cross on
    Cancel even when the style hint that controls it is off. The app's event
    filter clears any such icon when a dialog is shown, so buttons stay text
    only. Icons are forced on here to stand in for the theme."""
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QDialog, QDialogButtonBox, QStyle, QVBoxLayout

    from app.__main__ import _NoDialogIcons

    app = _qapp()
    strip = _NoDialogIcons(app)
    app.installEventFilter(strip)
    dialog = QDialog()
    try:
        QVBoxLayout(dialog)
        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, dialog
        )
        painted = QIcon(app.style().standardIcon(QStyle.StandardPixmap.SP_DialogOkButton))
        for button in box.buttons():
            button.setIcon(painted)  # a theme has painted an icon on
        assert any(not b.icon().isNull() for b in box.buttons())  # sanity: icons are on
        dialog.show()
        app.processEvents()
        assert all(b.icon().isNull() for b in box.buttons())  # the filter cleared them
    finally:
        app.removeEventFilter(strip)
        dialog.close()


def test_browser_grab_of_video_runs_analysis_for_the_full_panel(
    db: Database, tmp_path: Path, monkeypatch
):
    """A YouTube hover grab must analyse the URL (so the real title resolves and
    the full quality panel is shown) rather than the quick Download Info dialog -
    the fix for 'watch' filenames and the missing settings popup on hover.
    confirm_video_quality defaults to True, so no setting needs to be touched."""
    _qapp()
    settings = Settings(db)
    settings.download_dir = tmp_path
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        window = MainWindow(manager, settings)
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert window.resolver.smart.matches(url)  # sanity: this is a smart video

        seen: list[object] = []
        monkeypatch.setattr(
            window,
            "_resolve_and_queue",
            lambda u, page_title, quality, *a, **k: seen.append((u, quality)),
        )
        window._browser_add(url, None, (), None)

        # Routed to analysis with quality=None -> _on_resolved opens QualityPanel.
        assert seen == [(url, None)]
    finally:
        manager.shutdown()


def test_single_video_urls_open_the_picker_ahead_of_analysis(db: Database, tmp_path: Path):
    """A single video shows its quality picker straight away; anything that
    might analyse into a list waits, so the picker isn't swapped out from
    under the user a second later."""
    from app.ui.main_window import _looks_like_one_video

    assert _looks_like_one_video("https://www.youtube.com/watch?v=abc")
    assert _looks_like_one_video("https://www.youtube.com/watch?v=abc&list=PL123")
    assert _looks_like_one_video("https://vimeo.com/123456")
    assert not _looks_like_one_video("https://www.youtube.com/playlist?list=PL123")
    assert not _looks_like_one_video("https://www.youtube.com/@somechannel")
    assert not _looks_like_one_video("https://www.youtube.com/channel/UC123")


def test_resolve_and_queue_takes_the_instant_panel_path(db: Database, tmp_path: Path, monkeypatch):
    """A pasted video URL must reach the instant picker, not the old
    wait-for-analysis-then-open-a-dialog route."""
    from app.ui import work_threads

    _qapp()
    settings = Settings(db)
    settings.download_dir = tmp_path
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        window = MainWindow(manager, settings)
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert window.resolver.smart.matches(url)  # warms the extractor list

        instant: list[str] = []
        monkeypatch.setattr(
            window,
            "_instant_quality_panel",
            lambda thread, u, *a, **k: instant.append(u),
        )
        # No thread may actually run: a real resolve of these URLs would emit
        # its result into whatever test is pumping events next, and _on_resolved
        # answers a dead URL with a modal box that nothing is there to close.
        monkeypatch.setattr(work_threads.ResolveThread, "start", lambda self: None)

        window._resolve_and_queue(url, None, None, (), None)
        assert instant == [url]

        # A plain file has nothing to analyse - it must not open a video picker.
        instant.clear()
        window._resolve_and_queue("https://files.example/big.zip", None, None, (), None)
        assert instant == []
    finally:
        manager.shutdown()


def test_video_quality_panel_survives_confirm_downloads_off(
    db: Database, tmp_path: Path, monkeypatch
):
    """Turning off 'ask for name and folder' (confirm_downloads) must not also
    silence the video quality panel - they are independent settings. Regression
    test for the bug where a single checkbox controlled both."""
    _qapp()
    settings = Settings(db)
    settings.download_dir = tmp_path
    settings.confirm_downloads = False  # off: skip the plain file name/folder prompt
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        window = MainWindow(manager, settings)
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        window.resolver.smart.matches(url)  # warm the extractor list synchronously

        seen: list[object] = []
        monkeypatch.setattr(
            window,
            "_resolve_and_queue",
            lambda u, page_title, quality, *a, **k: seen.append((u, quality)),
        )
        window._browser_add(url, None, (), None)

        # confirm_video_quality defaults to True regardless of confirm_downloads.
        assert seen == [(url, None)]
    finally:
        manager.shutdown()


def test_video_quality_panel_off_queues_at_default_quality(
    db: Database, tmp_path: Path, monkeypatch
):
    """confirm_video_quality off queues immediately at the default quality with
    no panel - independent of confirm_downloads, which stays on here."""
    _qapp()
    settings = Settings(db)
    settings.download_dir = tmp_path
    settings.confirm_video_quality = False
    settings.video_default_quality = "720p"
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        window = MainWindow(manager, settings)
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        window.resolver.smart.matches(url)  # warm the extractor list synchronously

        seen: list[object] = []
        monkeypatch.setattr(
            window,
            "_resolve_and_queue",
            lambda u, page_title, quality, *a, **k: seen.append((u, quality)),
        )
        window._browser_add(url, None, (), None)

        assert seen == [(url, "720p")]
    finally:
        manager.shutdown()


def _drawer(manager: DownloadManager, ffmpeg: str | None = None) -> DetailDrawer:
    from app.ui.detail_drawer import DetailDrawer

    noop = lambda _v: None  # noqa: E731
    return DetailDrawer(
        manager,
        ffmpeg=ffmpeg,
        on_open_file=noop,
        on_open_folder=noop,
        on_rename=noop,
        on_remove=noop,
        on_extract=noop,
        on_security=noop,
    )


def _pump(times: int = 60) -> None:
    import time

    app = _qapp()
    for _ in range(times):
        app.processEvents()
        time.sleep(0.01)


def test_job_timestamps_round_trip(db: Database, tmp_path: Path):
    job = db.create_job("http://example.invalid/x.bin", str(tmp_path), "x.bin")
    created, updated = db.job_timestamps(job.id)
    assert created and updated  # both stamped on insert
    assert db.job_timestamps(999_999) == (None, None)


def test_detail_drawer_populates_real_fields(db: Database, tmp_path: Path):
    from app.core.models import JobStatus

    _qapp()
    settings = Settings(db)
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        # A finished, on-disk download of an unknown (non-media) type.
        target = tmp_path / "report.bin"
        target.write_bytes(b"x" * 4096)
        job = db.create_job("https://files.example.com/a/report.bin", str(tmp_path), "report.bin")
        db.update_job_total(job.id, 4096)
        db.set_job_status(job.id, JobStatus.COMPLETED)
        view = next(v for v in manager.snapshot() if v.id == job.id)

        drawer = _drawer(manager, ffmpeg=None)
        drawer.show_view(view, 0.0, [])

        def cell(grid: str, key: str) -> str:
            return str(getattr(drawer, grid)._rows[key][1].text())

        assert cell("_file_grid", "Type") == "BIN file"
        assert cell("_file_grid", "Size on disk") == "4.0 KB"
        assert cell("_file_grid", "Added")  # a formatted timestamp
        assert cell("_source_grid", "Server") == "files.example.com"
        assert cell("_source_grid", "Protocol") == "HTTPS"
        assert cell("_source_grid", "Connections") == str(manager.connections)
        # No ffmpeg and not an archive -> the third tab is hidden.
        assert drawer._tabs[2].isHidden()
        # Activity was folded into Overview; footer is the short action strip.
        assert set(drawer._act_btns) == {"open", "folder", "rename", "remove"}
        assert len(drawer._tabs) == 3
        from app.ui.components import IconButton

        assert isinstance(drawer._security_btn, IconButton)
    finally:
        manager.shutdown()


def test_detail_drawer_archive_contents(db: Database, tmp_path: Path):
    import zipfile

    from app.core.models import JobStatus

    _qapp()
    settings = Settings(db)
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        archive_path = tmp_path / "bundle.zip"
        with zipfile.ZipFile(archive_path, "w") as zf:
            for name in ("a.txt", "b.txt", "c.txt"):
                zf.writestr(name, "hello")
        job = db.create_job("https://x.example/bundle.zip", str(tmp_path), "bundle.zip")
        db.set_job_status(job.id, JobStatus.COMPLETED)
        view = next(v for v in manager.snapshot() if v.id == job.id)

        drawer = _drawer(manager)
        drawer.show_view(view, 0.0, [])
        _pump()  # let the archive-listing worker finish
        assert drawer._tabs[2].text() == "Contents"
        assert not drawer._tabs[2].isHidden()
        assert drawer._media_grid._rows["Files"][1].text() == "3"
    finally:
        manager.shutdown()


def test_detail_drawer_media_tab_stays_visible(db: Database, tmp_path: Path):
    """A completed video's Media tab must show after the ffprobe worker fills it
    - it lives on a non-current stacked page, so the visibility test has to read
    the explicit flag, not 'is on screen' (the regression this guards)."""
    import shutil
    import subprocess

    from app.core.models import JobStatus

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not installed")

    video = tmp_path / "clip.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "quiet",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=320x240:rate=24",
            "-c:v",
            "libx264",
            str(video),
        ],
        check=True,
    )
    _qapp()
    settings = Settings(db)
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        job = db.create_job("https://v.example/clip.mp4", str(tmp_path), "clip.mp4")
        db.update_job_total(job.id, video.stat().st_size)
        db.set_job_status(job.id, JobStatus.COMPLETED)
        view = next(v for v in manager.snapshot() if v.id == job.id)
        drawer = _drawer(manager, ffmpeg=ffmpeg)
        drawer.show_view(view, 0.0, [])
        _pump()
        assert drawer._tabs[2].text() == "Media"
        assert not drawer._tabs[2].isHidden()
        assert drawer._media_grid._rows["Resolution"][1].text() == "320x240"
        assert drawer._media_grid._rows["Video"][1].text() == "H264"
    finally:
        manager.shutdown()
