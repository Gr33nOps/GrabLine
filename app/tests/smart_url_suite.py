"""Concrete smart-engine URLs for local latency / regression checks.

Shared by the opt-in network pytest module and ``scripts/bench_smart_urls.py``.
Incomplete Twitch/social links are intentionally omitted until we have working
examples.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SuiteUrl:
    name: str
    url: str
    #: Soft budget (seconds) for JS-less / normal ``inspect`` to return formats.
    inspect_budget: float
    #: True when inspect may escalate to cookies (age gate, etc.).
    may_need_session: bool = False
    #: Live / is_live smoke: just confirm we don't hang forever.
    live_smoke: bool = False
    #: Playlist: flat listing is enough (no per-entry formats).
    playlist: bool = False
    #: Soft entry: bench warns but does not fail (site/extractor currently flaky).
    optional: bool = False


SUITE: tuple[SuiteUrl, ...] = (
    # Budgets are soft ceilings for a warm process on a normal link. First-hit
    # cold starts (DNS / IPv6 probe / CDN) can spike; the important signal is
    # JS-less analysis staying well under the old 26-87s cookies+runtime path.
    SuiteUrl("yt-regular", "https://www.youtube.com/watch?v=dQw4w9WgXcQ", 25.0),
    SuiteUrl("yt-4k-hdr", "https://www.youtube.com/watch?v=LXb3EKWsInQ", 25.0),
    SuiteUrl("yt-8k", "https://www.youtube.com/watch?v=1La4QzGeaaQ", 25.0),
    SuiteUrl("yt-60fps", "https://www.youtube.com/watch?v=aqz-KE-bpKQ", 25.0),
    SuiteUrl("yt-shorts", "https://www.youtube.com/shorts/jNQXAC9IVRw", 25.0),
    SuiteUrl(
        "yt-age",
        "https://www.youtube.com/watch?v=SkRSXFQerZs",
        60.0,
        may_need_session=True,
    ),
    SuiteUrl(
        "yt-playlist",
        "https://www.youtube.com/playlist?list=PLBCF2DAC6FFB574DE",
        25.0,
        playlist=True,
    ),
    SuiteUrl(
        "yt-live-nasa",
        "https://www.youtube.com/@NASA/live",
        30.0,
        live_smoke=True,
    ),
    # Vimeo currently needs a working OAuth client in yt-dlp; keep as optional.
    SuiteUrl("vimeo", "https://vimeo.com/347119375", 30.0, optional=True),
    SuiteUrl("dailymotion", "https://www.dailymotion.com/video/x84sh87", 25.0),
    SuiteUrl(
        "tiktok",
        "https://www.tiktok.com/@scout2015/video/6718335390845095173",
        25.0,
    ),
    # Artist pages can be thousands of flat entries; keep optional for now.
    SuiteUrl(
        "soundcloud",
        "https://soundcloud.com/monstercat",
        90.0,
        playlist=True,
        optional=True,
    ),
    SuiteUrl("bilibili", "https://www.bilibili.com/video/BV1xx411c7mD/", 30.0),
    SuiteUrl("twitch-live", "https://www.twitch.tv/ninja", 30.0, live_smoke=True, optional=True),
)
