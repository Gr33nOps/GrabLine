"""Global download speed limiter (F1.8): a thread-safe token bucket shared by
every worker connection, so the cap applies to the whole app, not per stream.
"""

from __future__ import annotations

import threading
import time

UNLIMITED = 0


class RateLimiter:
    """Token bucket in bytes/second. A rate of 0 means unlimited.

    Workers call :meth:`throttle` with the size of the chunk they just moved;
    the call sleeps exactly long enough to keep the long-run average at the
    configured rate. The bucket holds at most one second of burst.
    """

    #: Longest single sleep before the debt is re-examined. A tight cap owes
    #: seconds per chunk, and a worker sitting inside one long sleep ignores
    #: Pause and Cancel for that whole time - the download that "won't stop".
    SLICE = 0.2

    def __init__(self, rate: int = UNLIMITED) -> None:
        self._lock = threading.Lock()
        self._rate = max(0, rate)
        self._tokens = float(self._rate)
        self._updated = time.monotonic()

    @property
    def rate(self) -> int:
        with self._lock:
            return self._rate

    def set_rate(self, rate: int) -> None:
        rate = max(0, rate)
        with self._lock:
            # No-op when unchanged: the fair-speed scheduler calls this every
            # pass, and resetting ``_updated`` / clamping tokens each time
            # made concurrent downloads see-saw instead of holding a steady
            # equal share.
            if rate == self._rate:
                return
            self._rate = rate
            self._tokens = min(self._tokens, float(self._rate))
            self._updated = time.monotonic()

    def throttle(self, amount: int, stop: threading.Event | None = None) -> None:
        """Sleep off this chunk's debt, waking early if *stop* is set."""
        if amount <= 0:
            return
        # Fast path: an unlimited bucket does nothing, so don't make every
        # worker of every download serialize on this shared lock for each
        # chunk. Reading the int is atomic; a rate change racing this check
        # just takes effect from the next chunk, which is already how the
        # interval-based limiter behaves.
        if self._rate == UNLIMITED:
            return
        with self._lock:
            if self._rate == UNLIMITED:
                return
            now = time.monotonic()
            self._tokens = min(float(self._rate), self._tokens + (now - self._updated) * self._rate)
            self._updated = now
            self._tokens -= amount  # may go negative: that's the debt to sleep off
            wait = -self._tokens / self._rate if self._tokens < 0 else 0.0
        if wait <= 0:
            return
        # Sleep in slices: the total is unchanged (so the average still lands
        # on the cap), but a pause is honoured within a slice, and lifting the
        # limit takes effect now instead of after the old debt is paid off.
        deadline = time.monotonic() + wait
        while self._rate != UNLIMITED:
            left = deadline - time.monotonic()
            if left <= 0:
                return
            if stop is None:
                time.sleep(min(left, self.SLICE))
            elif stop.wait(min(left, self.SLICE)):
                return
