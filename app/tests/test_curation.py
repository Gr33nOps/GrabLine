from __future__ import annotations

from typing import Any

import pytest

from app.core.errors import DownloadError
from app.engines.smart import (
    MediaInfo,
    SmartEngine,
    curate_formats,
    friendly_error,
    generic_quality_options,
    needs_js_runtime,
    option_for_label,
    parse_playlist,
)

MB = 1024 * 1024


def _youtube_like_info() -> dict[str, Any]:
    """A trimmed-down but structurally faithful yt-dlp info dict."""
    return {
        "id": "abc123",
        "title": "Test Video",
        "formats": [
            # audio-only
            {
                "format_id": "140",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "abr": 128,
                "filesize": 3 * MB,
                "ext": "m4a",
            },
            {
                "format_id": "251",
                "vcodec": "none",
                "acodec": "opus",
                "abr": 160,
                "filesize": 4 * MB,
                "ext": "webm",
            },
            # video-only (heights slightly off the ladder, as in real life)
            {
                "format_id": "248",
                "vcodec": "vp9",
                "acodec": "none",
                "height": 1088,
                "tbr": 4000,
                "filesize": 80 * MB,
                "ext": "webm",
            },
            {
                "format_id": "247",
                "vcodec": "vp9",
                "acodec": "none",
                "height": 720,
                "tbr": 2000,
                "filesize": 40 * MB,
                "ext": "webm",
            },
            # combined (already has audio)
            {
                "format_id": "18",
                "vcodec": "avc1",
                "acodec": "mp4a.40.2",
                "height": 360,
                "tbr": 700,
                "filesize": 20 * MB,
                "ext": "mp4",
            },
        ],
    }


def test_curated_labels_and_order():
    options = curate_formats(_youtube_like_info())
    assert [o.label for o in options] == ["Best", "1080p", "720p", "360p", "MP3", "M4A", "FLAC"]


def test_video_size_estimates_add_audio_when_needed():
    options = {o.label: o for o in curate_formats(_youtube_like_info())}
    # video-only 1080p (80 MB) + best audio (4 MB opus)
    assert options["1080p"].estimated_size == 84 * MB
    assert options["720p"].estimated_size == 44 * MB
    # the 360p format already has audio: no double counting
    assert options["360p"].estimated_size == 20 * MB
    assert options["Best"].estimated_size == 84 * MB


def test_format_specs():
    options = {o.label: o for o in curate_formats(_youtube_like_info())}
    assert options["Best"].format_spec == "bv*+ba/b"
    # Tier selectors fall back to best-available so a video lacking that exact
    # tier still downloads instead of failing "Requested format is not available".
    assert options["1080p"].format_spec == "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"
    assert options["MP3"].format_spec == "ba/b"
    assert options["MP3"].audio_format == "mp3"
    assert options["M4A"].format_spec == "ba[ext=m4a]/ba/b"
    assert options["MP3"].estimated_size == 4 * MB


def test_audio_only_source_still_offers_audio():
    info = {
        "formats": [
            {"format_id": "0", "vcodec": "none", "acodec": "mp3", "abr": 192, "filesize": 5 * MB}
        ]
    }
    options = curate_formats(info)
    assert [o.label for o in options] == ["MP3", "M4A", "FLAC"]


def test_empty_formats_yield_no_options():
    assert curate_formats({"formats": []}) == ()


def test_parse_playlist_flat_listing():
    info = {
        "_type": "playlist",
        "title": "Lecture Series",
        "uploader": "Prof X",
        "webpage_url": "https://tube.example/playlist?list=1",
        "entries": [
            {"url": "https://tube.example/watch?v=a", "title": "Intro", "duration": 60},
            None,  # deleted video
            {"url": "abc123", "ie_key": "Youtube", "title": "Part 2"},
            {"url": "notaurl", "ie_key": "Other", "title": "skipped"},
        ],
    }
    playlist = parse_playlist(info)
    assert playlist is not None
    assert playlist.title == "Lecture Series"
    assert playlist.uploader == "Prof X"
    assert [entry.url for entry in playlist.entries] == [
        "https://tube.example/watch?v=a",
        "https://www.youtube.com/watch?v=abc123",
    ]
    assert playlist.entries[0].duration == 60
    # original positions survive the gaps
    assert [entry.index for entry in playlist.entries] == [1, 3]


