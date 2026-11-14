"""Restart outcome checks independent of another test's live Qt application."""

import sys

from PySide6.QtWidgets import QApplication

from app.core import launcher
from app.tests.conftest import wait_for


def test_restart_without_qt_spawns_the_current_install(monkeypatch):
    seen = []
    monkeypatch.setattr(QApplication, "instance", lambda: None)

    def launch(command, **kwargs):
        seen.append(command)

    monkeypatch.setattr("subprocess.Popen", launch)

    assert launcher.restart_current() is True
    assert seen == [[sys.executable, "-m", "app"]]


def test_failed_qt_restart_does_not_report_success(tmp_path, monkeypatch):
    # Exercise the real Qt failure tuple, which is itself truthy.
    # No executable exists at this disposable path, so no app is launched.
    monkeypatch.setattr(QApplication, "instance", lambda: object())
    monkeypatch.setattr(launcher, "launch_command", lambda: [str(tmp_path / "missing-app")])

    assert launcher.restart_current() is False


def test_qt_restart_starts_a_real_disposable_child(tmp_path, monkeypatch):
    marker = tmp_path / "started.txt"
    script = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('started')"
    monkeypatch.setattr(QApplication, "instance", lambda: object())
    monkeypatch.setattr(
        launcher, "launch_command", lambda: [sys.executable, "-c", script, str(marker)]
    )

    assert launcher.restart_current() is True
    wait_for(lambda: marker.exists() and marker.read_text() == "started", timeout=10)
