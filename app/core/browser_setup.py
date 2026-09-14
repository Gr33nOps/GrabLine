"""Browser integration setup for the Setup wizard.

Copies the repo's extension folder to a fixed, writable location under the
data dir so a "Load unpacked" pick keeps working even if the checkout moves,
and reports which browsers are installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from app.core import paths, proc

#: Public store listing for each browser family, where the user clicks a single
#: "Add" to install. No app can install an extension silently - a browser will
#: only add one from its own store (one click) or via a manual developer load.
#: Fill CHROME_WEBSTORE_URL once the extension is published there.
AMO_LISTING_URL = "https://addons.mozilla.org/firefox/addon/grabline-connect/"
CHROME_WEBSTORE_URL: str | None = None

#: How each browser can install GrabLine Connect for free.
#: "auto"  - a free store / signed add-on exists, so it can be one click.
#: "unpacked" - needs the Chrome Web Store ($5) for auto; free path is
#:             Developer mode -> Load unpacked (permanent, one manual step).
BROWSERS: tuple[tuple[str, str, str], ...] = (
    ("Firefox", "firefox", "auto"),
    ("Microsoft Edge", "chromium", "auto"),
    ("Chrome", "chromium", "unpacked"),
    ("Brave", "chromium", "unpacked"),
    ("Vivaldi", "chromium", "unpacked"),
    ("Opera", "chromium", "unpacked"),
    ("Arc", "chromium", "unpacked"),
    ("Chromium", "chromium", "unpacked"),
)


@dataclass(frozen=True)
class BrowserStep:
    name: str
    kind: str  # "chromium" | "firefox"
    method: str  # "auto" | "unpacked"
    installed: bool


def _source_extension_dir() -> Path:
    """Where the extension files ship. In a frozen (installed) build they're
    bundled inside the app via PyInstaller datas, unpacked to ``sys._MEIPASS``;
    from source it's the repo's ``extension/`` next to the app package."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", None) or Path(sys.executable).parent
        return Path(base) / "extension"
    return Path(paths.__file__).resolve().parents[2] / "extension"


def stable_extension_dir() -> Path:
    """The fixed location the browser loads the extension from."""
    return paths.data_dir() / "browser-extension"


def install_extension_files() -> Path:
    """Copy the extension to the stable path and return it. Overwrites any
    previous copy so a git pull refreshes it."""
    source = _source_extension_dir()
    manifest = source / "manifest.json"
    if not manifest.is_file():
        raise FileNotFoundError(f"extension folder not found at {source}")
    target = stable_extension_dir()
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(source, target)
    return target


def _classify_browser(identifier: str) -> tuple[str, str] | None:
    """Map an OS browser identifier (desktop file, ProgId, bundle id) onto a
    (family, display name), where family is 'firefox' or 'chromium'. Order
    matters: Brave/Edge strings also contain 'chrome'-ish substrings."""
    ident = identifier.lower()
    if "firefox" in ident or "mozilla" in ident:
        return ("firefox", "Firefox")
    if "brave" in ident:
        return ("chromium", "Brave")
    if "edge" in ident or "msedge" in ident:
        return ("chromium", "Microsoft Edge")
    if "vivaldi" in ident:
        return ("chromium", "Vivaldi")
    # Arc's bundle/app id is company.thebrowser.Browser; a bare "arc" substring
    # would false-match ids like "search", so require the company marker.
    if "thebrowser" in ident:
        return ("chromium", "Arc")
    if "chromium" in ident:
        return ("chromium", "Chromium")
    if "chrome" in ident:
        return ("chromium", "Chrome")
    if "opera" in ident:
        return ("chromium", "Opera")
    return None


def _linux_default_browser_id() -> str | None:
    result = subprocess.run(  # argument list only - no shell (S1)
        ["xdg-settings", "get", "default-web-browser"],
        capture_output=True,
        text=True,
        timeout=5,
        env=proc.clean_env(),  # system libs, not the frozen app's bundled ones
    )
    return result.stdout.strip() or None if result.returncode == 0 else None


def _windows_default_browser_id() -> str | None:  # pragma: no cover - windows-only
    if sys.platform == "win32":
        import winreg

        key = r"SOFTWARE\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            prog_id, _ = winreg.QueryValueEx(handle, "ProgId")
        return str(prog_id)
    return None