def test_parse_playlist_returns_none_for_videos():
    assert parse_playlist({"_type": "video", "formats": []}) is None
    assert parse_playlist({"id": "x", "formats": []}) is None


def test_generic_quality_options_shape():
    options = generic_quality_options()
    labels = [option.label for option in options]
    assert labels == ["Best", "1080p", "720p", "480p", "MP3", "M4A", "FLAC"]
    assert options[-1].audio_format == "flac"
    assert options[-2].audio_format == "m4a"
    assert all(option.format_spec for option in options)


def test_option_for_label_prefers_curated_options():
    curated = curate_formats(_youtube_like_info())
    option = option_for_label("1080p", curated)
    assert option is not None and option in curated
    assert option.height == 1080


def test_option_for_label_falls_back_to_generic_tiers():
    # No curated list at all (F1.3 handoff before inspection details exist).
    option = option_for_label("720p")
    assert option is not None
    assert option.format_spec == "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b"
    audio = option_for_label("mp3")
    assert audio is not None and audio.audio_format == "mp3"


def test_option_for_label_best_and_unknown():
    curated = curate_formats(_youtube_like_info())
    best = option_for_label("BEST", curated)
    assert best is not None and best.label == "Best"
    assert option_for_label("8888p", curated) is None


def test_friendly_errors():
    assert "private" in friendly_error("ERROR: [youtube] abc: Private video").lower()
    assert "age-restricted" in friendly_error("Sign in to confirm your age")
    assert "DRM" in friendly_error("This video is DRM protected")
    geo_message = "The uploader has not made this video available in your country"
    assert "region-blocked" in friendly_error(geo_message)
    assert "cookie" in friendly_error("Could not copy Chrome cookie database").lower()
    # Format-not-available: cookies are usually the cause, not the fix, so the
    # message must steer toward turning session OFF, never on.
    fmt = friendly_error("ERROR: [youtube] abc: Requested format is not available").lower()
    assert "turn it off" in fmt
    bot = friendly_error("ERROR: [youtube] abc: Sign in to confirm you're not a bot").lower()
    assert "browser" in bot or "cookie" in bot
    # YouTube often uses a curly apostrophe; matching must not depend on ASCII.
    curly = "ERROR: [youtube] abc: Sign in to confirm you\u2019re not a bot"
    assert "browser" in friendly_error(curly).lower() or "cookie" in friendly_error(curly).lower()
    browse_404 = (
        "ERROR: [soundcloud:user] discover: Unable to download JSON metadata: "
        "HTTP Error 404: Not Found"
    )
    assert "browse" in friendly_error(browse_404)
    # unknown errors: first line, ERROR: prefix stripped, no traceback
    assert friendly_error("ERROR: something odd\nTraceback...") == "something odd"


def test_inspect_youtube_uses_browser_login_automatically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """YouTube analysis must open with the browser login on its own - callers
    still pass use_session=False, but the person never sees a bot-check."""
    engine = SmartEngine()
    calls: list[tuple[bool, bool]] = []

    def fake_extract(url: str, **kwargs: Any) -> dict[str, Any]:
        use_session = bool(kwargs.get("use_session"))
        with_runtime = bool(kwargs.get("with_runtime"))
        calls.append((use_session, with_runtime))
        assert use_session  # forced for YouTube even when the caller said False
        return {
            "id": "x",
            "title": "Signed in",
            "webpage_url": url,
            "formats": [
                {
                    "format_id": "18",
                    "ext": "mp4",
                    "height": 360,
                    "url": "https://example.com/v.mp4",
                    "vcodec": "avc1",
                    "acodec": "mp4a",
                }
            ],
        }

    monkeypatch.setattr(engine, "_extract_info", fake_extract)
    result = engine.inspect(
        "https://youtu.be/x", use_session=False, session_browser="firefox"
    )
    assert isinstance(result, MediaInfo)
    assert result.title == "Signed in"
    assert calls == [(True, True)]


