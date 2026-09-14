"""Scheduler extras: download later (start at), weekday restrictions, battery
mode, wait-for-network with instant retry on reconnect, the new power actions,
the completion script, and the completion sound plumbing.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.core import power, scripts
from app.core.manager import DownloadManager
from app.core.settings import Settings
from app.db.database import Database

# ------------------------------------------------------------------- power


def test_power_actions_launch_platform_commands(monkeypatch: pytest.MonkeyPatch):
    launched: list[list[str]] = []
    monkeypatch.setattr("app.core.power.subprocess.Popen", lambda cmd, **kw: launched.append(cmd))
    assert power.sleep() and power.shutdown() and power.hibernate() and power.lock()
    assert len(launched) == 4
    if sys.platform == "linux":
        assert ["systemctl", "hibernate"] in launched
        assert ["loginctl", "lock-session"] in launched


def test_on_battery_reads_psutil_and_caches(monkeypatch: pytest.MonkeyPatch):
    class _Battery:
        power_plugged = False

    monkeypatch.setattr(power, "_battery_checked", 0.0)
    import psutil

    monkeypatch.setattr(psutil, "sensors_battery", lambda: _Battery())
    assert power.on_battery() is True
    # Cached: even if the sensor changes, within the window the answer holds.
    monkeypatch.setattr(psutil, "sensors_battery", lambda: None)
    assert power.on_battery() is True
    monkeypatch.setattr(power, "_battery_checked", 0.0)  # expire the cache
    assert power.on_battery() is False  # None (desktop) = plugged in


# ------------------------------------------------------------------ script


def test_run_script_appends_file_path(tmp_path: Path):
    marker = tmp_path / "ran.txt"
    target = tmp_path / "movie.mkv"
    script = tmp_path / "hook.sh"
    script.write_text(f'#!/bin/sh\necho "$1" > "{marker}"\n')
    script.chmod(0o755)
    assert scripts.run_script(str(script), str(target))
    deadline = time.time() + 5
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.read_text().strip() == str(target)


def test_run_script_rejects_junk():
    assert not scripts.run_script("", "/tmp/x")
    assert not scripts.run_script('unclosed "quote', "/tmp/x")
    assert not scripts.run_script("/no/such/binary-here", "/tmp/x")


# ---------------------------------------------------------------- settings


def test_scheduler_settings_roundtrip(db: Database):
    settings = Settings(db)
    assert settings.download_days == (0, 1, 2, 3, 4, 5, 6)
    assert settings.pause_on_battery is False
    assert settings.wait_for_network is False
    assert settings.sound_on_complete is False
    assert settings.script_on_complete == ""

    settings.download_days = [5, 6]  # weekend downloads
    settings.pause_on_battery = True
    settings.wait_for_network = True
    settings.sound_on_complete = True
    settings.sound_file = "/tmp/ding.wav"
    settings.script_on_complete = "/usr/bin/notify-send done"
    settings.after_queue_action = "hibernate"

    fresh = Settings(db)
    assert fresh.download_days == (5, 6)
    assert fresh.pause_on_battery is True
    assert fresh.wait_for_network is True
    assert fresh.sound_on_complete is True
    assert fresh.sound_file == "/tmp/ding.wav"
    assert fresh.script_on_complete == "/usr/bin/notify-send done"
    assert fresh.after_queue_action == "hibernate"

    settings.download_days = []  # can't configure "never download"
    assert Settings(db).download_days == (0, 1, 2, 3, 4, 5, 6)
    with pytest.raises(ValueError):
        settings.after_queue_action = "explode"


# ------------------------------------------------------------------ gating


def test_weekday_restriction_gates_downloads(db: Database):
    manager = DownloadManager(db, max_concurrent=0)
    try:
        today = datetime.now().weekday()
        manager.settings.download_days = [d for d in range(7) if d != today]
        assert manager.downloads_allowed_now() is False
        manager.settings.download_days = [today]
        assert manager.downloads_allowed_now() is True
    finally:
        manager.shutdown()


def test_battery_mode_gates_downloads(db: Database, monkeypatch: pytest.MonkeyPatch):
    manager = DownloadManager(db, max_concurrent=0)
    try:
        manager.settings.pause_on_battery = True
        monkeypatch.setattr("app.core.manager.power.on_battery", lambda: True)
        assert manager.downloads_allowed_now() is False
        monkeypatch.setattr("app.core.manager.power.on_battery", lambda: False)
        assert manager.downloads_allowed_now() is True
    finally:
        manager.shutdown()


def _await_net_probe(manager: DownloadManager, expected: bool) -> None:
    """Kick the (asynchronous) connectivity probe and wait for its verdict.

    The probe runs off-thread on purpose - inline it froze the UI for seconds,
    since the scheduler holds the manager lock across a pass - so the answer
    lands a moment after the call that triggers it.
    """
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        manager._net_checked = 0.0  # due for a fresh probe
        manager.downloads_allowed_now()
        probe = manager._net_probe
        if probe is not None:
            probe.join(timeout=5.0)
        if manager._net_ok is expected:
            return
    raise AssertionError(f"connectivity probe never reported online={expected}")


def test_wait_for_network_gates_and_fast_retries(db: Database, monkeypatch: pytest.MonkeyPatch):
    manager = DownloadManager(db, max_concurrent=0)
    try:
        manager.settings.wait_for_network = True
        monkeypatch.setattr("app.core.manager.connectivity.is_online", lambda: False)
        _await_net_probe(manager, expected=False)
        assert manager.downloads_allowed_now() is False

        # A failed job waiting out a long backoff...
        manager._retry_at[42] = time.monotonic() + 300
        # ...retries immediately once the internet returns.
        monkeypatch.setattr("app.core.manager.connectivity.is_online", lambda: True)
        _await_net_probe(manager, expected=True)
        assert manager.downloads_allowed_now() is True
        # The reconnect zeroes the backoff; the live scheduler thread may then
        # consume the (stale) entry at any moment - both outcomes mean "retry
        # now", so never assert the key still exists (that raced in CI).
        assert manager._retry_at.get(42, 0.0) == 0.0
    finally:
        manager.shutdown()


def test_start_at_holds_until_the_chosen_time(db: Database, dest: Path):
    manager = DownloadManager(db, max_concurrent=0)
    try:
        job = manager.add_url("http://x.test/later.bin", dest_dir=dest)
        manager.set_job_start_at(job.id, datetime.now() + timedelta(hours=2))
        assert manager._next_queued() is None  # held: its moment hasn't come

        manager.set_job_start_at(job.id, datetime.now() - timedelta(minutes=1))
        picked = manager._next_queued()
        assert picked is not None and picked.id == job.id  # due -> runs

        manager.set_job_start_at(job.id, None)  # clearing also releases it
        picked = manager._next_queued()
        assert picked is not None and picked.id == job.id
    finally:
        manager.shutdown()
