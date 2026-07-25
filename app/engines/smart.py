"""The Smart Engine (F0.3): yt-dlp in-process, behind the resolver.

Three responsibilities:
- ``SmartEngine.matches``: does one of yt-dlp's site extractors recognize this
  URL? (Offline check; the catch-all generic extractor doesn't count.)
- ``SmartEngine.inspect``: extract metadata without downloading and curate the
  format zoo into the short quality list the panel shows (Best/2160p/.../MP3).
- ``SmartDownload``: run one job through yt-dlp with progress mapped onto the
  queue, honoring pause/cancel, with MP3/M4A extraction (+ cover art & tags),
  subtitles, clip trimming (F0.7), and the opt-in browser session (F0.8).

yt-dlp is imported lazily so the CLI stays fast for plain direct downloads.
"""

from __future__ import annotations

import contextlib
import copy
import glob
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.core import naming, net
from app.core.errors import DownloadError
from app.core.i18n import N_, t
from app.core.models import Job, JobStatus
from app.db.database import Database

log = logging.getLogger(__name__)

#: yt-dlp's per-format filename tag, e.g. the `.f137` in `Clip.f137.mp4`.
_FORMAT_SUFFIX = re.compile(r"\.f[0-9a-zA-Z_-]+$")

#: The standard quality ladder shown in the panel, top first.
QUALITY_TIERS = (2160, 1440, 1080, 720, 480, 360)

#: Substring -> user-facing message for the yt-dlp error zoo (F1.8 groundwork).
_FRIENDLY_ERRORS: tuple[tuple[str, str], ...] = (
    (
        "This video is private",
        N_("This video is private. Its owner restricted access."),
    ),
    (
        "Private video",
        N_("This video is private. Its owner restricted access."),
    ),
    (
        "Sign in to confirm your age",
        N_(
            "This video is age-restricted. GrabLine tried your browser login "
            "automatically. To download it you need to be signed in to YouTube in "
            "your browser (Firefox by default) on an age-verified account. Sign in "
            "there, then try again."
        ),
    ),
    (
        "confirm you're not a bot",
        N_(
            "YouTube blocked this download. Sign in to YouTube in your browser "
            "(the one listed under Settings → Browser Integration), then try "
            "again. GrabLine uses that login automatically - you do not need a "
            "cookies file."
        ),
    ),
    (
        "Requested format is not available",
        N_(
            "YouTube didn't return a usable video format. This is usually "
            "temporary - try again shortly. If it only fails with 'Let GrabLine "
            "use my browser session' turned on, turn it off: on a PC without a "
            "JavaScript runtime, browser cookies can make YouTube hide the "
            "downloadable formats."
        ),
    ),
    (
        "n challenge",
        N_(
            "YouTube needs a JavaScript runtime to unlock this video's formats. "
            "GrabLine installs one (Deno) automatically; if this keeps happening, "
            "install Node.js and restart GrabLine."
        ),
    ),
    (
        "Only images are available",
        N_(
            "YouTube needs a JavaScript runtime to unlock this video's formats. "
            "GrabLine installs one (Deno) automatically; if this keeps happening, "
            "install Node.js and restart GrabLine."
        ),
    ),
    (
        "available in your country",
        N_("This video is region-blocked and not available from your location."),
    ),
    (
        "geo restricted",
        N_("This video is region-blocked and not available from your location."),
    ),
    (
        "This live event will begin",
        N_("This is a live stream that has not started yet. Try again once it is over."),
    ),
    (
        "is not a valid URL",
        N_("That does not look like a valid address."),
    ),
    (
        "Unable to download JSON metadata: HTTP Error 404",
        N_(
            "This does not look like a track or video page. It may be a browse "
            "page, or the item was removed. Open the track/video itself and grab "
            "it there, or right-click its title link."
        ),
    ),
    (
        "Unsupported URL",
        N_("No downloadable media was found at this address."),
    ),
    (
        "DRM",
        N_("This content is DRM-protected. GrabLine cannot and will not bypass DRM."),
    ),
    (
        "cookie database",
        N_(
            "Could not read the browser's cookie store. Close the browser "
            "completely and try again (Chromium locks its cookie database while "
            "running)."
        ),
    ),
    (
        "could not find",
        N_(
            "Could not find that browser's cookie store. Pick the browser you are "
            "actually signed in with under Settings."
        ),
    ),
)


#: yt-dlp failures a browser login gets past (age gate, bot check, members-only,
#: private). These need cookies, not just a JS runtime.
_AUTH_WALL_MARKERS = (
    "sign in to confirm your age",
    "confirm you're not a bot",
    "sign in to confirm you're not a bot",
    "members-only",
    "join this channel",
    "this video is available to",
    "login required",
    "private video",
    "sign in to",
)

#: yt-dlp failures a JS runtime (+ EJS solver) cures: YouTube's n challenge was
#: skipped, so the jsless clients handed back throttled/storyboard formats or
#: signature-expired media URLs (the intermittent "HTTP Error 403: Forbidden"
#: on the fast path - solving the challenge yields fresh, working URLs).
_RUNTIME_MARKERS = (
    "requested format is not available",
    "only images are available",
    "n challenge",
    "http error 403",
    "unable to download video data",
    "unable to download webpage: http error 403",
)


def _apply_network_guards(opts: dict[str, Any], proxy: str | None) -> None:
    """Bound and route every yt-dlp network operation.

    ``socket_timeout`` turns a dead connection into a retryable error instead
    of a job that sits at "Fetching metadata" forever. Forcing IPv4 on a
    network whose IPv6 is black-holed (see net.ipv6_broken) is the difference
    between a ~3s analysis and a ~80s one: without it every request serially
    times out on the v6 addresses before touching v4. A proxy connects onward
    itself, so no source binding then.
    """
    opts["socket_timeout"] = 20
    if not proxy and net.ipv6_broken():
        opts["source_address"] = "0.0.0.0"  # how --force-ipv4 is spelled internally


def _handoff_headers(raw: Any, *, has_cookie_source: bool) -> dict[str, str]:
    """The browser handoff's Referer/Cookie/User-Agent to hand yt-dlp, or ``{}``
    when there are none. Many gated CDNs refuse a stream (or serve an HTML error
    yt-dlp can't use) without the page's Referer, so the same headers the HLS and
    direct engines forward ride into yt-dlp too. yt-dlp merges these under each
    extractor's own headers, so a site extractor still wins where it sets them.
    The Cookie is dropped when yt-dlp is already loading cookies itself
    (cookiefile / cookiesfrombrowser), so the two jars can't fight.
    """
    if not isinstance(raw, dict) or not raw:
        return {}
    headers = {str(key): str(value) for key, value in raw.items()}
    if has_cookie_source:
        headers.pop("Cookie", None)
    return headers


def _normalize_error_text(message: str) -> str:
    """Lowercase and flatten curly quotes so YouTube's `you're` still matches
    our ASCII markers (yt-dlp surfaces the site's typography unchanged)."""
    return message.lower().replace("\u2019", "'").replace("\u2018", "'").replace("\u02bc", "'")


def _looks_like_auth_wall(message: str) -> bool:
    """True when a browser login could get past this failure (see _download_smart)."""
    lowered = _normalize_error_text(message)
    return any(marker in lowered for marker in _AUTH_WALL_MARKERS)


def _cookie_source_failed(message: str) -> bool:
    """True when yt-dlp could not read the browser cookie store (locked DB,
    missing profile, etc.) - worth trying another installed browser."""
    lowered = _normalize_error_text(message)
    return any(
        marker in lowered
        for marker in (
            "cookie database",
            "could not copy",
            "could not find",
            "unable to load cookies",
            "cookiesfrombrowser",
            "failed to decrypt",
        )
    )