def test_inspect_reuses_a_recent_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Analysis is the slow part of adding a video; adding the same URL twice
    ('Download again', or answering yes to the duplicate prompt) must reuse it
    rather than redo the whole extraction."""
    engine = SmartEngine()
    calls: list[str] = []

    def fake(url: str, **kwargs: Any) -> MediaInfo:
        calls.append(url)
        return MediaInfo(
            url=url,
            id="v",
            title="Video",
            uploader=None,
            duration=None,
            thumbnail_url=None,
            options=(),
        )

    monkeypatch.setattr(engine, "_inspect_uncached", fake)

    first = engine.inspect("https://youtu.be/abc")
    second = engine.inspect("https://youtu.be/abc")
    assert calls == ["https://youtu.be/abc"]  # analysed once, then reused
    assert second is first

    # A different URL analyses afresh. use_session=True is a no-op for YouTube
    # (session is forced anyway), so the same URL still hits the cache.
    engine.inspect("https://youtu.be/xyz")
    engine.inspect("https://youtu.be/abc", use_session=True)
    assert len(calls) == 2

    # A stale entry is re-analysed rather than served forever.
    monkeypatch.setattr(SmartEngine, "INSPECT_TTL", -1.0)
    engine.inspect("https://youtu.be/abc")
    assert len(calls) == 3


def test_needs_js_runtime_only_for_youtube_or_a_session() -> None:
    assert needs_js_runtime("https://www.youtube.com/watch?v=a")
    assert needs_js_runtime("https://youtu.be/a")
    assert needs_js_runtime("https://music.youtube.com/watch?v=a")
    assert not needs_js_runtime("https://example.com/clip.mp4")
    # A browser session can push yt-dlp onto a JS-dependent client anywhere.
    assert needs_js_runtime("https://example.com/clip.mp4", use_session=True)


def test_provision_js_runtime_fetches_only_for_urls_that_need_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The download path (and the analysis *fallback*) provision a runtime for
    YouTube URLs, fetching Deno on demand - and never for an ordinary file."""
    from app.core import jsruntime
    from app.engines import smart

    monkeypatch.setattr(jsruntime, "detect_js_runtime", lambda *a, **k: None)
    fetched: list[str] = []

    def fake_ensure_deno(**kwargs: Any) -> str:
        fetched.append("deno")
        return "/managed/deno"

    monkeypatch.setattr(jsruntime, "ensure_deno", fake_ensure_deno)

    assert smart.provision_js_runtime("https://youtu.be/abc") == ("deno", "/managed/deno")
    assert fetched == ["deno"]  # YouTube gets one fetched on demand

    fetched.clear()
    assert smart.provision_js_runtime("https://example.com/clip.mp4") is None
    assert fetched == []  # an ordinary file never triggers a 40 MB download


def test_provisioning_failure_never_breaks_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import jsruntime
    from app.engines import smart

    monkeypatch.setattr(jsruntime, "detect_js_runtime", lambda *a, **k: None)

    def boom(**kwargs: Any) -> str:
        raise DownloadError("no network")

    monkeypatch.setattr(jsruntime, "ensure_deno", boom)
    # Best effort: analysis carries on without a runtime rather than failing.
    assert smart.provision_js_runtime("https://youtu.be/abc") is None


def test_youtube_analysis_uses_runtime_with_browser_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """YouTube analysis always attaches the browser login (and the JS runtime
    those clients need) so a bot-check never reaches the person."""
    engine = SmartEngine()
    calls: list[tuple[bool, bool]] = []

    good = {
        "id": "v",
        "title": "T",
        "formats": [
            {
                "format_id": "18",
                "vcodec": "avc1",
                "acodec": "mp4a",
                "height": 360,
                "tbr": 700,
                "filesize": MB,
                "ext": "mp4",
            }
        ],
    }

    def fake_extract(url: str, *, with_runtime: bool, use_session: bool, **kwargs: Any) -> dict:
        calls.append((use_session, with_runtime))
        return good

    monkeypatch.setattr(engine, "_extract_info", fake_extract)
    info = engine.inspect("https://youtu.be/fast", use_session=False)
    assert isinstance(info, MediaInfo) and info.options
    assert calls == [(True, True)]

    # Non-YouTube still stays jsless unless a session is requested.
    calls.clear()
    info = engine.inspect("https://vimeo.com/clip", use_session=False)
    assert isinstance(info, MediaInfo)
    assert calls == [(False, False)]

    # A hard failure on YouTube is not endlessly retried (already on the
    # cookies+runtime path from the first attempt).
    calls.clear()

    def hard_error(url: str, *, with_runtime: bool, use_session: bool, **kwargs: Any) -> dict:
        calls.append((use_session, with_runtime))
        raise DownloadError("This video is private - its owner restricted access.")

    monkeypatch.setattr(engine, "_extract_info", hard_error)
    with pytest.raises(DownloadError):
        engine.inspect("https://youtu.be/private")
    assert calls == [(True, True)]
