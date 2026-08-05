"""Regression tests for the worker-thread lifetime fixes (the field crashes:
'QThread: Destroyed while thread is still running').

The core defect: a dialog owned its worker QThread, and closing the dialog
while the worker still ran destroyed a running thread -> SIGABRT. ``retain``
moves ownership to a module registry until the thread finishes; ``shutdown``
drains anything still running before the interpreter tears it down. These
tests exercise that contract without needing to trigger the native abort.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from app.ui import threads


def _qapp() -> QApplication:
    instance = QApplication.instance()
    return instance if isinstance(instance, QApplication) else QApplication([])


class _Sleeper(QThread):
    def __init__(self, seconds: float) -> None:
        super().__init__()
        self._seconds = seconds

    def run(self) -> None:
        time.sleep(self._seconds)


def test_retain_holds_then_releases_on_finish():
    _qapp()
    thread = _Sleeper(0.05)
    threads.retain(thread)
    assert thread in threads._RUNNING  # owned while running
    thread.start()
    assert thread.wait(2000)  # finished
    # Let the queued finished->release slot run.
    deadline = time.monotonic() + 2
    while thread in threads._RUNNING and time.monotonic() < deadline:
        QApplication.processEvents()
    assert thread not in threads._RUNNING  # released after finish


def test_shutdown_waits_for_a_running_retained_thread():
    _qapp()
    thread = _Sleeper(0.4)
    threads.retain(thread)
    thread.start()
    assert thread.isRunning()
    # shutdown must block until the worker has actually stopped: destroying a
    # running QThread is what aborted the process in the field.
    threads.shutdown(timeout_ms=5000)
    assert not thread.isRunning()


def test_shutdown_is_a_noop_when_nothing_is_running():
    _qapp()
    # A normal quit (no in-flight fetches) must return immediately.
    start = time.monotonic()
    threads.shutdown(timeout_ms=5000)
    assert time.monotonic() - start < 0.5


def test_quality_panel_open_close_with_thumbnail_does_not_leak_a_live_owner():
    """The panel's thumbnail fetcher must be retained (not owned by the dialog),
    so closing the panel mid-fetch cannot destroy a running thread."""
    from app.engines.smart import MediaInfo, QualityOption
    from app.ui import theme
    from app.ui.quality_panel import QualityPanel

    app = _qapp()
    theme.remember_default(app)
    media = MediaInfo(
        url="https://youtube.com/watch?v=x",
        id="x",
        title="t",
        uploader="u",
        duration=1.0,
        thumbnail_url="http://10.255.255.1/never.jpg",  # never answers quickly
        options=(QualityOption("Best", "video", "b", None, 1000),),
    )
    panel = QualityPanel(media)
    fetcher = panel._fetcher
    assert fetcher is not None
    assert fetcher in threads._RUNNING  # retained, not dialog-owned
    panel.done(0)  # close before the fetch returns
    panel.deleteLater()
    # The fetcher is still tracked and will be drained at shutdown, never
    # destroyed out from under a running thread.
    threads.shutdown(timeout_ms=8000)
    assert not fetcher.isRunning()
