"""Browser Setup core: staging the extension to a stable path, detection."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.core import browser_setup


@pytest.fixture(autouse=True)
def isolated_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))


def test_source_extension_has_a_manifest():
    manifest = browser_setup._source_extension_dir() / "manifest.json"
    assert manifest.is_file(), "the extension must ship with the app"


def test_install_extension_files_stages_a_usable_copy():
    target = browser_setup.install_extension_files()
    assert target == browser_setup.stable_extension_dir()
    assert (target / "manifest.json").is_file()
    assert (target / "background.js").is_file()
    assert (target / "content").is_dir()


def test_install_extension_files_refreshes_on_reinstall():
    first = browser_setup.install_extension_files()
    stale = first / "stale-leftover.txt"
    stale.write_text("old")
    browser_setup.install_extension_files()  # a second run (app update)
    assert not stale.exists()  # stale files are cleared
    assert (first / "manifest.json").is_file()


@pytest.mark.skipif(sys.platform == "win32", reason="posix browser roots")
def test_detect_browsers_marks_installed(tmp_path: Path):
    home = tmp_path / "home"
    (home / ".config" / "google-chrome").mkdir(parents=True)
    (home / ".mozilla").mkdir(parents=True)
    steps = {s.name: s for s in browser_setup.detect_browsers("linux", home=home)}
    assert steps["Chrome"].installed is True
    assert steps["Firefox"].installed is True
    assert steps["Brave"].installed is False
    # Firefox and Edge are the free-auto path; Chrome/Brave are load-unpacked.
    assert steps["Firefox"].method == "auto"
    assert steps["Microsoft Edge"].method == "auto"
    assert steps["Chrome"].method == "unpacked"


def test_detect_cookie_browser_prefers_firefox_over_edge(tmp_path: Path):
    home = tmp_path / "home"
    (home / ".mozilla" / "firefox").mkdir(parents=True)
    (home / ".config" / "microsoft-edge").mkdir(parents=True)  # present but unused
    assert browser_setup.detect_cookie_browser("linux", home=home) == "firefox"


def test_detect_cookie_browser_falls_back_to_installed(tmp_path: Path):
    home = tmp_path / "home"
    (home / ".config" / "google-chrome").mkdir(parents=True)
    assert browser_setup.detect_cookie_browser("linux", home=home) == "chrome"


def test_detect_cookie_browser_none_when_nothing_present(tmp_path: Path):
    assert browser_setup.detect_cookie_browser("linux", home=tmp_path / "empty") is None


def test_classify_browser_maps_families():
    classify = browser_setup._classify_browser
    assert classify("firefox.desktop") == ("firefox", "Firefox")
    assert classify("org.mozilla.firefox") == ("firefox", "Firefox")
    assert classify("brave-browser.desktop") == ("chromium", "Brave")
    assert classify("MSEdgeHTM") == ("chromium", "Microsoft Edge")
    assert classify("ChromeHTML") == ("chromium", "Chrome")
    assert classify("chromium.desktop") == ("chromium", "Chromium")
    assert classify("vivaldi-stable.desktop") == ("chromium", "Vivaldi")
    assert classify("company.thebrowser.Browser") == ("chromium", "Arc")
    assert classify("OperaStable") == ("chromium", "Opera")
    assert classify("something-else") is None
    assert classify("websearch-helper") is None  # 'arc' substring must not match


def test_default_browser_reads_linux_setting(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(browser_setup, "_linux_default_browser_id", lambda: "firefox.desktop")
    assert browser_setup.default_browser(platform="linux") == ("firefox", "Firefox")


def test_default_browser_none_when_tool_fails(monkeypatch: pytest.MonkeyPatch):
    def boom() -> str | None:
        raise FileNotFoundError("no xdg-settings")

    monkeypatch.setattr(browser_setup, "_linux_default_browser_id", boom)
    assert browser_setup.default_browser(platform="linux") is None


def test_extension_install_url_picks_the_store(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(browser_setup, "default_browser", lambda *a, **k: ("firefox", "Firefox"))
    assert browser_setup.extension_install_url() == browser_setup.AMO_LISTING_URL
    # Chromium has no live store URL yet -> None (manual load path applies).
    monkeypatch.setattr(browser_setup, "default_browser", lambda *a, **k: ("chromium", "Chrome"))
    assert browser_setup.extension_install_url() is None
    monkeypatch.setattr(browser_setup, "default_browser", lambda *a, **k: None)
    assert browser_setup.extension_install_url() is None
