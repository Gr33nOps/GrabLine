"""Best-effort update check against the GitHub releases API.

Never raises and never blocks the UI (call it from a thread). If the repo is
private or offline, it simply returns None and nothing happens.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, BinaryIO

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

#: Parallel Range fetch for the big AppImage / Setup / DMG assets. One
#: connection on a 150MB AppImage felt "stuck at 0%" for a long time; eight
#: connections cut wall time sharply on typical links.
_PARALLEL_CONNECTIONS = 8
_PARALLEL_MIN_BYTES = 4 * 1024 * 1024
_CHUNK = 256 * 1024
_PROGRESS_EVERY = 256 * 1024  # emit at most once per this many new bytes


ProgressCallback = Callable[[int, int | None], None]


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
        # Prefer the Inno Setup installer; also accept the portable zip when
        # a release ships only that (or Setup failed to build).
        return (lowered.endswith(".exe") and "setup" in lowered) or (
            "windows" in lowered and lowered.endswith(".zip")
        )
    if platform == "darwin":
        return lowered.endswith(".dmg")
    # Prefer the AppImage; fall back to the .deb so a Linux user still gets an
    # installer when the AppImage leg of a release was the one that failed.
    return lowered.endswith(".appimage") or (
        lowered.startswith("grabline_") and lowered.endswith("_amd64.deb")
    )


def _asset_rank(name: str, platform: str) -> int:
    """Lower is better. Prefer the primary installer over fallback packages."""
    lowered = name.lower()
    if platform.startswith("win"):
        if lowered.endswith(".exe") and "setup" in lowered:
            return 0
        return 1
    if platform == "darwin":
        return 0
    if lowered.endswith(".appimage"):
        return 0
    return 1


def installer_update(
    proxy: str | None = None, platform: str | None = None
) -> tuple[str, str, str, int] | None:
    """(tag, asset name, download URL, size) of this platform's installer for a
    newer release, or None when already up to date / no matching asset.

    ``size`` is the GitHub asset byte length (0 when unknown). Network failures
    raise :class:`DownloadError` so the UI's failed handler runs (instead of
    the 'you have the latest version' notice)."""
    import sys

    platform = platform or sys.platform
    data = _fetch_latest(proxy)
    tag = str(data.get("tag_name") or "").strip()
    if not tag or not is_newer(tag, __version__):
        return None
    assets = [a for a in (data.get("assets") or []) if isinstance(a, dict)]
    ranked = sorted(
        assets,
        key=lambda asset: _asset_rank(str(asset.get("name") or ""), platform),
    )
    for asset in ranked:
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if name and url and _asset_matches(name, platform):
            raw_size = asset.get("size")
            size = int(raw_size) if isinstance(raw_size, int) and raw_size > 0 else 0
            return (tag, name, url, size)
    # Newer tag exists but this platform's installer is missing (e.g. the
    # macOS job failed) - still an error the user should see, not "up to date".
    raise DownloadError(f"GrabLine {tag} is out, but no installer for this system is attached yet")


def _ua() -> dict[str, str]:
    return {"User-Agent": _API_HEADERS["User-Agent"]}


def _emit(
    progress: ProgressCallback | None,
    received: int,
    total: int | None,
    *,
    force: bool = False,
    last: list[int] | None = None,
) -> None:
    """Throttle progress callbacks so the GUI isn't flooded every chunk."""
    if not callable(progress):
        return
    if last is not None and not force and received - last[0] < _PROGRESS_EVERY:
        return
    if last is not None:
        last[0] = received
    progress(received, total)


def _download_single(
    client: httpx.Client,
    url: str,
    target: Path,
    total_hint: int | None,
    progress: ProgressCallback | None,
    cancel: threading.Event | None,
) -> None:
    with client.stream("GET", url, headers=_ua()) as response:
        response.raise_for_status()
        total_raw = response.headers.get("Content-Length")
        total = int(total_raw) if total_raw and total_raw.isdigit() else total_hint
        received = 0
        last = [0]
        with open(target, "wb") as handle:
            for chunk in response.iter_bytes(_CHUNK):
                if cancel is not None and cancel.is_set():
                    raise UpdateCancelled("update download cancelled")
                handle.write(chunk)
                received += len(chunk)
                _emit(progress, received, total, last=last)
        _emit(progress, received, total, force=True, last=last)