def _runtime_might_help(message: str) -> bool:
    """True when providing a JS runtime + EJS solver could cure this failure."""
    lowered = _normalize_error_text(message)
    return any(marker in lowered for marker in _RUNTIME_MARKERS)


def friendly_error(message: str) -> str:
    """Map a raw yt-dlp error onto a sentence a person can act on."""
    normalized = _normalize_error_text(message)
    for marker, friendly in _FRIENDLY_ERRORS:
        if marker.lower() in normalized:
            return t(friendly)
    first_line = message.strip().splitlines()[0] if message.strip() else t("download failed")
    return first_line.removeprefix("ERROR:").strip()


@dataclass(frozen=True)
class QualityOption:
    """One row in the quality panel."""

    label: str  # "Best", "1080p", "MP3", "M4A"
    kind: str  # "video" | "audio"
    format_spec: str  # yt-dlp format selector
    height: int | None = None
    estimated_size: int | None = None
    audio_format: str | None = None  # "mp3" | "m4a" for audio options


@dataclass(frozen=True)
class MediaInfo:
    """What inspect() learned about a URL, ready for the quality panel."""

    url: str
    id: str
    title: str
    uploader: str | None
    duration: float | None
    thumbnail_url: str | None
    options: tuple[QualityOption, ...]
    subtitle_languages: tuple[str, ...] = ()
    auto_caption_languages: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlaylistEntry:
    url: str
    title: str
    duration: float | None
    index: int


@dataclass(frozen=True)
class PlaylistInfo:
    """A fast flat listing of a playlist (F1.7): titles and URLs, no formats."""

    url: str
    title: str
    uploader: str | None
    entries: tuple[PlaylistEntry, ...]


def _tier_format(tier: int) -> str:
    """A quality-tier selector that never dead-ends. Prefers the best
    video+audio at or below ``tier`` (then a muxed stream at that cap), but
    falls back to the best available format - so a video that has no stream at
    that exact tier still downloads instead of failing "Requested format is
    not available"."""
    return f"bv*[height<={tier}]+ba/b[height<={tier}]/bv*+ba/b"


def generic_quality_options() -> tuple[QualityOption, ...]:
    """Static picker for playlist batches, where per-video sizes are unknown.
    yt-dlp resolves the actual formats per entry at download time."""
    options = [QualityOption(label="Best", kind="video", format_spec="bv*+ba/b")]
    for tier in (1080, 720, 480):
        options.append(
            QualityOption(
                label=f"{tier}p",
                kind="video",
                format_spec=_tier_format(tier),
                height=tier,
            )
        )
    options.append(QualityOption(label="MP3", kind="audio", format_spec="ba/b", audio_format="mp3"))
    options.append(
        QualityOption(label="M4A", kind="audio", format_spec="ba[ext=m4a]/ba/b", audio_format="m4a")
    )
    options.append(
        QualityOption(label="FLAC", kind="audio", format_spec="ba/b", audio_format="flac")
    )
    return tuple(options)


def option_for_label(
    label: str, options: tuple[QualityOption, ...] | None = None
) -> QualityOption | None:
    """The quality option matching ``label`` (case-insensitive), for handoffs
    that already carry a choice (F1.3 in-page panel). Tries a media's curated
    ``options`` first, then the generic tiers; "best" falls back to the top."""
    wanted = label.strip().lower()
    for pool in (options or (), generic_quality_options()):
        for option in pool:
            if option.label.lower() == wanted:
                return option
        if wanted == "best" and pool:
            return pool[0]
    return None


def parse_playlist(info: dict[str, Any]) -> PlaylistInfo | None:
    """Turn a flat-extracted yt-dlp info dict into a PlaylistInfo, or None."""
    if info.get("_type") != "playlist":
        return None
    entries: list[PlaylistEntry] = []
    for position, raw in enumerate(info.get("entries") or [], start=1):
        if not raw:
            continue  # deleted/private items come through as None
        url = raw.get("url") or raw.get("webpage_url")
        if url and not str(url).startswith(("http://", "https://")):
            # Some flat extractions yield bare video IDs.
            url = (
                f"https://www.youtube.com/watch?v={url}" if raw.get("ie_key") == "Youtube" else None
            )
        if not url:
            continue
        entries.append(
            PlaylistEntry(
                url=str(url),
                title=str(raw.get("title") or f"Item {position}"),
                duration=raw.get("duration"),
                index=position,
            )
        )
    return PlaylistInfo(
        url=str(info.get("webpage_url") or info.get("original_url") or ""),
        title=str(info.get("title") or "Playlist"),
        uploader=info.get("uploader") or info.get("channel"),
        entries=tuple(entries),
    )


def _format_size(fmt: dict[str, Any]) -> int | None:
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    return int(size) if size else None


def _snap_to_tier(height: int) -> int | None:
    """1088 -> 1080, 2176 -> 2160 … formats are rarely exactly on the ladder."""
    for tier in QUALITY_TIERS:
        if abs(height - tier) <= tier * 0.08:
            return tier
    return None


def curate_formats(info: dict[str, Any]) -> tuple[QualityOption, ...]:
    """Boil the raw format list down to the curated picker (F0.3)."""
    formats: list[dict[str, Any]] = info.get("formats") or []
    video_formats = [f for f in formats if f.get("vcodec") not in (None, "none")]
    audio_formats = [
        f
        for f in formats
        if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
    ]

    best_audio = max(audio_formats, key=lambda f: f.get("abr") or 0, default=None)
    audio_size = _format_size(best_audio) if best_audio else None

    def tier_estimate(tier: int) -> int | None:
        candidates = [
            f for f in video_formats if f.get("height") and _snap_to_tier(f["height"]) == tier
        ]
        if not candidates:
            return None
        best = max(candidates, key=lambda f: (f.get("tbr") or 0, _format_size(f) or 0))
        video_size = _format_size(best)
        if video_size is None:
            return None
        needs_audio = best.get("acodec") in (None, "none")
        if needs_audio and audio_size is not None:
            return video_size + audio_size
        return video_size

    options: list[QualityOption] = []
    tiers_present = sorted(
        {
            tier
            for f in video_formats
            if f.get("height") and (tier := _snap_to_tier(f["height"])) is not None
        },
        reverse=True,
    )
    if video_formats:
        best_estimate = tier_estimate(tiers_present[0]) if tiers_present else None
        options.append(
            QualityOption(
                label="Best",
                kind="video",
                format_spec="bv*+ba/b",
                height=max((f.get("height") or 0) for f in video_formats) or None,
                estimated_size=best_estimate,
            )
        )
        for tier in tiers_present:
            options.append(
                QualityOption(
                    label=f"{tier}p",
                    kind="video",
                    format_spec=_tier_format(tier),
                    height=tier,
                    estimated_size=tier_estimate(tier),
                )
            )
    if audio_formats or video_formats:
        options.append(
            QualityOption(
                label="MP3",
                kind="audio",
                format_spec="ba/b",
                estimated_size=audio_size,
                audio_format="mp3",
            )
        )
        options.append(
            QualityOption(
                label="M4A",
                kind="audio",
                format_spec="ba[ext=m4a]/ba/b",
                estimated_size=audio_size,
                audio_format="m4a",
            )
        )
        options.append(
            QualityOption(
                label="FLAC",
                kind="audio",
                format_spec="ba/b",
                audio_format="flac",  # lossless re-encode; no size estimate
            )
        )
    return tuple(options)


#: Raw single-video info dicts from analysis, handed to the download so a
#: fresh add never pays the extraction twice (analysis for the quality panel,
#: then the download re-extracting the very same thing - the reason YouTube
#: felt twice as slow as single-extraction sites). Keyed (url, proxy).
#: Format URLs from YouTube stay valid for hours, far beyond this TTL.
_INFO_TTL = 300.0
_info_lock = threading.Lock()
_info_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}

