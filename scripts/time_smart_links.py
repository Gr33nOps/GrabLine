#!/usr/bin/env python3
"""One-by-one timing for dialog / start / finish on a fixed link list.

Phases:
  dialog  – SmartEngine.inspect (quality panel ready)
  start   – download-ready prefetch + yt-dlp until first progress byte
  finish  – download completes (capped at 360p / worst so 4K/8K don't run for hours)

Live URLs only measure dialog (+ note). Incomplete / homepage URLs are recorded
as skipped with a reason.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.engines.smart import (  # noqa: E402
    MediaInfo,
    PlaylistInfo,
    SmartEngine,
    prefetch_download_ready,
    take_download_ready,
)


@dataclass
class LinkSpec:
    name: str
    url: str
    kind: str = "video"  # video | playlist | live | skip
    note: str = ""


LINKS: list[LinkSpec] = [
    LinkSpec("yt-regular", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    LinkSpec("yt-4k-hdr", "https://www.youtube.com/watch?v=LXb3EKWsInQ"),
    LinkSpec("yt-8k", "https://www.youtube.com/watch?v=1La4QzGeaaQ"),
    LinkSpec("yt-60fps", "https://www.youtube.com/watch?v=aqz-KE-bpKQ"),
    LinkSpec("yt-shorts", "https://www.youtube.com/shorts/jNQXAC9IVRw"),
    LinkSpec("yt-live-nasa", "https://www.youtube.com/@NASA/live", kind="live"),
    LinkSpec(
        "yt-playlist",
        "https://www.youtube.com/playlist?list=PLBCF2DAC6FFB574DE",
        kind="playlist",
        note="Original PL590L5… 404s; using working public playlist",
    ),
    LinkSpec(
        "yt-music-home",
        "https://music.youtube.com/",
        kind="skip",
        note="Homepage, not a single video/playlist URL",
    ),
    LinkSpec("yt-age", "https://www.youtube.com/watch?v=SkRSXFQerZs"),
    LinkSpec("twitch-live", "https://www.twitch.tv/ninja", kind="live"),
    LinkSpec(
        "twitch-vod",
        "https://www.twitch.tv/videos/",
        kind="skip",
        note="Incomplete URL in request (no video id)",
    ),
    LinkSpec(
        "twitch-clip",
        "https://clips.twitch.tv/",
        kind="skip",
        note="Incomplete URL in request (no clip id)",
    ),
    LinkSpec("vimeo", "https://vimeo.com/148751763"),
    LinkSpec("dailymotion", "https://www.dailymotion.com/video/x84sh87"),
    LinkSpec(
        "tiktok",
        "https://www.tiktok.com/@scout2015/video/6718335390845095173",
    ),
    LinkSpec(
        "reddit",
        "https://www.reddit.com/r/nextfuckinglevel/",
        kind="skip",
        note="Subreddit page, not a concrete media post URL",
    ),
    LinkSpec(
        "soundcloud",
        "https://soundcloud.com/monstercat",
        kind="playlist",
        note="Artist page → flat playlist; finish = first entry only",
    ),
    LinkSpec("bilibili", "https://www.bilibili.com/video/BV1xx411c7mD/"),
    LinkSpec(
        "facebook",
        "https://www.facebook.com/watch/",
        kind="skip",
        note="Incomplete URL in request",
    ),
    LinkSpec(
        "instagram",
        "https://www.instagram.com/reel/",
        kind="skip",
        note="Incomplete URL in request",
    ),
    LinkSpec("x-twitter", "https://x.com/", kind="skip", note="Homepage, not a post URL"),
]


@dataclass
class TimingRow:
    name: str
    url: str
    kind: str
    status: str
    dialog_s: float | None = None
    start_s: float | None = None
    finish_s: float | None = None
    title: str = ""
    bytes: int = 0
    note: str = ""
    error: str = ""
    extras: dict = field(default_factory=dict)


#: Cap download quality so finish times stay measurable.
_FORMAT = "best[height<=360]/worst"


def _download_timed(url: str, dest: Path) -> tuple[float, float, int, str]:
    """Return (start_s, finish_s, bytes, title).

    start_s = time until first progress byte after YoutubeDL begins.
    finish_s = wall time for the whole download call.
    Prefetch should already have warmed cookies/runtime for YouTube.
    """
    import yt_dlp

    from app.core import jsruntime
    from app.core.browser_setup import detect_cookie_browser
    from app.engines import smart

    first_byte = threading.Event()
    t_first: list[float] = []
    t_origin = [time.perf_counter()]

    def hook(d: dict) -> None:
        if d.get("status") == "downloading" and not first_byte.is_set():
            t_first.append(time.perf_counter() - t_origin[0])
            first_byte.set()

    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "format": _FORMAT,
        "outtmpl": {"default": str(dest / "%(id)s.%(ext)s")},
        "retries": 2,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 8,
        "progress_hooks": [hook],
        "socket_timeout": 20,
    }
    smart._apply_network_guards(opts, None)
    # YouTube: always attach runtime; cookies when a browser profile exists.
    if smart.needs_js_runtime(url):
        runtime = jsruntime.detect_js_runtime()
        if runtime is None:
            try:
                runtime = ("deno", str(jsruntime.ensure_deno()))
            except Exception:
                runtime = None
        if runtime:
            name, path = runtime
            opts["js_runtimes"] = {name: {"path": path}}
            opts["remote_components"] = ["ejs:github"]
        browser = detect_cookie_browser()
        if browser:
            opts["cookiesfrombrowser"] = (browser,)

    cached = take_download_ready(url, wait=90.0)
    info: dict | None = None
    with yt_dlp.YoutubeDL(opts) as ydl:
        if cached is not None:
            try:
                raw = ydl.process_ie_result(cached, download=True)
                info = raw if isinstance(raw, dict) else None
            except yt_dlp.utils.DownloadError:
                # Prefetch URLs can 403; fresh extract with the same opts.
                first_byte.clear()
                t_first.clear()
                t_origin[0] = time.perf_counter()
                raw = ydl.extract_info(url, download=True)
                info = raw if isinstance(raw, dict) else None
        else:
            raw = ydl.extract_info(url, download=True)
            info = raw if isinstance(raw, dict) else None
    finish_s = time.perf_counter() - t_origin[0]
    start_s = t_first[0] if t_first else finish_s
    title = ""
    nbytes = 0
    if isinstance(info, dict):
        title = str(info.get("title") or "")[:80]
        req = info.get("requested_downloads") or [info]
        for item in req:
            fp = item.get("filepath") or item.get("_filename")
            if fp and Path(fp).is_file():
                nbytes += Path(fp).stat().st_size
    return start_s, finish_s, nbytes, title


def time_one(spec: LinkSpec, engine: SmartEngine) -> TimingRow:
    row = TimingRow(name=spec.name, url=spec.url, kind=spec.kind, status="ok", note=spec.note)
    if spec.kind == "skip":
        row.status = "skipped"
        return row

    # --- dialog ---
    try:
        t0 = time.perf_counter()
        result = engine.inspect(spec.url)
        row.dialog_s = round(time.perf_counter() - t0, 2)
        if isinstance(result, MediaInfo):
            row.title = result.title[:80]
            if not result.options and spec.kind == "video":
                row.status = "no-formats"
                return row
        elif isinstance(result, PlaylistInfo):
            row.title = result.title[:80]
            row.extras["entries"] = len(result.entries)
        else:
            row.status = "bad-type"
            return row
    except Exception as exc:
        row.status = "dialog-error"
        row.error = str(exc)[:200]
        return row

    if spec.kind == "live":
        row.status = "live-smoke"
        row.note = (row.note + "; " if row.note else "") + "live — not fully downloaded"
        return row

    # Resolve a concrete media URL for playlists (first entry).
    download_url = spec.url
    if spec.kind == "playlist" and isinstance(result, PlaylistInfo) and result.entries:
        download_url = result.entries[0].url
        row.extras["download_entry"] = download_url
        row.note = (row.note + "; " if row.note else "") + "finish = first playlist entry"

    # --- prefetch (overlaps with "user in dialog") ---
    try:
        t1 = time.perf_counter()
        prefetch_download_ready(download_url)
        ready = take_download_ready(download_url, wait=120.0)
        prefetch_s = round(time.perf_counter() - t1, 2)
        row.extras["prefetch_s"] = prefetch_s
        if ready is None and smart_needs_prefetch(download_url):
            row.note = (row.note + "; " if row.note else "") + "prefetch empty — live extract"
    except Exception as exc:
        row.extras["prefetch_error"] = str(exc)[:120]

    # --- start + finish ---
    try:
        with tempfile.TemporaryDirectory(prefix="gl-time-") as tmp:
            start_s, finish_s, nbytes, title = _download_timed(download_url, Path(tmp))
        row.start_s = round(start_s, 2)
        row.finish_s = round(finish_s, 2)
        row.bytes = nbytes
        if title and not row.title:
            row.title = title
        row.extras["format_cap"] = _FORMAT
    except Exception as exc:
        row.status = "download-error"
        row.error = str(exc)[:200]
    return row


def smart_needs_prefetch(url: str) -> bool:
    from app.engines.smart import needs_js_runtime

    return needs_js_runtime(url)


def main() -> int:
    only = {x.strip() for x in (sys.argv[1:] or []) if x.strip()}
    specs = [s for s in LINKS if not only or s.name in only]
    engine = SmartEngine()
    print("warming extractors…", flush=True)
    engine.warm_up()
    rows: list[TimingRow] = []
    for i, spec in enumerate(specs, 1):
        print(f"\n[{i}/{len(specs)}] {spec.name}  {spec.url}", flush=True)
        row = time_one(spec, engine)
        rows.append(row)
        print(
            f"  status={row.status} dialog={row.dialog_s} start={row.start_s} "
            f"finish={row.finish_s} bytes={row.bytes} err={row.error[:60] if row.error else ''}",
            flush=True,
        )

    out = ROOT / "dist" / "smart_link_timings.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "format_cap": _FORMAT,
        "phases": {
            "dialog_s": "SmartEngine.inspect → quality dialog can open",
            "start_s": "seconds until first downloaded byte after download begins",
            "finish_s": "wall time for capped-quality download to complete",
        },
        "rows": [asdict(r) for r in rows],
    }
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
