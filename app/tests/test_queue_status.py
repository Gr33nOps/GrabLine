"""Queue identity stays visible in waiting rows and the drawer, at narrow widths."""

from dataclasses import replace

from app.core.manager import DownloadManager
from app.core.models import JobStatus
from app.core.settings import Settings
from app.tests.qt_app import ensure_app
from app.ui.main_window import _COL_STATUS, MainWindow
from scripts.package_extension import _manifest_for


def test_waiting_status_shows_queue_and_updates_after_rename_or_move(db, tmp_path):
    app = ensure_app()
    settings = Settings(db)
    settings.download_dir = tmp_path
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    window = MainWindow(manager, settings)
    try:
        queue = manager.create_queue("Movies")
        job = db.create_job("https://example.invalid/file", str(tmp_path), "file.bin")
        manager.set_job_queue(job.id, queue.id)
        window.show()
        window.resize(950, 650)
        window.refresh()
        app.processEvents()
        pill = window._pills[job.id]
        assert pill.full_text() == "●  Queued: Movies"
        assert pill.text() == pill.full_text()
        window._drawer.show_view(manager.snapshot()[0], 0)
        app.processEvents()
        assert window._drawer._pill.full_text() == pill.full_text()
        assert window._drawer._pill.text() == pill.full_text()
        assert window._drawer._pill.width() > 0

        renamed = replace(queue, name="A very long queue name " * 12)
        manager.update_queue(renamed)
        window.refresh()
        app.processEvents()
        assert pill.full_text() == f"●  Queued: {renamed.name}"
        assert "…" in pill.text()
        assert pill.toolTip() == pill.full_text()
        assert window.table.columnWidth(_COL_STATUS) <= 240
        assert window._drawer._source_grid._rows["Queue"][1].text() == renamed.name

        manager.set_job_queue(job.id, None)
        window.refresh()
        app.processEvents()
        assert pill.full_text() == "●  Waiting to start"
        assert pill.text() == pill.full_text()
        assert pill.width() >= pill.fontMetrics().horizontalAdvance(pill.full_text())
        assert window._drawer._source_grid._rows["Queue"][1].text() == "Default"
        db.set_job_status(job.id, JobStatus.COMPLETED)
        window.refresh()
        assert pill.full_text() == "●  Completed"
    finally:
        window.shutdown()
        window.close()
        manager.shutdown()


def test_store_packages_enable_blocking_only_on_firefox():
    import json
    from pathlib import Path

    manifest = json.loads((Path(__file__).parents[2] / "extension/manifest.json").read_text())
    firefox = _manifest_for("firefox", manifest)
    chrome = _manifest_for("chrome", manifest)
    assert "webRequestBlocking" in firefox["permissions"]
    assert "webRequestBlocking" not in chrome["permissions"]
    assert "downloads.ui" not in firefox["permissions"]
    assert "scripts" in firefox["background"]
    assert "service_worker" in chrome["background"]