#: Download-shaped extracts (cookies + JS runtime for YouTube) started while
#: the quality panel is open so Confirm does not wait on a second extraction.
_READY_TTL = 300.0
_ready_lock = threading.Lock()
_ready_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_ready_inflight: dict[tuple[str, str], threading.Event] = {}
_ready_cancels: dict[tuple[str, str], threading.Event] = {}


def _remember_info(url: str, proxy: str | None, info: dict[str, Any]) -> None:
    if not info.get("formats"):
        return  # a flat playlist listing can't be downloaded from
    with _info_lock:
        now = time.monotonic()
        for key, (at, _value) in list(_info_cache.items()):
            if now - at >= _INFO_TTL:
                del _info_cache[key]
        _info_cache[(url, proxy or "")] = (now, copy.deepcopy(info))


def recall_info(url: str, proxy: str | None = None) -> dict[str, Any] | None:
    """A fresh analysis of ``url``, ready for ``process_ie_result`` - or None."""
    with _info_lock:
        hit = _info_cache.get((url, proxy or ""))
        if hit is None or time.monotonic() - hit[0] >= _INFO_TTL:
            return None
        return copy.deepcopy(hit[1])


def _ready_key(url: str, proxy: str | None) -> tuple[str, str]:
    return (url, proxy or "")


def _remember_download_ready(url: str, proxy: str | None, info: dict[str, Any]) -> None:
    if not info.get("formats"):
        return
    with _ready_lock:
        now = time.monotonic()
        for key, (at, _value) in list(_ready_cache.items()):
            if now - at >= _READY_TTL:
                del _ready_cache[key]
        _ready_cache[_ready_key(url, proxy)] = (now, copy.deepcopy(info))


def recall_download_ready(url: str, proxy: str | None = None) -> dict[str, Any] | None:
    """Prefetched download-ready info for ``url``, or None."""
    with _ready_lock:
        hit = _ready_cache.get(_ready_key(url, proxy))
        if hit is None or time.monotonic() - hit[0] >= _READY_TTL:
            return None
        return copy.deepcopy(hit[1])


def cancel_download_prefetch(url: str, proxy: str | None = None) -> None:
    """Signal an in-flight panel prefetch to abandon its result."""
    with _ready_lock:
        cancel = _ready_cancels.get(_ready_key(url, proxy))
    if cancel is not None:
        cancel.set()


def take_download_ready(
    url: str,
    proxy: str | None = None,
    *,
    wait: float = 0.0,
    stop: threading.Event | None = None,
) -> dict[str, Any] | None:
    """Return a prefetched extract, waiting briefly if one is still running.

    Used by the download path so Confirm after the quality panel does not start
    a second full YouTube extraction when the panel already kicked one off.
    """
    key = _ready_key(url, proxy)
    hit = recall_download_ready(url, proxy)
    if hit is not None:
        return hit
    if wait <= 0:
        return None
    with _ready_lock:
        done = _ready_inflight.get(key)
    if done is None:
        return recall_download_ready(url, proxy)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if stop is not None and stop.is_set():
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if done.wait(timeout=min(0.25, remaining)):
            break
    return recall_download_ready(url, proxy)


def prefetch_download_ready(
    url: str,
    *,
    proxy: str | None = None,
    session_browser: str | None = None,
    headers: dict[str, str] | None = None,
) -> None:
    """Start a background download-shaped extract for ``url`` (idempotent).

    Matches SmartDownload's YouTube policy: cookies + JS runtime when a browser
    profile exists. Safe to call from the GUI thread; work runs off-thread.
    """
    key = _ready_key(url, proxy)
    with _ready_lock:
        hit = _ready_cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < _READY_TTL:
            return  # already warm
        if key in _ready_inflight:
            return  # already running
        done = threading.Event()
        cancel = threading.Event()
        _ready_inflight[key] = done
        _ready_cancels[key] = cancel

    def work() -> None:
        try:
            if cancel.is_set():
                return
            from app.core.browser_setup import detect_cookie_browser

            browser = session_browser or detect_cookie_browser()
            youtube = needs_js_runtime(url)
            use_session = bool(youtube and browser)
            with_runtime = bool(youtube)  # unthrottled formats need the challenge solved
            if cancel.is_set():
                return
            engine = SmartEngine()
            info = engine._extract_info(
                url,
                with_runtime=with_runtime,
                use_session=use_session,
                session_browser=browser or "chrome",
                proxy=proxy,
                force_generic=False,
                headers=headers,
            )
            if cancel.is_set():
                return
            _remember_download_ready(url, proxy, info)
            log.info("prefetched download-ready info for %s", url)
        except Exception:
            log.debug("download-ready prefetch failed for %s", url, exc_info=True)
        finally:
            with _ready_lock:
                _ready_inflight.pop(key, None)
                _ready_cancels.pop(key, None)
            done.set()

    threading.Thread(target=work, name="gl-prefetch", daemon=True).start()


def needs_js_runtime(url: str, *, use_session: bool = False) -> bool:
    """Does this URL need a JavaScript runtime to extract properly?

    YouTube expects one to solve its 'n challenge'. Without it yt-dlp falls
    back to solving the challenge in pure Python - slow - and formats can come
    back throttled or unusable ("Requested format is not available"), so it is
    needed for every YouTube URL, not only signed-in ones. Other sites only
    need it when a browser session pushes yt-dlp onto a JS-dependent client.
    """
    if use_session:
        return True
    host = (urlsplit(url).hostname or "").lower()
    return host in ("youtu.be", "youtube.com", "youtube-nocookie.com") or host.endswith(
        (".youtube.com", ".youtube-nocookie.com")
    )


def provision_js_runtime(
    url: str, *, use_session: bool = False, proxy: str | None = None
) -> tuple[str, str] | None:
    """The (name, path) of a JS runtime for a yt-dlp run: one already
    installed, else Deno fetched once for a URL that needs it.

    Best-effort - on failure the caller carries on without one.
    """
    from app.core import jsruntime

    found = jsruntime.detect_js_runtime()
    if found is not None:
        return found
    if not needs_js_runtime(url, use_session=use_session):
        return None
    try:
        log.info("no JavaScript runtime found - fetching Deno (one-time ~40 MB)")
        return ("deno", str(jsruntime.ensure_deno(proxy=proxy)))
    except DownloadError as exc:
        log.warning("could not provision a JS runtime: %s", exc)
    except Exception:  # never let runtime setup break the caller
        log.exception("unexpected error provisioning a JS runtime")
    return None


