"""Rate limits for the phone-remote listener (PROTOCOL.md sections 6 and 9).

Pure logic, no Qt and no sockets. Every object here is used from the listener
thread only, so nothing is locked. All of them take a monotonic ``clock`` so
tests can drive time.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable

REQUESTS_PER_SECOND = 20.0
REQUEST_BURST = 40.0
FLOOD_LIMIT = 200  # rate-limited requests ...
FLOOD_WINDOW_S = 10.0  # ... in this many seconds close the device's connections
FLOOD_REFUSAL_S = 30.0
FAILURE_LIMIT = 10  # failed pair/auth from one address ...
FAILURE_WINDOW_S = 60.0  # ... in this many seconds
FAILURE_REFUSAL_S = 60.0
_MAX_TRACKED_ADDRESSES = 1024

Clock = Callable[[], float]


class TokenBucket:
    """``rate`` tokens per second up to ``burst``; a request takes ``cost``."""

    def __init__(
        self,
        rate: float = REQUESTS_PER_SECOND,
        burst: float = REQUEST_BURST,
        clock: Clock = time.monotonic,
    ) -> None:
        self._rate = rate
        self._burst = burst
        self._clock = clock
        self._tokens = burst
        self._stamp = clock()

    def take(self, cost: float = 1.0) -> int:
        """Take ``cost`` tokens. Returns 0 when taken, else the milliseconds
        to wait before the same request could succeed (nothing is taken)."""
        now = self._clock()
        self._tokens = min(self._burst, self._tokens + max(0.0, now - self._stamp) * self._rate)
        self._stamp = now
        if cost <= self._tokens:
            self._tokens -= cost
            return 0
        return max(1, math.ceil((cost - self._tokens) / self._rate * 1000))


class DeviceLimiter:
    """One paired device's budget, shared by all its connections."""

    def __init__(self, clock: Clock = time.monotonic) -> None:
        self._clock = clock
        self._bucket = TokenBucket(clock=clock)
        self._limited: deque[float] = deque()
        self._refused_until = 0.0

    def take(self, cost: float = 1.0) -> int:
        """0 when the request may run, else ``retryAfterMs``."""
        return self._bucket.take(cost)

    def note_limited(self) -> bool:
        """Record one rate-limited request. True when this is a flood (more
        than 200 in 10 s): the device is then refused for 30 s."""
        now = self._clock()
        self._limited.append(now)
        while self._limited and now - self._limited[0] > FLOOD_WINDOW_S:
            self._limited.popleft()
        if len(self._limited) <= FLOOD_LIMIT:
            return False
        self._limited.clear()
        self._refused_until = now + FLOOD_REFUSAL_S
        return True

    def refused_ms(self) -> int:
        """Milliseconds left of a flood refusal, 0 when the device is welcome."""
        left = self._refused_until - self._clock()
        return max(1, math.ceil(left * 1000)) if left > 0 else 0


class FailureTracker:
    """Failed ``pair``/``auth`` attempts per remote address (section 6):
    ten in a minute and the address is refused at accept for a minute."""

    def __init__(self, clock: Clock = time.monotonic) -> None:
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._refused: dict[str, float] = {}

    def fail(self, address: str) -> None:
        now = self._clock()
        if address not in self._failures and len(self._failures) >= _MAX_TRACKED_ADDRESSES:
            self._prune(now)
            if len(self._failures) >= _MAX_TRACKED_ADDRESSES:
                # Only private and link-local peers get this far, so this many
                # live entries means an attack: forget the oldest.
                del self._failures[next(iter(self._failures))]
        times = self._failures.setdefault(address, deque())
        times.append(now)
        while times and now - times[0] > FAILURE_WINDOW_S:
            times.popleft()
        if len(times) >= FAILURE_LIMIT:
            times.clear()
            if len(self._refused) >= _MAX_TRACKED_ADDRESSES:
                self._prune(now)
            self._refused[address] = now + FAILURE_REFUSAL_S

    def refused(self, address: str) -> bool:
        until = self._refused.get(address)
        if until is None:
            return False
        if self._clock() >= until:
            del self._refused[address]
            return False
        return True

    def _prune(self, now: float) -> None:
        for address in [a for a, t in self._failures.items() if not t or now - t[-1] > 60.0]:
            del self._failures[address]
        for address in [a for a, until in self._refused.items() if now >= until]:
            del self._refused[address]
