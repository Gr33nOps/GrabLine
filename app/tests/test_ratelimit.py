from __future__ import annotations

import threading
import time

from app.core.ratelimit import RateLimiter


def test_unlimited_never_sleeps():
    limiter = RateLimiter(0)
    started = time.monotonic()
    for _ in range(1000):
        limiter.throttle(10_000_000)
    assert time.monotonic() - started < 0.5


def test_burst_is_free_then_throttles():
    limiter = RateLimiter(1_000_000)  # 1 MB/s, bucket starts with 1 MB
    started = time.monotonic()
    limiter.throttle(500_000)
    limiter.throttle(500_000)  # exactly the burst: still free
    assert time.monotonic() - started < 0.2
    limiter.throttle(500_000)  # 0.5 s of debt
    elapsed = time.monotonic() - started
    assert 0.4 <= elapsed < 2.0


def test_set_rate_zero_lifts_the_cap():
    limiter = RateLimiter(1000)
    limiter.set_rate(0)
    started = time.monotonic()
    limiter.throttle(10_000_000)
    assert time.monotonic() - started < 0.2


def test_negative_amounts_ignored():
    limiter = RateLimiter(1000)
    limiter.throttle(0)
    limiter.throttle(-5)  # no crash, no sleep


def test_a_stop_event_cuts_the_wait_short():
    """A tight cap owes seconds of sleep per chunk. Pause and Cancel have to
    land during that sleep, or the download keeps going long after the click."""
    limiter = RateLimiter(1000)  # 1 KB/s: the chunk below owes ~9 seconds
    stop = threading.Event()
    threading.Timer(0.1, stop.set).start()

    started = time.monotonic()
    limiter.throttle(10_000, stop)
    assert time.monotonic() - started < 1.0


def test_lifting_the_cap_wakes_a_waiting_worker():
    """Setting the limit back to unlimited must not leave every worker asleep
    on the debt it owed at the old rate."""
    limiter = RateLimiter(1000)
    threading.Timer(0.1, limiter.set_rate, [0]).start()

    started = time.monotonic()
    limiter.throttle(10_000)
    assert time.monotonic() - started < 1.0


def test_the_full_debt_is_still_paid_when_nothing_interrupts():
    """Slicing the sleep must not slice the cap: 2 KB at 1 KB/s is 2 seconds
    of debt however many naps it takes."""
    limiter = RateLimiter(1000)
    started = time.monotonic()
    limiter.throttle(3000)  # 1 KB of burst is free, 2 KB is owed
    assert 1.8 <= time.monotonic() - started < 3.5