class SmartEngine:
    """Extractor matching and metadata inspection."""

    #: How long an analysis stays reusable. Only ever feeds the quality panel -
    #: the download re-extracts the URL itself - so a short window is safe.
    INSPECT_TTL = 300.0

    def __init__(self) -> None:
        self._extractors: list[Any] | None = None
        self._lock = threading.Lock()
        self._inspected: dict[tuple[Any, ...], tuple[float, MediaInfo | PlaylistInfo]] = {}

    def _extractor_classes(self) -> list[Any]:
        with self._lock:
            if self._extractors is None:
                from yt_dlp.extractor import gen_extractor_classes

                self._extractors = [ie for ie in gen_extractor_classes() if ie.IE_NAME != "generic"]
            return self._extractors

    def warm_up(self) -> None:
        """Build the extractor list now. Call from a worker thread."""
        self._extractor_classes()

    @property
    def warm(self) -> bool:
        """True once the extractor list exists, i.e. matches() is instant.

        Building that list imports yt-dlp and instantiates ~1700 extractor
        classes - seconds of work, and it holds ``_lock`` while it happens.
        The GUI thread must ask this before calling matches(), or a URL added
        in the first seconds after launch freezes the window until the
        background warm-up finishes.
        """
        return self._extractors is not None

    def matches(self, url: str) -> bool:
        """Offline check: does a real site extractor (not generic) claim this URL?"""
        return any(ie.suitable(url) for ie in self._extractor_classes())

    def inspect(
        self,
        url: str,
        *,
        use_session: bool = False,
        session_browser: str = "chrome",
        proxy: str | None = None,
        force_generic: bool = False,
        headers: dict[str, str] | None = None,
    ) -> MediaInfo | PlaylistInfo:
        """Metadata for a URL, reusing a recent analysis of the same URL.

        Analysis is the slow part of adding a video (yt-dlp fetches the page
        and lists every format). The quality panel only needs that list, so
        YouTube runs JS-less first (~seconds); cookies + JS runtime escalate
        only on auth walls / empty formats. While the panel is open a separate
        download-ready prefetch pays for the unthrottled extract so Confirm
        does not wait on a second full extraction.
        """
        # Auto-pick a browser name for the cache key / escalation path, but do
        # not force use_session=True - that was making every YouTube analysis
        # take the 26-87s cookies+runtime path before the panel appeared.
        if needs_js_runtime(url) and not session_browser:
            from app.core.browser_setup import detect_cookie_browser

            session_browser = detect_cookie_browser() or "chrome"
        header_key = tuple(sorted((headers or {}).items()))
        key = (url, use_session, session_browser, proxy or "", force_generic, header_key)
        now = time.monotonic()
        with self._lock:
            hit = self._inspected.get(key)
            if hit is not None and now - hit[0] < self.INSPECT_TTL:
                return hit[1]
        result = self._inspect_uncached(
            url,
            use_session=use_session,
            session_browser=session_browser,
            proxy=proxy,
            force_generic=force_generic,
            headers=headers,
        )
        log.info("analyzed %s in %.1fs", url, time.monotonic() - now)
        with self._lock:
            # Drop anything stale so a long session can't grow this unbounded.
            self._inspected = {
                k: v for k, v in self._inspected.items() if now - v[0] < self.INSPECT_TTL
            }
            self._inspected[key] = (now, result)
        return result

    def _inspect_uncached(
        self,
        url: str,
        *,
        use_session: bool = False,
        session_browser: str = "chrome",
        proxy: str | None = None,
        force_generic: bool = False,
        headers: dict[str, str] | None = None,
    ) -> MediaInfo | PlaylistInfo:
        """Metadata for a single video, or a fast flat listing for a playlist.

        Analysis runs *without* a JavaScript runtime first: the quality panel
        only needs the format list, which YouTube returns fine without solving
        its challenge - measured ~4s JS-less vs 26-87s with the runtime, same
        options either way. The runtime matters for the *download* (unthrottled
        URLs), and SmartDownload provisions it there. Only when the JS-less
        pass comes back degraded (a runtime-marker error, an auth/bot wall, or
        a video with no usable formats) does analysis retry once - with a JS
        runtime and/or the browser login, matching SmartDownload's escalation.
        A forced browser session uses the runtime from the start (cookies push
        yt-dlp onto JS-dependent clients).

        ``force_generic`` runs yt-dlp's page-scraping generic extractor even
        when no site extractor claims the URL - the last-resort path for media
        embedded in pages yt-dlp has no dedicated support for.
        """
        try:
            info = self._extract_info(
                url,
                with_runtime=use_session,  # cookies need JS-dependent clients
                use_session=use_session,
                session_browser=session_browser,
                proxy=proxy,
                force_generic=force_generic,
                headers=headers,
            )
        except DownloadError as exc:
            # _extract_info already mapped to a friendly sentence; recover the
            # raw yt-dlp text from __cause__ so marker checks still work.
            raw = str(exc.__cause__ or exc)
            want_login = not use_session and _looks_like_auth_wall(raw)
            want_runtime = not use_session and _runtime_might_help(raw)
            if not (want_login or want_runtime):
                raise
            log.info(
                "jsless analysis of %s failed (%s); retrying with%s%s",
                url,
                exc,
                " a JS runtime" if want_runtime or want_login else "",
                f" + {session_browser} login" if want_login else "",
            )
            info = self._extract_info(
                url,
                with_runtime=True,
                use_session=use_session or want_login,
                session_browser=session_browser,
                proxy=proxy,
                force_generic=force_generic,
                headers=headers,
            )
        result = self._parse_inspected(
            url, info, use_session=use_session, session_browser=session_browser, proxy=proxy
        )
        if isinstance(result, MediaInfo) and not result.options and not use_session:
            # Degraded jsless answer (e.g. storyboard images only): one retry
            # with the runtime before reporting there's nothing to download.
            log.info("jsless analysis of %s found no formats; retrying with a runtime", url)
            info = self._extract_info(
                url,
                with_runtime=True,
                use_session=use_session,
                session_browser=session_browser,
                proxy=proxy,
                force_generic=force_generic,
                headers=headers,
            )
            result = self._parse_inspected(
                url, info, use_session=use_session, session_browser=session_browser, proxy=proxy
            )
        return result

    def _extract_info(
        self,
        url: str,
        *,
        with_runtime: bool,
        use_session: bool,
        session_browser: str,
        proxy: str | None,
        force_generic: bool,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """One yt-dlp metadata extraction. ``noplaylist`` keeps watch-URLs-
        with-a-list-param as single videos; pure playlist URLs still come back
        as playlists, and ``extract_flat`` makes that one cheap request
        instead of hundreds."""
        import yt_dlp

        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "extract_flat": "in_playlist",
            "skip_download": True,
        }
        _apply_network_guards(opts, proxy)
        if force_generic:
            # Scrape the page itself for <video>/og:video/JSON-LD/m3u8 links.
            opts["force_generic_extractor"] = True
        if with_runtime:
            runtime = provision_js_runtime(url, use_session=use_session, proxy=proxy)
            if runtime is not None:
                name, path = runtime
                opts["js_runtimes"] = {name: {"path": path}}
                opts["remote_components"] = ["ejs:github"]
        if use_session:
            opts["cookiesfrombrowser"] = (session_browser,)
        if proxy:
            opts["proxy"] = proxy
        # The browser handoff's Referer/Cookie/User-Agent, so a gated video
        # analyses with the same credentials its page had (matching the download).
        forwarded = _handoff_headers(headers, has_cookie_source=use_session)
        if forwarded:
            opts["http_headers"] = forwarded
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            # Keep the raw message on __cause__ so callers can still match
            # auth/runtime markers after we surface a friendly sentence.
            raise DownloadError(friendly_error(str(exc))) from exc
        if not isinstance(info, dict):
            raise DownloadError("No downloadable media was found at this address.")
        if not force_generic:
            # Hand this analysis to the download so it can skip re-extracting
            # when the download stays on the same cookie/runtime policy.
            _remember_info(url, proxy, info)
        return info

    def _parse_inspected(
        self,
        url: str,
        info: dict[str, Any],
        *,
        use_session: bool,
        session_browser: str,
        proxy: str | None,
    ) -> MediaInfo | PlaylistInfo:
        playlist = parse_playlist(info)
        if playlist is not None:
            if not playlist.entries:
                raise DownloadError("This playlist appears to be empty.")
            if len(playlist.entries) == 1:
                # A one-item playlist deserves the full single-video panel.
                return self.inspect(
                    playlist.entries[0].url,
                    use_session=use_session,
                    session_browser=session_browser,
                    proxy=proxy,
                )
            return playlist
        return MediaInfo(
            url=url,
            id=str(info.get("id") or ""),
            title=str(info.get("title") or "Untitled"),
            uploader=info.get("uploader") or info.get("channel"),
            duration=info.get("duration"),
            thumbnail_url=info.get("thumbnail"),
            options=curate_formats(info),
            subtitle_languages=tuple(sorted((info.get("subtitles") or {}).keys())),
            auto_caption_languages=tuple(sorted((info.get("automatic_captions") or {}).keys())),
        )


