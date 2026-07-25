"""Opt-in live URL latency checks for the smart engine.

Skipped by default (and in CI). Run locally:

    GRABLINE_NETWORK_TESTS=1 .venv/bin/pytest app/tests/test_smart_url_regression.py -m network -q
"""

from __future__ import annotations

import os
import time

import pytest

from app.engines.smart import (
    MediaInfo,
    PlaylistInfo,
    SmartEngine,
    prefetch_download_ready,
    take_download_ready,
)
from app.tests.smart_url_suite import SUITE, SuiteUrl

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.environ.get("GRABLINE_NETWORK_TESTS") != "1",
        reason="set GRABLINE_NETWORK_TESTS=1 to run live URL checks",
    ),
]


def _assert_inspect(entry: SuiteUrl) -> MediaInfo | PlaylistInfo:
    engine = SmartEngine()
    engine.warm_up()
    started = time.perf_counter()
    result = engine.inspect(entry.url)
    elapsed = time.perf_counter() - started
    assert elapsed < entry.inspect_budget, (
        f"{entry.name}: inspect took {elapsed:.1f}s (budget {entry.inspect_budget}s)"
    )
    if entry.playlist or entry.live_smoke:
        assert isinstance(result, MediaInfo | PlaylistInfo)
        return result
    assert isinstance(result, MediaInfo)
    assert result.options, f"{entry.name}: no quality options"
    return result


@pytest.mark.parametrize("entry", SUITE, ids=[e.name for e in SUITE])
def test_suite_inspect_budget(entry: SuiteUrl) -> None:
    if entry.optional:
        pytest.xfail(f"{entry.name} is optional (site/extractor flaky)")
    _assert_inspect(entry)


def test_youtube_prefetch_lands_before_long_wait() -> None:
    """Panel-style path: JS-less inspect, then download-ready prefetch finishes."""
    entry = next(e for e in SUITE if e.name == "yt-regular")
    engine = SmartEngine()
    engine.warm_up()
    t0 = time.perf_counter()
    info = engine.inspect(entry.url)
    inspect_s = time.perf_counter() - t0
    assert isinstance(info, MediaInfo) and info.options
    assert inspect_s < entry.inspect_budget

    t1 = time.perf_counter()
    prefetch_download_ready(entry.url)
    ready = take_download_ready(entry.url, wait=90.0)
    prefetch_s = time.perf_counter() - t1
    assert ready is not None and ready.get("formats"), f"prefetch failed after {prefetch_s:.1f}s"
    # Prefetch is the slow cookies+runtime pass; it must still finish in a
    # single extract's time, not two stacked ones.
    assert prefetch_s < 120.0, f"prefetch took {prefetch_s:.1f}s"
