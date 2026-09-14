from __future__ import annotations

from pathlib import Path

from app.core.instance import app_is_running, clear_pid, write_pid


def test_pid_lifecycle(tmp_path: Path):
    pid_path = tmp_path / "grabline.pid"
    assert not app_is_running(pid_path)
    write_pid(pid_path)
    assert app_is_running(pid_path)  # we are that process
    clear_pid(pid_path)
    assert not app_is_running(pid_path)
    clear_pid(pid_path)  # idempotent


def test_stale_pid_is_not_running(tmp_path: Path):
    pid_path = tmp_path / "grabline.pid"
    pid_path.write_text("99999999")  # far beyond pid_max: provably no such process
    assert not app_is_running(pid_path)


def test_a_reused_pid_does_not_count_as_running(tmp_path: Path):
    """The crash case: GrabLine dies, its pid file survives, and the OS hands
    that pid to something unrelated (here, the test runner itself). The
    extension would then cancel browser downloads and hand them to nobody.
    The live app holds a lock on this file, and nothing holds this one."""
    import os

    pid_path = tmp_path / "grabline.pid"
    pid_path.write_text(str(os.getpid()))  # a real, live, but unrelated process
    assert not app_is_running(pid_path)


def test_garbage_pid_file(tmp_path: Path):
    pid_path = tmp_path / "grabline.pid"
    pid_path.write_text("not a pid")
    assert not app_is_running(pid_path)
    pid_path.write_text("-5")
    assert not app_is_running(pid_path)