class _StopRequested(Exception):
    pass


@dataclass
class _LiveProgress:
    per_file: dict[str, int] = field(default_factory=dict)
    totals: dict[str, int | None] = field(default_factory=dict)
    #: Best-effort overall size from yt-dlp's info_dict (requested formats),
    #: used until per-file totals catch up. Without this, DASH merges sat at
    #: "Fetching metadata…" while tens of MB were already on disk.
    expected_total: int | None = None


# ---------------------------------------------------- cancellable ffmpeg
# yt-dlp gives no handle on the FFmpeg it spawns, and its postprocessor hook
# only fires *between* steps - so cancelling during a 4K merge or an MP3
# extraction left ffmpeg running to completion, pinning a core (and holding
# the temp files) for minutes after the download vanished from the list.
# Wrapping the Popen yt-dlp's ffmpeg postprocessor uses is the only handle
# there is. Children are recorded against the thread that spawned them, so one
# job's cancel can never reach into another's.

_children_lock = threading.Lock()
_children: dict[int, set[Any]] = {}
_tracking_installed = False


def _track_ffmpeg_children() -> None:
    """Install the Popen wrapper once, on first use of the Smart engine."""
    global _tracking_installed
    with _children_lock:
        if _tracking_installed:
            return
        _tracking_installed = True
    try:
        from yt_dlp.postprocessor import ffmpeg as ffmpeg_pp
    except Exception:  # pragma: no cover - yt-dlp layout changed; cancel still
        log.debug("could not install ffmpeg tracking", exc_info=True)
        return
    base = ffmpeg_pp.Popen

    class _TrackedPopen(base):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            owner = threading.get_ident()
            with _children_lock:
                _children.setdefault(owner, set()).add(self)

    ffmpeg_pp.Popen = _TrackedPopen


def _kill_tracked_children(owner: int | None) -> None:
    """Stop the FFmpeg processes started by ``owner``'s thread."""
    if owner is None:
        return
    with _children_lock:
        live = _children.get(owner, set())
        _children[owner] = {child for child in live if child.poll() is None}
        doomed = list(_children[owner])
    for child in doomed:
        with contextlib.suppress(Exception):
            child.kill()


def _forget_tracked_children(owner: int | None) -> None:
    if owner is None:
        return
    with _children_lock:
        _children.pop(owner, None)


class _ProgressPersister:
    """Writes a SMART job's progress to SQLite on its own background thread,
    off yt-dlp's download thread.

    yt-dlp calls ``progress_hooks`` synchronously and will not read the next
    chunk until the hook returns - unlike the segmented engine, which already
    decouples its checkpoint writes onto their own thread (see downloader.py's
    _Checkpointer), a hook that writes to the database inline makes ANY delay
    in that write (lock contention from a sibling download's own writes, or
    just the UI's periodic queries sharing the same connection) a direct stall
    in this job's actual network throughput - measured as a download that
    craters to a trickle purely because something else was also busy with the
    database, with no real bandwidth or server-side cause at all. The hook now
    only updates an in-memory counter (see SmartDownload._hook); this thread
    reads it and persists on its own schedule instead.
    """

    def __init__(
        self,
        db: Database,
        job_id: int,
        interval: float,
        snapshot: Callable[[], tuple[int, int | None]],
    ) -> None:
        self._db = db
        self._job_id = job_id
        self._interval = interval
        self._snapshot = snapshot
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name=f"gl-smart-persist-{job_id}", daemon=True
        )
        self._stopped = False
        self._last_downloaded = -1
        self._last_total: int | None = None

    def start(self) -> None:
        self._thread.start()

    def flush(self) -> None:
        downloaded, total = self._snapshot()
        if downloaded != self._last_downloaded:
            self._last_downloaded = downloaded
            self._db.update_job_downloaded(self._job_id, downloaded)
        if total is not None and total != self._last_total:
            self._last_total = total
            self._db.update_job_total(self._job_id, total)

    def _loop(self) -> None:
        while not self._stop_event.wait(self._interval):
            self.flush()

    def stop(self) -> None:
        """Stop the thread and write one final, up-to-the-moment snapshot.
        Idempotent - safe to call from every outcome path without tracking
        which one actually ran first."""
        if self._stopped:
            return
        self._stopped = True
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        self.flush()


