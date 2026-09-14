"""Performance guards for the add path (issue: "YouTube analysis feels slow").

These pin costs that were measured and removed, so they cannot creep back:
building an httpx client no longer re-parses the CA bundle, and asking whether
a URL is claimed by a yt-dlp extractor no longer re-runs ~1750 regexes.

They assert generous ceilings (10-100x the measured figure), so they fail on a
regression rather than on a slow CI runner.
"""

from __future__ import annotations

import time

from app.core import net
from app.engines.smart import SmartEngine


def test_matches_is_memoized_per_url() -> None:
    engine = SmartEngine()
    engine._extractor_classes()  # the one-time list build is not what's measured
    url = "https://example.invalid/not-a-video.zip"  # worst case: nothing claims it

    engine.matches(url)  # prime
    start = time.perf_counter()
    for _ in range(200):
        engine.matches(url)
    per_call = (time.perf_counter() - start) / 200
    assert per_call < 0.001, f"{per_call * 1000:.2f} ms per matches() - the memo is not working"


def test_matches_still_answers_correctly_through_the_memo() -> None:
    engine = SmartEngine()
    assert engine.matches("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is True
    assert engine.matches("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is True  # cached
    assert engine.matches("https://example.invalid/file.zip") is False
    assert engine.matches("https://example.invalid/file.zip") is False


def test_the_match_memo_cannot_grow_without_bound() -> None:
    engine = SmartEngine()
    engine._extractors = []  # no extractors: every answer is False, instantly
    for i in range(SmartEngine._MATCH_CACHE_CAP + 50):
        engine.matches(f"https://example.invalid/{i}")
    assert len(engine._matched) <= SmartEngine._MATCH_CACHE_CAP


def test_repeated_client_builds_do_not_reparse_the_ca_bundle() -> None:
    net.ssl_context(verify=True)
    net.ipv6_broken()
    start = time.perf_counter()
    for _ in range(20):
        net.build_client().close()
    per_client = (time.perf_counter() - start) / 20
    assert per_client < 0.010, f"{per_client * 1000:.1f} ms per client - CA bundle re-parsed?"
