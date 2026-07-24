"""Best-effort update check against the GitHub releases API.

Never raises and never blocks the UI (call it from a thread). If the repo is
private or offline, it simply returns None and nothing happens.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

import httpx

from app import __version__
from app.core import net
from app.core.errors import DownloadError


class UpdateCancelled(DownloadError):
    """The user cancelled an in-progress installer download. Subclasses
    DownloadError so the UI's file-op thread delivers it like any other
    failure, and the caller tells a cancel apart from a real error."""


log = logging.getLogger(__name__)

_LATEST = "https://api.github.com/repos/Gr33nOps/GrabLine/releases/latest"
_NUM = re.compile(r"\d+")

#: GitHub rejects / stalls anonymous clients that omit a User-Agent. Identify
#: ourselves so the check returns in seconds instead of hanging then failing.
_API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": f"GrabLine/{__version__}",
    "X-GitHub-Api-Version": "2022-11-28",
}

#: Tight connect, modest read: the releases JSON is tiny. A single long
#: timeout was making "Check for updates" sit on a black-holed route for the
#: full window before giving up.
_API_TIMEOUT = httpx.Timeout(connect=5.0, read=12.0, write=12.0, pool=5.0)

#: Installer downloads are tens of MB; allow the body to trickle without
#: treating a brief stall as death, but fail a dead connect quickly.
_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)


def _parts(version: str) -> tuple[int, ...]:
    return tuple(int(n) for n in _NUM.findall(version))


def is_newer(candidate: str, current: str) -> bool:
    """True if ``candidate`` is a strictly higher version than ``current``."""
    return _parts(candidate) > _parts(current)


def _fetch_latest(proxy: str | None = None) -> dict[str, Any]:
    """GET /releases/latest once, with one retry on a transient network blip.

    Raises :class:`DownloadError` when GitHub can't be reached so the UI can
    say "could not check" instead of lying that you're already up to date.
    """
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            with net.build_client(
                proxy=proxy, follow_redirects=True, timeout=_API_TIMEOUT
            ) as client:
                response = client.get(_LATEST, headers=_API_HEADERS)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, dict):
                    return data
                raise DownloadError("unexpected reply from GitHub releases")
            raise DownloadError(f"GitHub releases returned HTTP {response.status_code}")
        except httpx.HTTPError as exc:
            last_error = exc
            log.debug("update check attempt %s failed: %s", attempt + 1, exc)
        except ValueError as exc:
            raise DownloadError(f"could not parse GitHub releases reply: {exc}") from exc
    raise DownloadError(f"could not reach GitHub releases: {last_error}")


def latest_release(proxy: str | None = None) -> tuple[str, str] | None:
    """Return (tag, html_url) of the latest release, or None if unavailable."""
    try:
        data = _fetch_latest(proxy)
    except DownloadError as exc:
        log.debug("update check failed: %s", exc)
        return None
    tag = str(data.get("tag_name") or "").strip()
    url = str(data.get("html_url") or "").strip()
    return (tag, url) if tag else None


def check_for_update(proxy: str | None = None) -> tuple[str, str] | None:
    """Return (tag, url) when a newer release exists, else None."""
    latest = latest_release(proxy)
    if latest is None:
        return None
    tag, url = latest
    return (tag, url) if is_newer(tag, __version__) else None


#: Where the "Download update" fallback points: the website's download
#: section, which always links the current installers.
WEBSITE_DOWNLOAD_URL = "https://gr33nops.github.io/GrabLine/#download"


def _asset_matches(name: str, platform: str) -> bool:
    lowered = name.lower()
    if platform.startswith("win"):
        return lowered.endswith(".exe") and "setup" in lowered
    if platform == "darwin":
        return lowered.endswith(".dmg")
    # Prefer the AppImage; fall back to the .deb so a Linux user still gets an
    # installer when the AppImage leg of a release was the one that failed.
    return lowered.endswith(".appimage") or (
        lowered.startswith("grabline_") and lowered.endswith("_amd64.deb")
    )


def installer_update(
    proxy: str | None = None, platform: str | None = None
) -> tuple[str, str, str] | None:
    """(tag, asset name, download URL) of this platform's installer for a
    newer release, or None when already up to date / no matching asset.

    Network failures raise :class:`DownloadError` so the UI's failed handler
    runs (instead of the 'you have the latest version' notice)."""
    import sys

    platform = platform or sys.platform
    data = _fetch_latest(proxy)
    tag = str(data.get("tag_name") or "").strip()
    if not tag or not is_newer(tag, __version__):
        return None
    assets = [a for a in (data.get("assets") or []) if isinstance(a, dict)]
    # Prefer AppImage over .deb when both match on Linux.
    ranked = sorted(
        assets,
        key=lambda asset: 0 if str(asset.get("name") or "").lower().endswith(".appimage") else 1,
    )
    for asset in ranked:
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if name and url and _asset_matches(name, platform):
            return (tag, name, url)
    # Newer tag exists but this platform's installer is missing (e.g. the
    # macOS job failed) - still an error the user should see, not "up to date".
    raise DownloadError(f"GrabLine {tag} is out, but no installer for this system is attached yet")


def download_installer(
    url: str,
    dest_dir: str,
    filename: str,
    proxy: str | None = None,
    progress: object = None,
    cancel: threading.Event | None = None,
) -> str:
    """Stream the installer to ``dest_dir`` and return its path. ``progress`` is
    an optional callable(received_bytes, total_bytes_or_None).

    Pass a ``cancel`` event to make the download interruptible: it is checked
    once per chunk, and when set the partial file is removed and
    :class:`UpdateCancelled` is raised, so pressing Cancel actually stops the
    transfer instead of leaving it running and opening a half-written installer.
    """
    from pathlib import Path

    target = Path(dest_dir) / filename
    cancelled = False
    # One automatic retry: mid-download drops ("failed midway") are common on
    # flaky links, and re-fetching from byte 0 is simpler than Range resume for
    # GitHub CDN URLs that sometimes ignore it.
    last_error: Exception | None = None
    for attempt in range(2):
        cancelled = False
        try:
            with (
                net.build_client(
                    proxy=proxy, follow_redirects=True, timeout=_DOWNLOAD_TIMEOUT
                ) as client,
                client.stream(
                    "GET", url, headers={"User-Agent": _API_HEADERS["User-Agent"]}
                ) as response,
            ):
                response.raise_for_status()
                total_raw = response.headers.get("Content-Length")
                total = int(total_raw) if total_raw and total_raw.isdigit() else None
                received = 0
                with open(target, "wb") as handle:
                    for chunk in response.iter_bytes(65536):
                        if cancel is not None and cancel.is_set():
                            cancelled = True
                            break
                        handle.write(chunk)
                        received += len(chunk)
                        if callable(progress):
                            progress(received, total)
            if cancelled:
                break
            return str(target)
        except (httpx.HTTPError, OSError) as exc:
            last_error = exc
            log.debug("installer download attempt %s failed: %s", attempt + 1, exc)
            target.unlink(missing_ok=True)
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
    if cancelled:
        target.unlink(missing_ok=True)
        raise UpdateCancelled("update download cancelled")
    if last_error is not None:
        raise DownloadError(f"could not download update: {last_error}") from last_error
    raise DownloadError("could not download update")
