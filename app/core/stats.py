"""Live-metric helpers for the dashboard.

``SpeedTracker`` turns a running byte total into a current/average/peak speed
and an ETA. ``SystemSampler`` reads CPU / disk / network throughput from
psutil, converting the cumulative counters into per-second rates. Both are
pure state machines - the UI polls them on a timer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class SpeedReading:
    current: float  # bytes/sec right now (smoothed)
    average: float  # bytes/sec over the session's active time
    peak: float  # highest current ever seen this session
    eta_seconds: float | None  # remaining / current, or None when unknowable


class SpeedTracker:
    """Aggregate download speed from a monotonically-growing byte total."""

    def __init__(self, smoothing: float = 0.4) -> None:
        self._smoothing = smoothing
        self._last_time: float | None = None
        self._last_total = 0
        self._ema = 0.0
        self.peak = 0.0
        self._active_seconds = 0.0
        self._session_bytes = 0

    def update(self, total_downloaded: int, remaining: int | None) -> SpeedReading:
        now = time.monotonic()
        if self._last_time is not None:
            elapsed = now - self._last_time
            if elapsed > 0:
                # A finishing job can shrink the live sum; clamp to 0.
                delta = max(0, total_downloaded - self._last_total)
                instant = delta / elapsed
                self._ema = self._ema * (1 - self._smoothing) + instant * self._smoothing
                if instant > 0:
                    self._active_seconds += elapsed
                    self._session_bytes += delta
                self.peak = max(self.peak, self._ema)
        self._last_time = now
        self._last_total = total_downloaded

        average = self._session_bytes / self._active_seconds if self._active_seconds > 0 else 0.0
        eta: float | None = None
        if remaining is not None and remaining > 0 and self._ema > 1:
            eta = remaining / self._ema
        return SpeedReading(current=self._ema, average=average, peak=self.peak, eta_seconds=eta)


@dataclass(frozen=True)
class SystemReading:
    cpu_percent: float
    disk_bytes_per_sec: float  # read + write
    net_recv_per_sec: float
    net_sent_per_sec: float


class SystemSampler:
    """CPU, disk, and network throughput from psutil, as per-second rates."""

    def __init__(self) -> None:
        self._last_time: float | None = None
        self._last_disk = 0
        self._last_recv = 0
        self._last_sent = 0
        self._available = True
        # Prime cpu_percent so the first real reading is meaningful, not 0.
        try:
            import psutil

            psutil.cpu_percent(interval=None)
        except Exception:  # pragma: no cover - no psutil / no perms
            self._available = False

    def sample(self) -> SystemReading:
        if not self._available:
            return SystemReading(0.0, 0.0, 0.0, 0.0)
        try:
            import psutil

            cpu = float(psutil.cpu_percent(interval=None))
            disk = psutil.disk_io_counters()
            net = psutil.net_io_counters()
        except Exception:  # pragma: no cover
            return SystemReading(0.0, 0.0, 0.0, 0.0)

        now = time.monotonic()
        disk_total = (disk.read_bytes + disk.write_bytes) if disk else 0
        recv = net.bytes_recv if net else 0
        sent = net.bytes_sent if net else 0

        disk_rate = recv_rate = sent_rate = 0.0
        if self._last_time is not None:
            elapsed = now - self._last_time
            if elapsed > 0:
                disk_rate = max(0, disk_total - self._last_disk) / elapsed
                recv_rate = max(0, recv - self._last_recv) / elapsed
                sent_rate = max(0, sent - self._last_sent) / elapsed
        self._last_time = now
        self._last_disk = disk_total
        self._last_recv = recv
        self._last_sent = sent
        return SystemReading(
            cpu_percent=cpu,
            disk_bytes_per_sec=disk_rate,
            net_recv_per_sec=recv_rate,
            net_sent_per_sec=sent_rate,
        )
