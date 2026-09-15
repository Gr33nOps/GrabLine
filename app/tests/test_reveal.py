"""Opening a download's folder in the OS file manager (never the browser, never
a hidden window)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

from app.core import reveal


def _only(tool: str):
    """A shutil.which stand-in that finds a single named tool at /usr/bin."""
    return lambda name: f"/usr/bin/{name}" if name == tool else None


def test_linux_uses_a_plain_path_not_a_file_url():
    # The bug: a file:// URL routed through x-scheme-handler/file, whose default
    # handler is the web browser. A plain directory path resolves as
    # inode/directory instead, so the file manager opens.
    command = reveal.unix_command(
        PurePosixPath("/home/u/Downloads"), "linux", which=_only("xdg-open")
    )
    assert command == ["/usr/bin/xdg-open", "/home/u/Downloads"]
    assert not any("file://" in part for part in command)


def test_linux_falls_back_to_gio_with_its_open_subcommand():
    command = reveal.unix_command(PurePosixPath("/data/clips"), "linux", which=_only("gio"))
    assert command == ["/usr/bin/gio", "open", "/data/clips"]


def test_linux_returns_none_when_no_opener_is_installed():
    assert reveal.unix_command(PurePosixPath("/x"), "linux", which=lambda name: None) is None


def test_macos_opens_the_folder_or_reveals_the_file():
    assert reveal.unix_command(PurePosixPath("/Users/me/Downloads"), "darwin") == [
        "open",
        "/Users/me/Downloads",
    ]
    revealed = reveal.unix_command(
        PurePosixPath("/Users/me/Downloads"),
        "darwin",
        reveal=PurePosixPath("/Users/me/Downloads/clip.mp4"),
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


# --------------------------------------------------- Linux manager selection
#
# The reported bug: "Open in Folder" opened Brave and showed the folder as a
# web page. xdg-open was tried first, and on a desktop where a browser has
# claimed inode/directory that is exactly what happens. These pin the fix:
# a real file manager is always preferred over any generic URL handler.


def _installed(*tools: str):
    """A shutil.which stand-in where exactly ``tools`` exist at /usr/bin."""
    available = set(tools)
    return lambda name: f"/usr/bin/{name}" if name in available else None


def _desktop(name: str) -> dict[str, str]:
    return {"XDG_CURRENT_DESKTOP": name}


@pytest.mark.parametrize(
    ("desktop", "manager"),
    [
        ("X-Cinnamon", "nemo"),  # Linux Mint / Cinnamon
        ("Cinnamon", "nemo"),
        ("KDE", "dolphin"),
        ("plasma", "dolphin"),
        ("GNOME", "nautilus"),
        ("ubuntu:GNOME", "nautilus"),  # colon-separated, as Ubuntu sets it
        ("XFCE", "thunar"),
        ("MATE", "caja"),
        ("LXQt", "pcmanfm-qt"),
        ("LXDE", "pcmanfm"),
        ("Deepin", "dde-file-manager"),
        ("Pantheon", "io.elementary.files"),
    ],
)
def test_each_desktop_gets_its_own_file_manager(desktop: str, manager: str):
    # Every manager is installed *and* so is xdg-open: the desktop's own must
    # still win, and xdg-open must not be chosen.
    command = reveal.unix_command(
        PurePosixPath("/home/u/Downloads"),
        "linux",
        which=_installed(*reveal._FILE_MANAGERS, "xdg-open", "gio"),
        environ=_desktop(desktop),
    )
    assert command is not None
    assert command[0] == f"/usr/bin/{manager}"
    assert "xdg-open" not in command[0] and "gio" not in command[0]


def test_desktop_session_is_read_when_xdg_current_desktop_is_missing():
    # Some session managers only set DESKTOP_SESSION, sometimes as a path.
    command = reveal.unix_command(
        PurePosixPath("/home/u/Downloads"),
        "linux",
        which=_installed("nemo", "nautilus", "xdg-open"),
        environ={"DESKTOP_SESSION": "/usr/share/xsessions/cinnamon"},
    )
    assert command == ["/usr/bin/nemo", "/home/u/Downloads"]


def test_falls_back_to_another_real_manager_when_the_desktops_own_is_missing():
    # KDE without Dolphin installed: still a real file manager, not xdg-open.
    command = reveal.unix_command(
        PurePosixPath("/home/u/Downloads"),
        "linux",
        which=_installed("thunar", "xdg-open", "gio"),
        environ=_desktop("KDE"),
    )
    assert command == ["/usr/bin/thunar", "/home/u/Downloads"]


def test_generic_opener_is_only_used_when_no_file_manager_exists():
    command = reveal.unix_command(
        PurePosixPath("/home/u/Downloads"),
        "linux",
        which=_installed("xdg-open", "gio"),
        environ=_desktop("KDE"),
    )
    # gio resolves inode/directory through GIO rather than the scheme-handler
    # chain, so it is the better of the two last resorts.
    assert command == ["/usr/bin/gio", "open", "/home/u/Downloads"]


def test_a_browser_is_never_chosen_as_the_file_manager():
    browsers = ("brave", "brave-browser", "firefox", "google-chrome", "chromium", "vivaldi")
    # A machine with browsers and one real file manager.
    command = reveal.unix_command(
        PurePosixPath("/home/u/Downloads"),
        "linux",
        which=_installed(*browsers, "nemo"),
        environ=_desktop("X-Cinnamon"),
    )
    assert command == ["/usr/bin/nemo", "/home/u/Downloads"]
    # And nothing browser-shaped is even a candidate.
    assert not set(browsers) & set(reveal._FILE_MANAGERS)
    assert not set(browsers) & set(reveal._GENERIC_OPENERS)
    for candidates in reveal._DESKTOP_PREFERENCE.values():
        assert not set(browsers) & set(candidates)


def test_managers_that_support_it_select_the_file():
    a_file = PurePosixPath("/home/u/Downloads/ubuntu.iso")
    nautilus = reveal.unix_command(
        a_file.parent,
        "linux",
        reveal=a_file,
        which=_installed("nautilus"),
        environ=_desktop("GNOME"),
    )
    assert nautilus == ["/usr/bin/nautilus", "--select", str(a_file)]
    dolphin = reveal.unix_command(
        a_file.parent, "linux", reveal=a_file, which=_installed("dolphin"), environ=_desktop("KDE")
    )
    assert dolphin == ["/usr/bin/dolphin", "--select", str(a_file)]


def test_managers_without_a_select_flag_open_the_directory_not_the_file():
    # Nemo, Thunar and Caja have no documented select flag. Handing them the
    # *file* would open it in its default application (a video player) rather
    # than showing it in its folder - so the directory is the right answer.
    a_file = PurePosixPath("/home/u/Downloads/clip.mp4")
    for desktop, manager in (("X-Cinnamon", "nemo"), ("XFCE", "thunar"), ("MATE", "caja")):
        command = reveal.unix_command(
            a_file.parent,
            "linux",
            reveal=a_file,
            which=_installed(manager),
            environ=_desktop(desktop),
        )
        assert command == [f"/usr/bin/{manager}", str(a_file.parent)]
        assert str(a_file) not in command


def test_an_unknown_desktop_still_finds_an_installed_manager():
    command = reveal.unix_command(
        PurePosixPath("/srv/files"),
        "linux",
        which=_installed("dolphin", "xdg-open"),
        environ={"XDG_CURRENT_DESKTOP": "some-wm-nobody-has-heard-of"},
    )
    assert command == ["/usr/bin/dolphin", "/srv/files"]


def test_no_desktop_environment_at_all_still_avoids_the_generic_opener():
    command = reveal.unix_command(
        PurePosixPath("/srv/files"), "linux", which=_installed("caja", "xdg-open"), environ={}
    )
    assert command == ["/usr/bin/caja", "/srv/files"]


def test_open_folder_prefers_the_real_manager_over_xdg_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """End to end through open_folder: the bug, reproduced and fixed. On a
    Cinnamon box with both Nemo and xdg-open installed, Nemo is launched."""
    launched: list[list[str]] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "X-Cinnamon")
    monkeypatch.setattr(shutil, "which", _installed("nemo", "xdg-open", "brave-browser"))
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kw: launched.append(command))
    a_file = tmp_path / "video.mp4"
    a_file.write_bytes(b"x")

    assert reveal.open_folder(a_file) is True
    assert launched == [["/usr/bin/nemo", str(tmp_path)]]


def test_open_folder_never_uses_shell_and_passes_an_argv_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    monkeypatch.setattr(shutil, "which", _installed("nautilus"))

    def _record(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr(subprocess, "Popen", _record)
    assert reveal.open_folder(tmp_path) is True
    (args, kwargs) = calls[0]
    assert isinstance(args[0], list)  # argv, never one shell string
    assert kwargs.get("shell") in (None, False)