def _darwin_default_browser_id(home: Path) -> str | None:  # pragma: no cover - macos-only
    plist = (
        home
        / "Library/Preferences/com.apple.LaunchServices"
        / "com.apple.launchservices.secure.plist"
    )
    result = subprocess.run(
        ["plutil", "-convert", "json", "-o", "-", str(plist)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        return None
    for handler in json.loads(result.stdout).get("LSHandlers", []):
        if handler.get("LSHandlerURLScheme") == "https":
            value = handler.get("LSHandlerRoleAll") or handler.get("LSHandlerRoleViewer")
            return str(value) if value else None
    return None


def default_browser(
    platform: str | None = None, home: Path | None = None
) -> tuple[str, str] | None:
    """The OS default web browser as (family, display name), or None if it
    can't be determined - family is 'firefox' or 'chromium'. Used to point the
    'Add the extension' button at the right store."""
    platform = platform or sys.platform
    home = home or Path.home()
    try:
        if platform == "win32":
            identifier = _windows_default_browser_id()
        elif platform == "darwin":
            identifier = _darwin_default_browser_id(home)
        else:
            identifier = _linux_default_browser_id()
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return _classify_browser(identifier) if identifier else None


def extension_install_url(platform: str | None = None, home: Path | None = None) -> str | None:
    """The one-click store install page for the default browser, or None when
    there isn't one yet (then the wizard's manual 'Load unpacked' path applies)."""
    browser = default_browser(platform, home)
    if browser is None:
        return None
    family = browser[0]
    if family == "firefox":
        return AMO_LISTING_URL
    if family == "chromium":
        return CHROME_WEBSTORE_URL
    return None


def _chromium_root(name: str, home: Path, platform: str) -> Path | None:
    support = home / "Library" / "Application Support"
    roots = {
        "linux": {
            "Chrome": home / ".config" / "google-chrome",
            "Chromium": home / ".config" / "chromium",
            "Microsoft Edge": home / ".config" / "microsoft-edge",
            "Brave": home / ".config" / "BraveSoftware" / "Brave-Browser",
            "Vivaldi": home / ".config" / "vivaldi",
            "Opera": home / ".config" / "opera",
            "Firefox": home / ".mozilla",
        },
        "darwin": {
            "Chrome": support / "Google" / "Chrome",
            "Chromium": support / "Chromium",
            "Microsoft Edge": support / "Microsoft Edge",
            "Brave": support / "BraveSoftware" / "Brave-Browser",
            "Vivaldi": support / "Vivaldi",
            "Opera": support / "com.operasoftware.Opera",
            "Arc": support / "Arc",
            "Firefox": support / "Firefox",
        },
    }
    return roots.get(platform, {}).get(name)


def _cookie_roots(platform: str, home: Path) -> dict[str, Path]:
    """Browser key (as yt-dlp / SESSION_BROWSERS names it) -> the profile
    root that only exists once that browser has actually been set up here."""
    if platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        roaming = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        return {
            "firefox": roaming / "Mozilla" / "Firefox" / "Profiles",
            "chrome": local / "Google" / "Chrome" / "User Data",
            "brave": local / "BraveSoftware" / "Brave-Browser" / "User Data",
            "chromium": local / "Chromium" / "User Data",
            "opera": roaming / "Opera Software" / "Opera Stable",
            "edge": local / "Microsoft" / "Edge" / "User Data",
        }
    if platform == "darwin":
        support = home / "Library" / "Application Support"
        return {
            "firefox": support / "Firefox" / "Profiles",
            "chrome": support / "Google" / "Chrome",
            "brave": support / "BraveSoftware" / "Brave-Browser",
            "chromium": support / "Chromium",
            "opera": support / "com.operasoftware.Opera",
            "edge": support / "Microsoft Edge",
        }
    config = home / ".config"
    return {
        "firefox": home / ".mozilla" / "firefox",
        "chrome": config / "google-chrome",
        "brave": config / "BraveSoftware" / "Brave-Browser",
        "chromium": config / "chromium",
        "opera": config / "opera",
        "edge": config / "microsoft-edge",
    }


#: Preference when several browsers are present. Firefox first: an existing
#: Firefox profile is a strong signal the person actually uses it, whereas
#: Edge ships with Windows and is usually present but unused.
_COOKIE_BROWSER_PREFERENCE = ("firefox", "chrome", "brave", "chromium", "opera", "edge")


def detect_cookie_browser(platform: str | None = None, home: Path | None = None) -> str | None:
    """The best browser to read a login session from: the first, by active-use
    preference, whose profile directory exists. None when nothing is found, so
    callers keep their own fallback. Used to seed the 'Browser session' choice
    so it points at the browser the person is signed into, not a hardcoded one."""
    platform = platform or sys.platform
    home = home or Path.home()
    roots = _cookie_roots(platform, home)
    for key in _COOKIE_BROWSER_PREFERENCE:
        root = roots.get(key)
        if root is not None and root.exists():
            return key
    return None


def cookie_browser_candidates(
    *, primary: str | None = None, platform: str | None = None, home: Path | None = None
) -> list[str]:
    """Installed browsers to try for cookie reads, ``primary`` first when set.

    Used when the first profile's cookie database is locked or missing - try the
    next one silently instead of asking the person about cookies.txt.
    """
    platform = platform or sys.platform
    home = home or Path.home()
    roots = _cookie_roots(platform, home)
    ordered: list[str] = []
    for key in ((primary,) if primary else ()) + _COOKIE_BROWSER_PREFERENCE:
        if not key or key in ordered:
            continue
        root = roots.get(key)
        if root is not None and root.exists():
            ordered.append(key)
    return ordered


def detect_browsers(platform: str | None = None, home: Path | None = None) -> list[BrowserStep]:
    """The browsers to show in the wizard, with a best-effort 'installed' flag.

    On Windows detection is unreliable (per-user vs machine, registry), so
    every browser is shown as available there.
    """
    platform = platform or sys.platform
    home = home or Path.home()
    steps: list[BrowserStep] = []
    for name, kind, method in BROWSERS:
        if platform == "win32":
            installed = True
        else:
            root = _chromium_root(name, home, platform)
            installed = root is not None and root.exists()
        steps.append(BrowserStep(name=name, kind=kind, method=method, installed=installed))
    return steps