def _plan_spans(total: int, connections: int) -> list[tuple[int, int]]:
    count = max(1, min(connections, max(1, total // (512 * 1024))))
    base, extra = divmod(total, count)
    spans: list[tuple[int, int]] = []
    offset = 0
    for index in range(count):
        length = base + (1 if index < extra else 0)
        spans.append((offset, offset + length - 1))
        offset += length
    return spans


def _fetch_span(
    client: httpx.Client,
    url: str,
    start: int,
    end: int,
    handle: BinaryIO,
    lock: threading.Lock,
    received_box: list[int],
    total: int,
    progress: ProgressCallback | None,
    last: list[int],
    cancel: threading.Event | None,
    stop: threading.Event,
) -> None:
    headers = {**_ua(), "Range": f"bytes={start}-{end}"}
    with client.stream("GET", url, headers=headers) as response:
        # Parallel fetch requires real partial content. A 200 means the server
        # ignored Range - fall back to a single stream rather than writing the
        # whole body at a random offset.
        if response.status_code != 206:
            raise DownloadError(f"installer range HTTP {response.status_code}")
        offset = start
        for chunk in response.iter_bytes(_CHUNK):
            if stop.is_set() or (cancel is not None and cancel.is_set()):
                raise UpdateCancelled("update download cancelled")
            with lock:
                handle.seek(offset)
                handle.write(chunk)
                received_box[0] += len(chunk)
                current = received_box[0]
            offset += len(chunk)
            _emit(progress, current, total, last=last)
    if offset != end + 1:
        raise DownloadError(
            f"installer range short read: got {offset - start} want {end - start + 1}"
        )


def _download_parallel(
    client: httpx.Client,
    url: str,
    target: Path,
    total: int,
    progress: ProgressCallback | None,
    cancel: threading.Event | None,
) -> None:
    spans = _plan_spans(total, _PARALLEL_CONNECTIONS)
    with open(target, "wb") as handle:
        handle.truncate(total)

    received_box = [0]
    last = [0]
    lock = threading.Lock()
    stop = threading.Event()
    _emit(progress, 0, total, force=True, last=last)

    def work(span: tuple[int, int]) -> None:
        start, end = span
        with open(target, "rb+") as part:
            _fetch_span(
                client,
                url,
                start,
                end,
                part,
                lock,
                received_box,
                total,
                progress,
                last,
                cancel,
                stop,
            )

    with ThreadPoolExecutor(max_workers=len(spans), thread_name_prefix="gl-upd") as pool:
        futures = [pool.submit(work, span) for span in spans]
        try:
            for future in as_completed(futures):
                future.result()
        except Exception as exc:
            stop.set()
            if cancel is not None and cancel.is_set():
                raise UpdateCancelled("update download cancelled") from exc
            raise
    _emit(progress, total, total, force=True, last=last)


def download_installer(
    url: str,
    dest_dir: str,
    filename: str,
    proxy: str | None = None,
    progress: object = None,
    cancel: threading.Event | None = None,
    expected_size: int | None = None,
) -> str:
    """Stream the installer to ``dest_dir`` and return its path. ``progress`` is
    an optional callable(received_bytes, total_bytes_or_None).

    Large assets that support HTTP Range are fetched on several connections so
    a 100MB+ AppImage/Setup does not crawl on one pipe. Pass a ``cancel`` event
    to make the download interruptible: it is checked once per chunk, and when
    set the partial file is removed and :class:`UpdateCancelled` is raised.
    """
    progress_cb: ProgressCallback | None = progress if callable(progress) else None
    target = Path(dest_dir) / filename
    hint = expected_size if expected_size and expected_size > 0 else None
    last_error: Exception | None = None

    for attempt in range(2):
        if cancel is not None and cancel.is_set():
            target.unlink(missing_ok=True)
            raise UpdateCancelled("update download cancelled")
        try:
            limits = httpx.Limits(
                max_connections=_PARALLEL_CONNECTIONS + 2,
                max_keepalive_connections=_PARALLEL_CONNECTIONS + 2,
            )
            with net.build_client(
                proxy=proxy,
                follow_redirects=True,
                timeout=_DOWNLOAD_TIMEOUT,
                limits=limits,
            ) as client:
                if hint is not None and hint >= _PARALLEL_MIN_BYTES:
                    total = hint
                    try:
                        _download_parallel(client, url, target, total, progress_cb, cancel)
                    except UpdateCancelled:
                        raise
                    except DownloadError as exc:
                        # CDN ignored Range or short-read - one stream still works.
                        log.debug("parallel installer fetch fell back: %s", exc)
                        target.unlink(missing_ok=True)
                        _download_single(client, url, target, total, progress_cb, cancel)
                else:
                    _download_single(client, url, target, hint, progress_cb, cancel)
            return str(target)
        except UpdateCancelled:
            target.unlink(missing_ok=True)
            raise
        except (httpx.HTTPError, OSError, DownloadError) as exc:
            last_error = exc
            log.debug("installer download attempt %s failed: %s", attempt + 1, exc)
            target.unlink(missing_ok=True)
            if cancel is not None and cancel.is_set():
                raise UpdateCancelled("update download cancelled") from exc
    raise DownloadError(f"could not download update: {last_error}") from last_error
