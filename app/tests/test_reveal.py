"""Opening a download's folder in the OS file manager (never the browser, never
a hidden window)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.core import reveal


def _only(tool: str):
    """A shutil.which stand-in that finds a single named tool at /usr/bin."""
    return lambda name: f"/usr/bin/{name}" if name == tool else None


def test_linux_uses_a_plain_path_not_a_file_url():
    # The bug: a file:// URL routed through x-scheme-handler/file, whose default
    # handler is the web browser. A plain directory path resolves as
    # inode/directory instead, so the file manager opens.
    command = reveal.unix_command(Path("/home/u/Downloads"), "linux", which=_only("xdg-open"))
    assert command == ["/usr/bin/xdg-open", "/home/u/Downloads"]
    assert not any("file://" in part for part in command)


def test_linux_falls_back_to_gio_with_its_open_subcommand():
    command = reveal.unix_command(Path("/data/clips"), "linux", which=_only("gio"))
    assert command == ["/usr/bin/gio", "open", "/data/clips"]


def test_linux_returns_none_when_no_opener_is_installed():
    assert reveal.unix_command(Path("/x"), "linux", which=lambda name: None) is None


def test_macos_opens_the_folder_or_reveals_the_file():
    assert reveal.unix_command(Path("/Users/me/Downloads"), "darwin") == [
        "open",
        "/Users/me/Downloads",
    ]
    revealed = reveal.unix_command(
        Path("/Users/me/Downloads"), "darwin", reveal=Path("/Users/me/Downloads/clip.mp4")
    )
    assert revealed == ["open", "-R", "/Users/me/Downloads/clip.mp4"]


def test_open_folder_reveals_the_file_when_it_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    launched: list[list[str]] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", _only("xdg-open"))
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kw: launched.append(command))
    a_file = tmp_path / "video.mp4"
    a_file.write_bytes(b"x")

    assert reveal.open_folder(a_file) is True
    # On Linux the folder opens (xdg-open can't select), never the file itself.
    assert launched == [["/usr/bin/xdg-open", str(tmp_path)]]


def test_open_folder_of_a_missing_file_opens_its_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    launched: list[list[str]] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", _only("xdg-open"))
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kw: launched.append(command))
    # A failed download: the file was never written, so the folder opens.
    assert reveal.open_folder(tmp_path / "never-made.mp4") is True
    assert launched == [["/usr/bin/xdg-open", str(tmp_path)]]


def test_windows_startfile_carries_no_hidden_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # The Windows bug: launching explorer with the FFmpeg console-hiding
    # startupinfo opened the folder window hidden. os.startfile never does.
    started: list[str] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "startfile", lambda p: started.append(p), raising=False)
    assert reveal.open_folder(tmp_path) is True
    assert started == [str(tmp_path)]


def test_windows_selects_the_file_with_explorer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    launched: list[list[str]] = []
    a_file = tmp_path / "video.mp4"
    a_file.write_bytes(b"x")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kw: launched.append(command))
    assert reveal.open_folder(a_file) is True
    assert launched == [["explorer", "/select,", str(a_file)]]


def test_open_folder_reports_failure_when_no_manager_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert reveal.open_folder(tmp_path) is False
