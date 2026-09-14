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