class SmartDownload:
    """Runs one smart job through yt-dlp. One-shot object, like SegmentedDownload.

    Job options (job.options):
        format_spec: str        yt-dlp format selector (required)
        audio_format: str|None  "mp3"/"m4a" -> extract audio + tags + cover art
        subtitles: {"lang": str, "auto": bool, "embed": bool} | None
        trim: [start_seconds, end_seconds] | None   (F0.7)
        use_session: bool, session_browser: str      (F0.8)
    """

    def __init__(
        self,
        db: Database,
        job: Job,
        *,
        ffmpeg_path: str | None = None,
        persist_interval: float = 0.3,
        ratelimit: int | None = None,
        fair_limiter: Any = None,
        proxy: str | None = None,
    ) -> None:
        self.db = db
        self.job = job
        self.ffmpeg_path = ffmpeg_path
        self.persist_interval = persist_interval
        self.ratelimit = ratelimit
        # Live equal-share cap from the manager; yt-dlp's ratelimit is fixed at
        # start, so fair-speed sleeps here on each progress delta instead.
        self.fair_limiter = fair_limiter
        self.proxy = proxy
        self._stop_event = threading.Event()
        self._cancelled = False
        self._live = _LiveProgress()
        self._known_files: set[str] = set()
        self._js_runtime: tuple[str, str] | None = None  # (yt-dlp name, path)
        self._title_adopted = False
        #: Which thread is running this download, so pause/cancel can reach the
        #: FFmpeg it spawned without touching another job's.
        self._worker_thread: int | None = None
        self._persister = _ProgressPersister(db, job.id, persist_interval, self._progress_snapshot)

    # ------------------------------------------------------------ control

    def pause(self) -> None:
        self._stop_event.set()
        _kill_tracked_children(self._worker_thread)

    def cancel(self) -> None:
        self._cancelled = True
        self._stop_event.set()
        _kill_tracked_children(self._worker_thread)

    @property
    def bytes_downloaded(self) -> int:
        return sum(self._live.per_file.values())

    # ---------------------------------------------------------------- run

    def run(self) -> JobStatus:
        self._worker_thread = threading.get_ident()
        _track_ffmpeg_children()
        self.db.set_job_status(self.job.id, JobStatus.DOWNLOADING)
        self._persister.start()
        try:
            return self._run_guarded()
        finally:
            # Thread ids get reused; leaving this job's children registered
            # would let a later job's cancel go looking at stale handles.
            _forget_tracked_children(self._worker_thread)

    def _run_guarded(self) -> JobStatus:
        import yt_dlp

        try:
            info = self._download_smart()
        except _StopRequested:
            return self._settle_stopped()
        except yt_dlp.utils.DownloadError as exc:
            if self._stop_event.is_set():  # our hook exception, re-wrapped by yt-dlp
                return self._settle_stopped()
            return self._finish_failed(friendly_error(str(exc)))
        except DownloadError as exc:
            return self._finish_failed(str(exc))
        except Exception:
            if self._stop_event.is_set():
                return self._settle_stopped()
            log.exception("unexpected error in smart job %s", self.job.id)
            return self._finish_failed("unexpected internal error (see log)")
        return self._finalize(info)

    # ------------------------------------------------------------ internals

    def _build_options(
        self, *, with_cookies: bool = False, with_runtime: bool = False
    ) -> dict[str, Any]:
        options = self.job.options
        if options.get("name_from_metadata"):
            # Queued without an analysis (the in-page quality panel): the job's
            # stored name is a placeholder, so let yt-dlp name the file from
            # the real title - _finalize adopts whatever lands on disk.
            outtmpl = str(Path(self.job.dest_dir) / "%(title)s.%(ext)s")
        else:
            base = Path(self.job.filename).stem or "download"
            outtmpl = str(Path(self.job.dest_dir) / f"{base}.%(ext)s")
        fmt = options.get("format_spec") or "bv*+ba/b"
        if not self.ffmpeg_path and "+" in fmt:
            # No FFmpeg to merge separate video+audio: prefer a pre-merged
            # (progressive) stream so the download still works instead of
            # aborting on the merge. yt-dlp picks bv*+ba first when it can and
            # only fails at merge time, so we must put the merge-free option
            # first, not rely on the trailing '/b' fallback.
            fmt = f"b/{fmt}"
        ydl_opts: dict[str, Any] = {
            "format": fmt,
            "outtmpl": {"default": outtmpl},
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "progress_hooks": [self._hook],
            "postprocessor_hooks": [self._postprocessor_hook],
            "retries": 3,
            "fragment_retries": 5,
            "continuedl": True,
            # YouTube (and other DASH/HLS) ships many small fragments; one at a
            # time leaves the pipe idle between requests. Parallelism cuts
            # time-to-first-byte wait without hammering the CDN the way a huge
            # connection count would. YouTube gets a higher budget - its CDNs
            # tolerate it and the fragments are tiny.
            "concurrent_fragment_downloads": (16 if needs_js_runtime(self.job.url) else 8),
            # Larger HTTP chunks also help progressive (non-fragment) URLs
            # start transferring sooner and keep the socket busy.
            "http_chunk_size": 10 * 1024 * 1024,
        }
        _apply_network_guards(ydl_opts, self.proxy)
        if self.ffmpeg_path:
            ydl_opts["ffmpeg_location"] = self.ffmpeg_path
        if with_runtime and self._js_runtime:
            # The slow path, used only on escalation: hand yt-dlp the runtime we
            # found (yt-dlp only auto-enables Deno, and only if on PATH) so it can
            # solve YouTube's n challenge, plus allow the EJS solver download
            # (without it the challenge is skipped and only storyboards come
            # back). The fast path omits both so normal videos use the jsless
            # android_vr/tv clients and start quickly.
            name, path = self._js_runtime
            ydl_opts["js_runtimes"] = {name: {"path": path}}
            ydl_opts["remote_components"] = ["ejs:github"]
        if self.ratelimit:
            ydl_opts["ratelimit"] = float(self.ratelimit)
        # Postprocessing (audio extraction, tags, subtitle conversion) needs
        # FFmpeg. Without it, plain video downloads still work untouched.
        has_ffmpeg = bool(self.ffmpeg_path)
        postprocessors: list[dict[str, Any]] = []
        audio_format = options.get("audio_format")
        if audio_format:
            if not has_ffmpeg:
                raise DownloadError(
                    "FFmpeg is required for audio extraction. Install it from Settings"
                )
            postprocessors.append(
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": audio_format,
                    # mp3 re-encodes: honor the configured bitrate; lossless
                    # (flac/wav) and remuxes keep the source quality ("0").
                    "preferredquality": (
                        str(options.get("audio_bitrate") or "192") if audio_format == "mp3" else "0"
                    ),
                }
            )
            postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True})
            # Embed cover art only for MP3. MP3 uses a reliable ffmpeg ID3 embed;
            # M4A (and other MP4-family audio) needs AtomicParsley, which we do
            # not bundle, and yt-dlp's ffmpeg fallback then fails ("Unable to
            # embed using ffprobe & ffmpeg") - which used to mark the whole
            # download failed even though the audio file was fine. Skipping the
            # embed for those formats lets them finish (without a cover) instead
            # of failing, and leaves no stray thumbnail file behind.
            if audio_format == "mp3":
                postprocessors.append({"key": "EmbedThumbnail"})
                ydl_opts["writethumbnail"] = True
        elif has_ffmpeg:
            postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True})
        subtitles = options.get("subtitles")
        if subtitles and subtitles.get("lang"):
            ydl_opts["writesubtitles"] = True
            ydl_opts["writeautomaticsub"] = bool(subtitles.get("auto"))
            ydl_opts["subtitleslangs"] = [subtitles["lang"]]
            if has_ffmpeg:
                postprocessors.append({"key": "FFmpegSubtitlesConvertor", "format": "srt"})
                if subtitles.get("embed"):
                    postprocessors.append({"key": "FFmpegEmbedSubtitle"})
        trim = options.get("trim")
        if trim:
            from yt_dlp.utils import download_range_func

            start, end = float(trim[0] or 0), float(trim[1])
            ydl_opts["download_ranges"] = download_range_func(None, [(start, end)])
            ydl_opts["force_keyframes_at_cuts"] = True
        # SponsorBlock: skip or just mark sponsor/intro/outro segments. Needs
        # FFmpeg to actually cut; marking as chapters works without a re-encode.
        sponsorblock = options.get("sponsorblock")
        if sponsorblock and has_ffmpeg:
            postprocessors.append(
                {"key": "SponsorBlock", "categories": ["sponsor", "selfpromo", "interaction"]}
            )
            postprocessors.append(
                {
                    "key": "ModifyChapters",
                    "remove_sponsor_segments": ["sponsor", "selfpromo", "interaction"]
                    if sponsorblock == "remove"
                    else [],
                }
            )
        # Keep the video's own chapter marks (yt-dlp writes them into the file).
        if options.get("chapters") and has_ffmpeg and audio_format is None:
            postprocessors.append({"key": "FFmpegMetadata", "add_chapters": True})
        # Save the poster/cover as a sidecar image, and the full metadata as
        # .info.json (title, uploader, description, tags - "metadata download").
        if options.get("save_thumbnail"):
            ydl_opts["writethumbnail"] = True
        if options.get("save_metadata"):
            ydl_opts["writeinfojson"] = True
        # A cookies.txt (Netscape format) the user exported - the manual/OAuth
        # cookie path, and what works headless where reading a live browser
        # profile can't. Takes precedence over cookiesfrombrowser.
        cookie_file = options.get("cookie_file")
        if cookie_file and Path(cookie_file).is_file():
            ydl_opts["cookiefile"] = cookie_file
        elif with_cookies and (browser := self._cookie_browser()):
            ydl_opts["cookiesfrombrowser"] = (browser,)
        # Power-user escape hatch: extra ffmpeg args (e.g. -metadata, a codec
        # tweak) applied to the merge/convert steps.
        extra_ffmpeg = options.get("ffmpeg_args")
        if extra_ffmpeg:
            ydl_opts["postprocessor_args"] = {"default": list(extra_ffmpeg)}
        if self.proxy:
            ydl_opts["proxy"] = self.proxy
        forwarded = _handoff_headers(
            options.get("http_headers"),
            has_cookie_source="cookiefile" in ydl_opts or "cookiesfrombrowser" in ydl_opts,
        )
        if forwarded:
            ydl_opts["http_headers"] = forwarded
        ydl_opts["postprocessors"] = postprocessors
        return ydl_opts

    def _needs_js_runtime(self) -> bool:
        return needs_js_runtime(self.job.url, use_session=bool(self.job.options.get("use_session")))

    def _ensure_js_runtime(self) -> None:
        """Make a JS runtime available before yt-dlp runs: prefer one already
        installed (Node/Bun/Deno/QuickJS - yt-dlp won't auto-enable them, so we
        pass them explicitly), and only download Deno if nothing is present.
        Non-fatal: on failure the download still tries."""
        if not self._needs_js_runtime():
            return
        from app.core import jsruntime

        found = jsruntime.detect_js_runtime()
        if found is not None:
            log.info("job %s: using %s as the JavaScript runtime", self.job.id, found[0])
            self._js_runtime = found
            return
        try:
            log.info(
                "job %s: no JavaScript runtime found - fetching Deno (one-time ~40 MB)",
                self.job.id,
            )
            self._js_runtime = ("deno", str(jsruntime.ensure_deno(proxy=self.proxy)))
        except DownloadError as exc:
            log.warning("job %s: could not provision a JS runtime: %s", self.job.id, exc)
        except Exception:  # never let runtime setup crash the job
            log.exception("job %s: unexpected error provisioning a JS runtime", self.job.id)

    def _wants_ffmpeg(self) -> bool:
        """Whether this job needs FFmpeg: a merge format (bv*+ba) joins the
        separate video and audio streams YouTube serves, and audio extraction
        (MP3/M4A/...) re-encodes - both are FFmpeg jobs."""
        opts = self.job.options
        fmt = opts.get("format_spec") or "bv*+ba/b"
        return "+" in fmt or bool(opts.get("audio_format"))

    def _ensure_ffmpeg(self) -> None:
        """Make FFmpeg available before yt-dlp runs, the same way the JS runtime
        is provisioned on demand. YouTube serves video and audio as separate
        streams that must be merged, so a quality download needs FFmpeg or
        yt-dlp aborts ('merging of multiple formats but ffmpeg is not
        installed'). Prefer one already present; fetch a pinned build once
        otherwise. Non-fatal: on failure _build_options falls back to a
        pre-merged format so the download still starts."""
        if self.ffmpeg_path:
            return
        from app.core.ffmpeg import ensure_ffmpeg, find_ffmpeg

        found = find_ffmpeg()
        if found is not None:
            self.ffmpeg_path = found
            return
        try:
            log.info("job %s: FFmpeg not found - fetching a pinned build (one-time)", self.job.id)
            self.ffmpeg_path = str(ensure_ffmpeg(proxy=self.proxy))
        except DownloadError as exc:
            log.warning("job %s: could not provision FFmpeg: %s", self.job.id, exc)
        except Exception:  # never let FFmpeg setup crash the job
            log.exception("job %s: unexpected error provisioning FFmpeg", self.job.id)

    def _cookie_browser(self) -> str | None:
        """Which browser to read a login from: the one chosen in Settings, or
        the auto-detected installed one. None if we can't find a browser."""
        configured = self.job.options.get("session_browser")
        if configured:
            return str(configured)
        from app.core.browser_setup import detect_cookie_browser

        return detect_cookie_browser()

    def _cookie_browser_fallbacks(self, primary: str | None) -> list[str]:
        """Other installed browsers to try when ``primary``'s cookie DB won't open."""
        from app.core.browser_setup import cookie_browser_candidates

        return [name for name in cookie_browser_candidates(primary=primary) if name != primary]

    def _download(self, *, with_cookies: bool, with_runtime: bool) -> dict[str, Any]:
        import yt_dlp

        opts = self._build_options(with_cookies=with_cookies, with_runtime=with_runtime)
        # Fast start: prefer a panel prefetch (cookies+runtime for YouTube),
        # then a plain analysis cache when this attempt stays cookie-free.
        cached = take_download_ready(
            self.job.url,
            self.proxy,
            # Prefetch usually finishes while the person picks a quality; wait
            # a bit if they confirmed instantly so we don't double-extract.
            wait=45.0 if (with_cookies or with_runtime) else 0.0,
            stop=self._stop_event,
        )
        if (
            cached is None
            and not with_cookies
            and not with_runtime
            and not self.job.options.get("hq_first")
        ):
            cached = recall_info(self.job.url, self.proxy)
        if cached is not None:
            log.info("job %s: downloading from the cached analysis", self.job.id)
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.process_ie_result(cached, download=True)
                if isinstance(info, dict):
                    return info
            except yt_dlp.utils.DownloadError as exc:
                if self._stop_event.is_set():
                    raise
                log.info(
                    "job %s: cached analysis rejected (%s); extracting fresh", self.job.id, exc
                )
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(self.job.url, download=True)
        if not isinstance(info, dict):
            raise DownloadError("No downloadable media was found at this address.")
        return info

    def _download_smart(self) -> dict[str, Any]:
        """Start the transfer as soon as the site allows, escalate on failure.

        Non-YouTube sites still prefer a cookie-free, jsless first attempt
        (seconds to first byte). YouTube currently bot-checks those anonymous
        clients, so when a browser login is configured we skip the doomed
        attempt and open with cookies + the JS runtime immediately - that is
        what `--cookies-from-browser` on the CLI is doing, and it is the only
        path that yields unthrottled formats today. Quality-first (Settings)
        also pays for the runtime up front so the full 4K ladder is visible.
        """
        import yt_dlp

        from app.core import jsruntime

        # YouTube's quality streams are separate video + audio: fetch FFmpeg
        # once, up front, so the very first download can merge them instead of
        # aborting. (The JS runtime is provisioned the same way, below.)
        if self._wants_ffmpeg():
            self._ensure_ffmpeg()

        runtime_first = bool(self.job.options.get("hq_first"))
        # YouTube needs a browser login + JS runtime today (bot check). Start
        # there automatically whenever any browser profile is available - no
        # Settings toggle, no cookies.txt, no failed anonymous attempt first.
        browser = self._cookie_browser()
        youtube_login_first = needs_js_runtime(self.job.url) and browser is not None
        want_runtime = runtime_first or youtube_login_first
        if want_runtime:
            if self._js_runtime is None:
                self._js_runtime = jsruntime.detect_js_runtime()
            if self._js_runtime is None:
                self._ensure_js_runtime()  # may download Deno once; no-op off YouTube
        try:
            return self._download(
                with_cookies=youtube_login_first,
                with_runtime=want_runtime and self._js_runtime is not None,
            )
        except yt_dlp.utils.DownloadError as exc:
            if self._stop_event.is_set():
                raise
            message = str(exc)
            # Cookie DB locked / wrong browser: try the next installed profile
            # before surfacing anything about cookies to the person.
            if youtube_login_first and _cookie_source_failed(message):
                for alt in self._cookie_browser_fallbacks(browser):
                    log.info(
                        "job %s: cookie read from %s failed; trying %s",
                        self.job.id,
                        browser,
                        alt,
                    )
                    self.job.options["session_browser"] = alt
                    try:
                        return self._download(
                            with_cookies=True,
                            with_runtime=self._js_runtime is not None,
                        )
                    except yt_dlp.utils.DownloadError as alt_exc:
                        if self._stop_event.is_set():
                            raise
                        message = str(alt_exc)
                        if not _cookie_source_failed(message):
                            raise
                        continue
            browser = self._cookie_browser()
            # Non-YouTube (or no browser yet): escalate auth walls the old way.
            add_login = (
                not youtube_login_first and browser is not None and _looks_like_auth_wall(message)
            )
            add_runtime = not want_runtime and _runtime_might_help(message)
            if not (add_login or add_runtime):
                raise  # unrecoverable (removed, geo-blocked, ...) - fail fast, no slow retry
            log.info(
                "job %s: retrying with%s%s",
                self.job.id,
                " a JS runtime" if add_runtime or add_login else "",
                f" + {browser} login" if add_login else "",
            )
            if self._js_runtime is None:
                self._js_runtime = jsruntime.detect_js_runtime()
            if self._js_runtime is None:
                self._ensure_js_runtime()  # may download Deno once; no-op off YouTube
            return self._download(
                with_cookies=youtube_login_first or add_login,
                with_runtime=self._js_runtime is not None,
            )

    def _adopt_title(self, event: dict[str, Any]) -> None:
        """Show the real video title the moment yt-dlp knows it.

        A hover-button add is queued on the tab title ("(93) YouTube",
        "Fetching title…") and normally corrected by the oEmbed lookup - but
        that lookup is best-effort, and when it loses the race or fails the
        placeholder used to sit in the list for the whole download. The
        progress hook carries the extracted metadata, so the first event
        renames the row; jobs named by a real analysis are left alone."""
        if self._title_adopted:
            return
        info = event.get("info_dict") or {}
        title = info.get("title")
        if not title:
            return
        placeholder = bool(self.job.options.get("name_from_metadata"))
        if self.job.title and not placeholder:
            return
        self._title_adopted = True
        self.job.title = str(title)
        self.db.update_job_title(self.job.id, self.job.title)

    def _adopt_size_hint(self, event: dict[str, Any]) -> None:
        """Seed an overall size from info_dict as soon as formats are known.

        Per-file progress totals arrive later (and a merge's audio track may
        not report a size until it starts), so without this the UI stays on
        "Fetching metadata…" while video data is already flowing."""
        if self._live.expected_total:
            return
        info = event.get("info_dict") or {}
        if not isinstance(info, dict):
            return
        formats = info.get("requested_formats")
        if isinstance(formats, list) and formats:
            sizes = [_format_size(fmt) for fmt in formats if isinstance(fmt, dict)]
            known = [size for size in sizes if size]
            if known:
                self._live.expected_total = sum(known)
                return
        size = info.get("filesize") or info.get("filesize_approx")
        if size:
            self._live.expected_total = int(size)

    def _hook(self, event: dict[str, Any]) -> None:
        if self._stop_event.is_set():
            raise _StopRequested
        self._adopt_title(event)
        self._adopt_size_hint(event)
        status = event.get("status")
        filename = event.get("tmpfilename") or event.get("filename") or ""
        if status == "downloading":
            self._known_files.add(filename)
            downloaded = int(event.get("downloaded_bytes") or 0)
            previous = self._live.per_file.get(filename, 0)
            self._live.per_file[filename] = downloaded
            total = event.get("total_bytes") or event.get("total_bytes_estimate")
            if total:
                self._live.totals[filename] = int(total)
            elif filename not in self._live.totals:
                # Don't overwrite a known size with None if a later hook
                # temporarily omits the field; only record "unknown" once.
                self._live.totals[filename] = None
            # Fair-speed (and pause) land here: yt-dlp's ratelimit is fixed at
            # start, so we sleep on the delta while it waits for the hook.
            delta = downloaded - previous
            if delta > 0 and self.fair_limiter is not None:
                self.fair_limiter.throttle(delta, self._stop_event)
            # In-memory only. yt-dlp calls this hook synchronously and won't
            # read the next chunk until it returns - a database write here
            # would make this job's throughput hostage to whatever else the
            # database is doing at that instant. _persister reads this same
            # state and writes it on its own thread, on its own schedule.
        elif status == "finished":
            final = event.get("total_bytes") or event.get("downloaded_bytes")
            if final:
                self._live.per_file[filename] = int(final)
                self._live.totals[filename] = int(final)

    def _postprocessor_hook(self, event: dict[str, Any]) -> None:
        if self._stop_event.is_set() and event.get("status") == "started":
            raise _StopRequested

    def _progress_snapshot(self) -> tuple[int, int | None]:
        """The current (downloaded, total) for _persister to write.

        Prefer the sum of per-file totals that are already known. A YouTube
        DASH merge often has video size early and audio size only later - the
        old rule waited for every track and left the UI stuck on "Fetching
        metadata…" for the whole video phase. When some tracks are still
        unknown, bump the shown total up to the live downloaded count so the
        progress bar never exceeds 100%."""
        downloaded = self.bytes_downloaded
        known = [size for size in self._live.totals.values() if size is not None]
        if known:
            total = sum(known)
            if any(size is None for size in self._live.totals.values()):
                total = max(total, downloaded)
            return downloaded, total
        if self._live.expected_total:
            return downloaded, max(self._live.expected_total, downloaded)
        return downloaded, None

    # ---------------------------------------------------------- completion

    def _finalize(self, info: dict[str, Any]) -> JobStatus:
        self._persister.stop()
        filepath = self._final_filepath(info)
        if filepath is None or not filepath.exists():
            return self._finish_failed("yt-dlp finished but the output file was not found")
        size = filepath.stat().st_size
        self.job.filename = filepath.name
        self.db.update_job_filename(self.job.id, filepath.name)
        self.db.update_job_total(self.job.id, size)
        self.db.update_job_downloaded(self.job.id, size)
        # Adopt (and persist) the real title when the job had none - or when it
        # was queued on a placeholder because the analysis was skipped.
        placeholder = bool(self.job.options.get("name_from_metadata"))
        if info.get("title") and (not self.job.title or placeholder):
            self.job.title = str(info["title"])
            self.db.update_job_title(self.job.id, self.job.title)
        self.db.set_job_status(self.job.id, JobStatus.COMPLETED)
        return JobStatus.COMPLETED

    def _final_filepath(self, info: dict[str, Any]) -> Path | None:
        downloads = info.get("requested_downloads") or []
        if downloads and downloads[0].get("filepath"):
            return Path(downloads[0]["filepath"])
        if info.get("filepath"):
            return Path(str(info["filepath"]))
        # Fallback: the newest plausible file matching our output stem.
        base = Path(self.job.filename).stem
        candidates = [
            p
            for p in Path(self.job.dest_dir).glob(f"{naming.sanitize_filename(base)}.*")
            if not p.name.endswith((".part", ".ytdl"))
        ]
        return max(candidates, key=lambda p: p.stat().st_mtime, default=None)

    def _settle_stopped(self) -> JobStatus:
        # Stop first: guarantees the background thread is dead before the
        # explicit zero-write below, so a cancelled job can never have a stale
        # non-zero snapshot land after it.
        self._persister.stop()
        if self._cancelled:
            self._remove_partials()
            self.db.update_job_downloaded(self.job.id, 0)
            self.db.set_job_status(self.job.id, JobStatus.CANCELLED)
            return JobStatus.CANCELLED
        # yt-dlp resumes its own .part files on the next run.
        self.db.set_job_status(self.job.id, JobStatus.PAUSED)
        return JobStatus.PAUSED

    def _remove_partials(self) -> None:
        """Delete everything this job left in the destination folder.

        yt-dlp scatters more than the output file around: ``.part`` for the
        partial download, ``.part-FragNNN`` for native HLS/DASH fragments,
        ``.ytdl`` for resume state, and - when video and audio are merged - a
        ``.temp.<ext>`` that ffmpeg is midway through writing when cancel kills
        it. Only the first and the last were being removed, so a cancelled 4K
        video could leave several gigabytes of scratch behind.
        """
        dest = Path(self.job.dest_dir)
        stems = {Path(name).name for name in self._known_files if name}
        for stem in stems:
            self._unlink(dest / stem, dest / f"{stem}.ytdl")
            self._unlink(*dest.glob(f"{glob.escape(stem)}.part*"))
        for base in self._output_bases(stems):
            self._unlink(*dest.glob(f"{glob.escape(base)}.temp.*"))

    def _output_bases(self, stems: set[str]) -> set[str]:
        """The names ffmpeg merges into, i.e. the per-format files' stems with
        yt-dlp's format-id suffix (``Clip.f137.mp4`` -> ``Clip``) dropped."""
        bases = {Path(self.job.filename).stem}
        bases |= {_FORMAT_SUFFIX.sub("", Path(stem).stem) for stem in stems}
        return {base for base in bases if base}

    @staticmethod
    def _unlink(*paths: Path) -> None:
        for path in paths:
            # A just-killed ffmpeg can still hold its output open on Windows;
            # one undeletable leftover must not fail the cancel.
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)

    def _finish_failed(self, message: str) -> JobStatus:
        self._persister.stop()
        log.warning("smart job %s failed: %s", self.job.id, message)
        self.db.set_job_status(self.job.id, JobStatus.FAILED, error=message)
        return JobStatus.FAILED
