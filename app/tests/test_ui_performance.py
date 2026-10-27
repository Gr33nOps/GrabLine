"""Responsiveness regressions: slow probes must not block the Qt event loop."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication, QTimer

from app.core import net
from app.core.manager import DownloadManager
from app.core.settings import Settings
from app.tests.qt_app import ensure_app
from app.ui import threads
from app.ui.dashboard_view import DashboardView
from app.ui.settings_view import SettingsView


def pump(app: QCoreApplication, predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert predicate()


def test_settings_opens_while_vpn_probe_is_blocked(db, monkeypatch):
    app = ensure_app()
    entered, release = threading.Event(), threading.Event()
    caller_threads = []

    def slow_probe():
        caller_threads.append(threading.get_ident())
        entered.set()
        release.wait(5)
        return ["wg0"]

    monkeypatch.setattr(net, "active_vpn_interfaces", slow_probe)
    try:
        view = SettingsView(Settings(db), lambda: None)
        view.show()
        pump(app, entered.is_set)
        assert len(caller_threads) == 1
        assert caller_threads[0] != threading.get_ident()
        # All controls exist for Save, but hidden sections are not laid out.
        assert len(view._pending_pages) == len(view._titles) - 1
        index = view._titles.index("Network")
        view._list.setCurrentRow(index)
        assert index not in view._pending_pages
        assert not release.is_set()
        release.set()
        pump(app, lambda: "wg0" in view._dialog._vpn_note.text())
        view.close()
        view.deleteLater()
    finally:
        release.set()
        threads.shutdown()


def test_dashboard_probe_is_single_flight_and_does_not_block_navigation(db, monkeypatch):
    app = ensure_app()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow_probe():
        calls.append(threading.get_ident())
        entered.set()
        release.wait(5)
        return ["wg0"]

    monkeypatch.setattr(net, "active_vpn_interfaces", slow_probe)
    manager = DownloadManager(db, settings=Settings(db), max_concurrent=0)
    view = DashboardView(manager)
    try:
        view.show()
        pump(app, entered.is_set)
        heartbeat = []
        QTimer.singleShot(0, lambda: heartbeat.append(True))
        view.hide()
        view.show()
        for _ in range(5):
            view._tick()
        pump(app, lambda: bool(heartbeat))
        assert len(calls) == 1
        assert calls[0] != threading.get_ident()
        release.set()
        pump(app, lambda: not view._heavy_pending)
        assert "wg0" in view._vpn.text()
    finally:
        release.set()
        view.shutdown()
        threads.shutdown()
        view.close()
        view.deleteLater()
        manager.shutdown()


def test_dashboard_tables_preserve_rows_and_selection(db):
    app = ensure_app()
    manager = DownloadManager(db, settings=Settings(db), max_concurrent=0)
    view = DashboardView(manager)
    try:
        tree = view.server_tree
        view._fill(tree, [("host", 100, 1), ("other", 200, 2)])
        item = tree.topLevelItem(0)
        assert item is not None
        item.setSelected(True)
        view._fill(tree, [("host", 300, 3)])
        assert tree.topLevelItem(0) is item
        assert item.isSelected()
        assert item.text(2) == "3"
        assert tree.topLevelItemCount() == 1
        view._fill(tree, [])
        assert tree.topLevelItemCount() == 0
    finally:
        view.shutdown()
        view.deleteLater()
        app.processEvents()
        manager.shutdown()


def test_download_rows_catch_up_after_visiting_another_page(db, tmp_path):
    app = ensure_app()
    from app.ui.main_window import MainWindow

    settings = Settings(db)
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    window = MainWindow(manager, settings)
    try:
        db.create_job("https://example.invalid/one", str(tmp_path), "one")
        window.show()
        window.refresh()
        assert window.table.rowCount() == 1
        window._switch_view("dashboard")
        db.create_job("https://example.invalid/two", str(tmp_path), "two")
        window.refresh()
        assert len(window._last_views) == 2  # background state stays current
        assert window.table.rowCount() == 1  # hidden table does no layout work
        window._switch_view("downloads")
        assert window.table.rowCount() == 2
    finally:
        window.shutdown()
        window.hide()
        window.deleteLater()
        app.processEvents()
        manager.shutdown()


def test_dashboard_recovers_after_summary_failure(db, monkeypatch):
    app = ensure_app()
    manager = DownloadManager(db, settings=Settings(db), max_concurrent=0)
    view = DashboardView(manager)
    calls = []
    totals = manager.stat_totals

    def flaky_totals():
        calls.append(True)
        if len(calls) == 1:
            raise OSError("temporary read failure")
        return totals()

    monkeypatch.setattr(manager, "stat_totals", flaky_totals)
    monkeypatch.setattr(net, "active_vpn_interfaces", lambda: ["wg0"])
    try:
        view.show()
        pump(app, lambda: bool(calls) and not view._heavy_pending)
        assert view._heavy_at > 0  # failures also respect the retry interval
        view._heavy_at = 0
        view._tick()
        pump(app, lambda: len(calls) == 2 and not view._heavy_pending)
        assert "wg0" in view._vpn.text()
    finally:
        view.shutdown()
        threads.shutdown()
        view.close()
        view.deleteLater()
        manager.shutdown()
