from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from app.db.database import Database
from app.tests.media_server import MediaServer


@pytest.fixture()
def server() -> Iterator[MediaServer]:
    media_server = MediaServer()
    media_server.start()
    yield media_server
    media_server.stop()


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "grabline-test.db")
    yield database
    database.close()


@pytest.fixture()
def dest(tmp_path: Path) -> Path:
    directory = tmp_path / "downloads"
    directory.mkdir()
    return directory


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def wait_for(condition: Callable[[], bool], timeout: float = 30.0, interval: float = 0.01) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


@pytest.fixture(autouse=True)
def _quiet_leaked_main_windows(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A test that builds a ``MainWindow`` and never calls its ``shutdown()``
    leaves the window's poll timers running; they then fire during a later
    test, against a database that test already closed. At teardown every
    window this test made is shut down (its own ``shutdown()``, which is what
    stops those timers)."""
    try:
        from app.ui import main_window as module
    except Exception:  # the UI is not importable here (no Qt): nothing to track
        yield
        return
    made: list[object] = []
    original = module.MainWindow.__init__

    def tracking(self: object, *args: object, **kwargs: object) -> None:
        made.append(self)
        original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(module.MainWindow, "__init__", tracking)
    yield
    for window in made:
        if not getattr(window, "_shutting_down", True):
            try:
                window.shutdown()  # type: ignore[attr-defined]
            except Exception:  # the test already closed what it owned
                for name in ("_timer", "_handoff_timer", "_rss_timer", "_watch_timer"):
                    timer = getattr(window, name, None)
                    if timer is not None:
                        timer.stop()
                window._shutting_down = True  # type: ignore[attr-defined]
