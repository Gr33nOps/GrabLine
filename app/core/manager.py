"""Download queue manager (F0.4): concurrency limit, pause/resume/cancel,
and per-kind engine dispatch (direct → segmenter, smart → yt-dlp, hls → FFmpeg).
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from app.core import categories, connectivity, naming, power
from app.core.credentials import CredentialStore
from app.core.downloader import RATE_LIMIT_MARKER, SegmentedDownload
from app.core.errors import DownloadError
from app.core.ffmpeg import find_ffmpeg
from app.core.models import Job, JobKind, JobStatus, Queue
from app.core.probe import ProbeResult
from app.core.ratelimit import RateLimiter
from app.core.settings import MAX_CONNECTIONS, Settings
from app.core.stats import SystemSampler
from app.db.database import Database
from app.engines.cloud import CloudDownload
from app.engines.hls import HlsDownload
from app.engines.manifest import HlsVariant
from app.engines.smart import MediaInfo, QualityOption, SmartDownload
from app.engines.torrent import SESSION as TORRENT_SESSION
from app.engines.torrent import TorrentDownload, TorrentStats

log = logging.getLogger(__name__)


class DownloadTask(Protocol):
    """What every engine's one-shot download object provides."""

    def run(self) -> JobStatus: ...
    def pause(self) -> None: ...
    def cancel(self) -> None: ...
    @property
    def bytes_downloaded(self) -> int: ...


_RETRY_BASE_SECONDS = 5.0
_RETRY_CAP_SECONDS = 300.0
_ONLINE_PROBE_SECONDS = 10.0

#: When several downloads start together with no global speed limit, leave them
#: uncapped this long so the line-capacity estimate can form at full speed.
#: Locking fair caps from the first handshake sample was trapping everyone at
#: a low equal share of a too-small budget.
_FAIR_PROBE_SECONDS = 1.5
#: If measured aggregate stays near our own fair budget, the estimate is the
#: bottleneck - raise it so equal shares can climb toward the real line rate.
_FAIR_SATURATION_RATIO = 0.90
_FAIR_SATURATION_BOOST = 1.20

#: Failures worth an automatic retry are everything EXCEPT these permanent
#: ones (DRM, private, 404s, disk-full ...). Unknown errors are retried, which
#: matches how IDM-style managers reconnect through flaky networks.
_PERMANENT_ERROR_MARKERS = (
    "drm",
    "private",
    "age-restricted",
    "region-blocked",
    "available in your country",
    "unsupported",
    "no downloadable",
    "web page",
    "not enough free disk",
    "did not contain",
    "http 4",
    "http error 4",
    "does not look like",
    "only http",
)

#: 4xx codes that are NOT permanent: 408 is a server-side timeout and 429 is
#: rate limiting - both clear on their own, so backing off and retrying is
#: exactly right even though the generic "http 4" marker would call them fatal.
#: RATE_LIMIT_MARKER covers the downloader's own verdict: a host that served
#: most of a file and then started refusing parallel requests is throttling us,
#: not denying the file, and the retry starts from the connection count it
#: learned to tolerate.
_TRANSIENT_OVERRIDES = (
    "http 408",
    "http error 408",
    "http 429",
    "http error 429",
    "too many requests",
    RATE_LIMIT_MARKER,
)


def _is_transient_error(error: str | None) -> bool:
    if not error:
        return False
    lowered = error.lower()
    if any(marker in lowered for marker in _TRANSIENT_OVERRIDES):
        return True
    return not any(marker in lowered for marker in _PERMANENT_ERROR_MARKERS)


def _in_window(now: datetime, from_str: str, to_str: str) -> bool:
    """Is ``now`` inside the daily [from, to) window? Handles a window that
    wraps past midnight (e.g. 23:00 to 07:00). An empty window is never in."""
    try:
        fh, fm = (int(p) for p in from_str.split(":"))
        th, tm = (int(p) for p in to_str.split(":"))
    except ValueError:
        return False
    start, end, cur = fh * 60 + fm, th * 60 + tm, now.hour * 60 + now.minute
    if start == end:
        return False
    return start <= cur < end if start < end else (cur >= start or cur < end)


@dataclass(frozen=True)
class JobView:
    """A read-only snapshot of one job for the UI."""

    id: int
    url: str
    filename: str
    dest_dir: str
    status: JobStatus
    total_size: int | None
    downloaded: int
    error: str | None
    kind: JobKind = JobKind.DIRECT
    title: str | None = None
    speed_limit_kbps: int = 0
    retry_count: int = 0
    tags: str = ""
    notes: str = ""
    queue_id: int | None = None

    @property
    def display_name(self) -> str:
        return self.title or self.filename


class DownloadManager:
    def __init__(
        self,
        db: Database,
        *,
        settings: Settings | None = None,
        max_concurrent: int | None = None,
        connections: int | None = None,
    ) -> None:
        self.db = db
        self.settings = settings or Settings(db)
        self.credentials = CredentialStore(db)
        # Explicit constructor values pin the knob; otherwise settings apply
        # live (reload_settings / next scheduler pass), no restart needed.
        self._max_concurrent_override = max_concurrent
        self._connections_override = connections
        self.limiter = RateLimiter(self.settings.speed_limit_kbps * 1024)
        self._applied_rate: int | None = None
        self._rate_lock = threading.Lock()
        #: (validity token, views) for snapshot(); see there.
        self._snapshot_cache: tuple[object, list[JobView]] | None = None
        # One reusable limiter per download that has its own cap; the running
        # task holds the very same instance, so a live change takes effect now.
        self._job_limiters: dict[int, RateLimiter] = {}
        # One shared limiter per host, so several downloads from the same
        # server obey a single per-host cap between them.
        self._host_limiters: dict[str, RateLimiter] = {}
        # Equal slice of the line when fair-speed is on; same instance is handed
        # to the task at start so live rebalance reaches workers immediately.
        self._fair_limiters: dict[int, RateLimiter] = {}
        # Automatic-throttle ('polite mode') sampling state.
        self._system_sampler = SystemSampler()
        self._rate_mark: tuple[float, int] | None = None
        self._last_download_rate = 0.0
        # Sticky estimate of line capacity (bytes/sec) used when fair-speed is
        # on and there is no global speed limit. While several jobs share, this
        # only ratchets up - shrinking it from our own throttled samples is what
        # made speeds bounce between downloads.
        self._line_capacity = 0.0
        # Multi-share without a global cap: briefly probe unlimited, then split.
        self._fair_sharing = False
        self._fair_probe_until = 0.0
        # Last fair cap pushed to each job (so torrent/handle updates are quiet).
        self._fair_applied: dict[int, int] = {}
        # job id -> monotonic deadline at which an auto-retry becomes due.
        self._retry_at: dict[int, float] = {}
        # Jobs paused because the download window closed; resumed when it opens.
        self._paused_by_schedule: set[int] = set()
        # wait-for-network probe state (assume online until proven otherwise).
        self._net_checked = 0.0
        self._net_ok = True
        self._net_probe: threading.Thread | None = None
        self._cond = threading.Condition()
        self._active: dict[int, DownloadTask] = {}
        self._threads: dict[int, threading.Thread] = {}
        #: Jobs force-removed while running: cancelling is asynchronous, so the
        #: worker deletes the row once it has actually stopped.
        self._pending_removal: set[int] = set()
        self._running = True
        # Retention (Settings -> Statistics): prune old per-day rows at startup.
        with contextlib.suppress(Exception):
            self.db.prune_stats(self.settings.stats_retention_days)
        self._scheduler = threading.Thread(target=self._loop, name="gl-scheduler", daemon=True)
        self._scheduler.start()

    @property
    def max_concurrent(self) -> int:
        if self._max_concurrent_override is not None:
            return self._max_concurrent_override
        return self.settings.max_concurrent

    @property
    def connections(self) -> int:
        if self._connections_override is not None:
            return self._connections_override
        return self.settings.connections

    def reload_settings(self) -> None:
        """Apply settings changes to live state (speed cap now, slots next pass)."""
        self._apply_global_rate()
        # The torrent session (ports, DHT, rate caps) follows live too.
        with contextlib.suppress(DownloadError):
            TORRENT_SESSION.configure(self.settings)
        self._kick()

    # -------------------------------------------------- speed limit + schedule

    def _in_full_speed_window(self, now: datetime) -> bool:
        return _in_window(now, self.settings.speed_full_from, self.settings.speed_full_to)

    def _effective_global_rate(self) -> int:
        base = self.settings.speed_limit_kbps * 1024
        if (
            base
            and self.settings.speed_schedule_enabled
            and self._in_full_speed_window(datetime.now())
        ):
            base = 0  # full speed during the scheduled window
        throttle = self._auto_throttle_rate()
        if throttle and (base == 0 or throttle < base):
            return throttle  # polite mode caps below the base (or the unlimited base)
        return base

    def _auto_throttle_rate(self) -> int:
        """The reduced cap (bytes/sec) when 'polite mode' sees other apps using
        the network heavily, else 0. Other traffic = system throughput minus
        our own accounted download rate."""
        if not self.settings.auto_throttle:
            return 0
        system = self._system_sampler.sample()
        other = max(0.0, system.net_recv_per_sec - self._download_rate())
        threshold = self.settings.auto_throttle_threshold_kbps * 1024
        return self.settings.auto_throttle_kbps * 1024 if other > threshold else 0

    def _download_rate(self) -> float:
        """GrabLine's own current download throughput (bytes/sec)."""
        return self._last_download_rate

    def _sample_throughput(self) -> None:
        """Refresh aggregate rate and the sticky line-capacity estimate.

        Must not take ``_cond``: the scheduler already holds it when this runs.
        """
        items = [(job_id, task.bytes_downloaded) for job_id, task in self._active.items()]
        total = sum(size for _, size in items)
        n_active = len(items)
        now = time.monotonic()
        if self._rate_mark is not None:
            elapsed = now - self._rate_mark[0]
            if elapsed > 0:
                measured = max(0.0, (total - self._rate_mark[1]) / elapsed)
                self._last_download_rate = measured
                self._update_line_capacity(measured, n_active)
        self._rate_mark = (now, total)

    def _update_line_capacity(self, measured: float, n_active: int) -> None:
        """Learn how fast the line is without letting fair-speed choke itself.

        One (or zero) active download: track the peak with a mild decay so a
        quieter period can settle. Two or more under fair-speed: only raise the
        estimate - our equal caps would otherwise lower the measured total and
        then shrink every share, which is the bounce the user saw.

        When the aggregate sits on our own fair budget (saturation), bump the
        estimate up so a handshake-low lock-in can climb to the real line rate.
        Overshoot is harmless: equal caps above the line just stop binding.
        """
        if measured <= 0:
            if n_active == 0:
                self._line_capacity *= 0.99
            return
        if n_active <= 1 or not self.settings.fair_speed:
            if measured > self._line_capacity:
                self._line_capacity = measured
            elif n_active == 1:
                self._line_capacity = max(measured, self._line_capacity * 0.995)
            return
        if measured > self._line_capacity:
            self._line_capacity = measured
            return
        if (
            self._line_capacity >= 64 * 1024
            and not self._in_fair_probe()
            and self._effective_global_rate() <= 0
            and measured >= self._line_capacity * _FAIR_SATURATION_RATIO
        ):
            self._line_capacity *= _FAIR_SATURATION_BOOST

    def _note_fair_share_state(self, n_active: int) -> None:
        """Start a short unlimited probe when multi-share begins without a cap."""
        sharing = n_active >= 2 and self.settings.fair_speed and self._effective_global_rate() <= 0
        if sharing and not self._fair_sharing:
            self._fair_probe_until = time.monotonic() + _FAIR_PROBE_SECONDS
        if not sharing:
            self._fair_probe_until = 0.0
        self._fair_sharing = sharing

    def _in_fair_probe(self) -> bool:
        return self._fair_probe_until > 0.0 and time.monotonic() < self._fair_probe_until

    def _apply_global_rate(self) -> None:
        # Atomic read-compare-set: the scheduler and reload_settings both land
        # here, and interleaving them let a pass that had already read the old
        # limit overwrite a cap the user had just applied.
        self._sample_throughput()
        with self._rate_lock:
            rate = self._effective_global_rate()
            if rate != self._applied_rate:
                self.limiter.set_rate(rate)
                self._applied_rate = rate
        self._note_fair_share_state(len(self._active))
        self._apply_fair_speed()

    # -------------------------------------------------- timed download window

    def downloads_allowed_now(self) -> bool:
        """False while any global restriction is closed: the timed window,
        the allowed weekdays, battery mode, or a dead internet connection."""
        now = datetime.now()
        days = self.settings.download_days
        if len(days) < 7 and now.weekday() not in days:
            return False
        if self.settings.pause_on_battery and power.on_battery():
            threshold = self.settings.battery_min_percent
            percent = power.battery_percent()
            # 0 = pause whenever on battery; otherwise only below the threshold.
            if threshold == 0 or percent is None or percent < threshold:
                return False
        if self.settings.download_schedule_enabled and not _in_window(
            now, self.settings.download_start, self.settings.download_stop
        ):
            return False
        return not (self.settings.wait_for_network and not self._online())

    def _online(self) -> bool:
        """Last known connectivity (wait-for-network only), refreshed off-thread.

        The probe itself is two TCP handshakes worth up to 3s. This runs inside
        the scheduler pass, which holds ``_cond`` - the same lock the UI's
        twice-a-second snapshot, pause, cancel and add all need - so probing
        inline froze the whole app every time it ran. The answer is only used to
        decide whether to start something in the next half second, so a slightly
        stale value costs nothing.
        """
        now = time.monotonic()
        if now - self._net_checked >= _ONLINE_PROBE_SECONDS:
            self._net_checked = now
            self._start_online_probe()
        return self._net_ok

    def _start_online_probe(self) -> None:
        if self._net_probe is not None and self._net_probe.is_alive():
            return

        def probe() -> None:
            online = connectivity.is_online()
            with self._cond:
                was_online = self._net_ok
                self._net_ok = online
                if online and not was_online:
                    for job_id in list(self._retry_at):
                        self._retry_at[job_id] = 0.0  # due now
                    log.info("internet is back - retrying failed downloads now")
                self._cond.notify_all()

        # Publish only once it is running: an observer that catches the thread
        # between construction and start() cannot even join it.
        thread = threading.Thread(target=probe, name="gl-netprobe", daemon=True)
        thread.start()
        self._net_probe = thread

    def _apply_download_schedule(self) -> None:
        """Pause active downloads when the window closes; resume them (and let
        queued ones start) when it opens."""
        if self.downloads_allowed_now():
            if self._paused_by_schedule:
                for job_id in list(self._paused_by_schedule):
                    job = self.db.get_job(job_id)
                    if job is not None and job.status is JobStatus.PAUSED:
                        self.db.set_job_status(job_id, JobStatus.QUEUED)
                self._paused_by_schedule.clear()
            return
        # Window closed: pause anything running and remember it.
        for job_id, task in list(self._active.items()):
            task.pause()
            self._paused_by_schedule.add(job_id)

    # -------------------------------------------------- per-download speed cap

    def _job_limiter_for(self, job: Job) -> RateLimiter | None:
        kbps = int(job.options.get("speed_limit_kbps") or 0)
        if kbps <= 0:
            self._job_limiters.pop(job.id, None)
            return None
        limiter = self._job_limiters.get(job.id)
        if limiter is None:
            limiter = RateLimiter(kbps * 1024)
            self._job_limiters[job.id] = limiter
        else:
            limiter.set_rate(kbps * 1024)
        return limiter

    def _fair_limiter_for(self, job_id: int) -> RateLimiter:
        limiter = self._fair_limiters.get(job_id)
        if limiter is None:
            limiter = RateLimiter(0)
            self._fair_limiters[job_id] = limiter
        return limiter

    def _fair_budget_bytes(self) -> int:
        """Bytes/sec to split evenly across active downloads.

        Prefer the configured global cap (that is the usual download-manager
        model: known ceiling ÷ N). When unlimited, use the sticky line-capacity
        estimate once it is large enough to be real traffic, not handshake noise.
        """
        global_rate = self._effective_global_rate()
        if global_rate > 0:
            return global_rate
        capacity = int(self._line_capacity)
        return capacity if capacity >= 64 * 1024 else 0

    def _compute_fair_caps(self, job_ids: Sequence[int]) -> dict[int, int]:
        """Per-job download caps in bytes/sec (0 = unlimited).

        Strict equal split of the budget - the same model IDM-style managers
        use. No reclaim of "unused" share: redistributing from short-term rate
        samples made one download surge while another starved, every half second.
        """
        if not job_ids:
            return {}
        if len(job_ids) == 1:
            # Fair-speed owns per-torrent handle limits (session cap is cleared),
            # so a lone job still needs the global speed limit applied here.
            return {job_ids[0]: self._effective_global_rate()}
        budget = self._fair_budget_bytes()
        if budget <= 0 or self._in_fair_probe():
            # Line not measured yet, or still in the post-start probe window:
            # leave everyone unlimited so capacity can form at full speed.
            return {job_id: 0 for job_id in job_ids}
        equal = max(1024, budget // len(job_ids))
        return {job_id: equal for job_id in job_ids}

    def _apply_fair_speed(self) -> None:
        """Push live per-job caps so simultaneous downloads share the line.

        Must not take ``_cond``: the scheduler already holds it when this runs.
        """
        active = dict(self._active)
        if not active:
            self._fair_applied.clear()
            return
        if not self.settings.fair_speed:
            caps = {job_id: 0 for job_id in active}
        else:
            caps = self._compute_fair_caps(list(active.keys()))
        stale = [job_id for job_id in self._fair_applied if job_id not in active]
        for job_id in stale:
            self._fair_applied.pop(job_id, None)
        for job_id, task in active.items():
            rate = max(0, int(caps.get(job_id, 0)))
            if self._fair_applied.get(job_id) == rate:
                continue
            self._fair_applied[job_id] = rate
            self._fair_limiter_for(job_id).set_rate(rate)
            apply = getattr(task, "set_download_limit", None)
            if callable(apply):
                with contextlib.suppress(Exception):
                    apply(rate)

    def _host_limiter_for(self, job: Job) -> RateLimiter | None:
        """The shared limiter for a job's server host, if a per-host cap is
        configured. All downloads from that host consume from one bucket."""
        host = urlsplit(job.url).hostname
        limits = self.settings.host_limits
        kbps = limits.get(host.lower()) if host else None
        if not kbps:
            return None
        limiter = self._host_limiters.get(host)  # type: ignore[arg-type]
        if limiter is None:
            limiter = RateLimiter(kbps * 1024)
            self._host_limiters[host] = limiter  # type: ignore[index]
        else:
            limiter.set_rate(kbps * 1024)
        return limiter

    def set_job_speed(self, job_id: int, kbps: int) -> None:
        """Cap one download to ``kbps`` KB/s (0 clears it). Applies live if it
        is running, and persists for the next run."""
        job = self.db.get_job(job_id)
        if job is None:
            return
        options = dict(job.options)
        if kbps > 0:
            options["speed_limit_kbps"] = kbps
        else:
            options.pop("speed_limit_kbps", None)
        self.db.update_job_options(job_id, options)
        job.options = options
        self._job_limiter_for(job)  # create/update/drop the live limiter

    def set_job_connections(self, job_id: int, connections: int) -> None:
        """Pin one download to exactly ``connections`` parallel connections
        (0 clears it back to automatic). Takes effect when the download
        (re)starts - live segments aren't torn down mid-flight."""
        job = self.db.get_job(job_id)
        if job is None:
            return
        options = dict(job.options)
        if connections > 0:
            options["connections"] = max(1, min(MAX_CONNECTIONS, connections))
        else:
            options.pop("connections", None)
        self.db.update_job_options(job_id, options)
        job.options = options

    def _set_job_option(self, job_id: int, key: str, value: str) -> None:
        job = self.db.get_job(job_id)
        if job is None:
            return
        options = dict(job.options)
        if value.strip():
            options[key] = value.strip()
        else:
            options.pop(key, None)
        self.db.update_job_options(job_id, options)

    def set_job_tags(self, job_id: int, tags: str) -> None:
        """Free-form tags/labels ('work, iso, later'); searchable in the UI."""
        self._set_job_option(job_id, "tags", tags)

    def set_job_notes(self, job_id: int, notes: str) -> None:
        self._set_job_option(job_id, "notes", notes)

    # --------------------------------------------------------- named queues

    def list_queues(self) -> list[Queue]:
        return self.db.list_queues()

    def create_queue(self, name: str) -> Queue:
        return self.db.create_queue(name)

    def update_queue(self, queue: Queue) -> None:
        self.db.update_queue(queue)
        self._kick()

    def delete_queue(self, queue_id: int) -> None:
        self.db.delete_queue(queue_id)
        self._kick()

    def set_job_queue(self, job_id: int, queue_id: int | None) -> None:
        """Move a download into a named queue / group (None = default)."""
        self.db.set_job_queue(job_id, queue_id)
        self._kick()

    def set_job_start_at(self, job_id: int, when: datetime | None) -> None:
        """Download later: hold this job until ``when`` (None starts it
        normally again)."""
        job = self.db.get_job(job_id)
        if job is None:
            return
        options = dict(job.options)
        if when is not None:
            options["start_at"] = when.isoformat(timespec="minutes")
        else:
            options.pop("start_at", None)
        self.db.update_job_options(job_id, options)
        self._kick()

    def set_job_after(self, job_id: int, after_job_id: int | None) -> None:
        """Job dependency: hold this download until ``after_job_id`` has
        COMPLETED ('download B only after A finishes'). None clears it."""
        job = self.db.get_job(job_id)
        if job is None or after_job_id == job_id:
            return
        options = dict(job.options)
        if after_job_id:
            options["after_job"] = after_job_id
        else:
            options.pop("after_job", None)
        self.db.update_job_options(job_id, options)
        self._kick()

    def _queue_for(self, filename: str) -> int | None:
        """The category queue for a new download; else the configured default
        queue (Settings → Queue Manager); else the global default (None)."""
        category = categories.category_for(filename)
        if category is not None:
            for queue in self.db.list_queues():
                if queue.category == category:
                    return queue.id
        default = self.settings.default_queue_id
        if default and any(q.id == default for q in self.db.list_queues()):
            return default
        return None

    def _apply_add_defaults(self, job: Job) -> Job:
        """Post-create defaults for every add path: hold the job when
        auto-start is off, and stamp the configured default tags."""
        changed = False
        if self.settings.default_tags:
            self.set_job_tags(job.id, self.settings.default_tags)
            changed = True
        if not self.settings.auto_start_downloads:
            self.db.set_job_status(job.id, JobStatus.PAUSED)
            changed = True
        if changed:
            fresh = self.db.get_job(job.id)
            return fresh if fresh is not None else job
        return job

    def find_existing(self, url: str) -> Job | None:
        """Duplicate detection at add time: the first job (any status) already
        pointing at this exact URL, or None. Indexed lookup, so a long download
        history doesn't turn every add into a full-table scan."""
        return self.db.first_job_for_url(url)

    def move_job_file(self, job_id: int, dest_dir: str | Path) -> Path:
        """Move a completed download into ``dest_dir`` (a favorite folder) and
        re-point the row. Never overwrites - the name gets ' (1)' if taken."""
        job = self.db.get_job(job_id)
        if job is None:
            raise DownloadError("this download no longer exists")
        source = Path(job.dest_dir) / job.filename
        if job.status is not JobStatus.COMPLETED or not source.is_file():
            raise DownloadError("only a finished download can be moved")
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        target = naming.unique_path(dest / job.filename)
        shutil.move(str(source), str(target))
        self.db.update_job_dest(job_id, str(dest))
        if target.name != job.filename:
            self.db.update_job_filename(job_id, target.name)
        return target

    # -------------------------------------------------------- queue priorities

    def _pending_order(self) -> list[Job]:
        """Jobs waiting to run, in current run order (list_jobs is priority
        sorted already)."""
        return [j for j in self.db.list_jobs() if j.status in (JobStatus.QUEUED, JobStatus.PAUSED)]

    def _reassign(self, order: list[Job]) -> None:
        # Dense, strictly-decreasing priorities so the order is unambiguous.
        for position, job in enumerate(order):
            self.db.set_priority(job.id, len(order) - position)
        self._kick()

    def _move(self, job_id: int, delta: int) -> None:
        order = self._pending_order()
        index = next((i for i, j in enumerate(order) if j.id == job_id), None)
        if index is None:
            return
        target = index + delta
        if not 0 <= target < len(order):
            return
        order[index], order[target] = order[target], order[index]
        self._reassign(order)

    def move_up(self, job_id: int) -> None:
        self._move(job_id, -1)

    def move_down(self, job_id: int) -> None:
        self._move(job_id, 1)

    def move_to_top(self, job_id: int) -> None:
        order = self._pending_order()
        picked = [j for j in order if j.id == job_id]
        if picked:
            self._reassign(picked + [j for j in order if j.id != job_id])

    def move_to_bottom(self, job_id: int) -> None:
        order = self._pending_order()
        picked = [j for j in order if j.id == job_id]
        if picked:
            self._reassign([j for j in order if j.id != job_id] + picked)

    # ------------------------------------------------------------- adding

    def _dest_for(self, filename: str, dest_dir: str | Path | None) -> str:
        if dest_dir is not None:
            return str(dest_dir)
        return str(
            categories.dest_dir_for(
                self.settings.download_dir,
                filename,
                enabled=self.settings.categories_enabled,
            )
        )

    def add_url(
        self,
        url: str,
        dest_dir: str | Path | None = None,
        filename: str | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        mirrors: Sequence[str] | None = None,
        probe: ProbeResult | None = None,
    ) -> Job:
        """Queue a direct (segmented) download. ``headers`` (cookies/referer
        from the browser) let a login-gated file download too; ``mirrors`` are
        alternate URLs tried in order if this one fails for good. ``probe`` is
        the resolve-time Range probe - when provided, the downloader skips a
        second identical round-trip and starts workers immediately."""
        name = naming.sanitize_filename(filename) if filename else naming.filename_from_url(url)
        name = naming.apply_rename_rules(name, self.settings.rename_rules)
        options: dict[str, Any] = {"http_headers": dict(headers)} if headers else {}
        if mirrors:
            options["mirrors"] = [m for m in mirrors if m and m != url]
        job = self.db.create_job(
            url,
            self._dest_for(name, dest_dir),
            name,
            options=options,
            queue_id=self._queue_for(name),
        )
        job = self._apply_add_defaults(job)
        if probe is not None:
            if probe.filename:
                job.filename = naming.sanitize_filename(probe.filename)
            job.final_url = probe.final_url
            job.total_size = probe.total_size
            job.resumable = probe.resumable
            job.etag = probe.etag
            job.last_modified = probe.last_modified
            self.db.update_job_probe(job)
            job = self.db.get_job(job.id) or job
        self._kick()
        return job

    def add_smart(
        self,
        url: str,
        media: MediaInfo,
        option: QualityOption,
        *,
        dest_dir: str | Path | None = None,
        subtitles: Mapping[str, Any] | None = None,
        trim: tuple[float, float] | None = None,
        extras: Mapping[str, Any] | None = None,
        use_session: bool = False,
        session_browser: str = "chrome",
        headers: Mapping[str, str] | None = None,
    ) -> Job:
        """Queue a Smart Engine (yt-dlp) download with a chosen quality option."""
        return self.add_smart_entry(
            url,
            media.title,
            option,
            dest_dir=dest_dir,
            subtitles=subtitles,
            trim=trim,
            extras=extras,
            use_session=use_session,
            session_browser=session_browser,
            headers=headers,
        )

    def add_smart_entry(
        self,
        url: str,
        title: str,
        option: QualityOption,
        *,
        dest_dir: str | Path | None = None,
        subtitles: Mapping[str, Any] | None = None,
        trim: tuple[float, float] | None = None,
        extras: Mapping[str, Any] | None = None,
        use_session: bool = False,
        session_browser: str = "chrome",
        headers: Mapping[str, str] | None = None,
    ) -> Job:
        """Queue one Smart Engine job from just a URL and title - the playlist
        path (F1.7), where entries were listed flat and formats resolve at
        download time. ``headers`` are the browser handoff's Referer/Cookie/
        User-Agent, forwarded to yt-dlp so a gated video downloads."""
        extension = option.audio_format if option.kind == "audio" else "mp4"
        base = naming.sanitize_filename(title)
        filename = naming.apply_rename_rules(f"{base}.{extension}", self.settings.rename_rules)
        from app.engines.smart import needs_js_runtime

        # YouTube: always attach a browser login automatically so the person
        # never has to know about cookies or flip a session toggle.
        if needs_js_runtime(url):
            use_session = True
            if not session_browser:
                from app.core.browser_setup import detect_cookie_browser

                session_browser = detect_cookie_browser() or "chrome"
        options: dict[str, Any] = {
            "format_spec": option.format_spec,
            "quality_label": option.label,
            "audio_format": option.audio_format,
            "subtitles": dict(subtitles) if subtitles else None,
            "trim": list(trim) if trim else None,
            "use_session": use_session,
            "session_browser": session_browser,
            # Quality-first (Settings → Video Downloader): use the JS runtime
            # from the first attempt for the full format ladder, trading the
            # fast jsless start.
            "hq_first": self.settings.video_hq_first,
            # Global video defaults; per-download extras below override them.
            "audio_bitrate": self.settings.audio_bitrate,
        }
        if self.settings.cookies_file:
            options["cookie_file"] = self.settings.cookies_file
        if headers:
            options["http_headers"] = dict(headers)
        if extras:
            options.update(extras)
        job = self.db.create_job(
            url,
            self._dest_for(filename, dest_dir),
            filename,
            kind=JobKind.SMART,
            title=title,
            options=options,
            queue_id=self._queue_for(filename),
        )
        job = self._apply_add_defaults(job)
        # Show an estimated size in the list immediately when analysis already
        # knew one - live hook totals refine it once the download is flowing.
        if option.estimated_size:
            self.db.update_job_total(job.id, option.estimated_size)
            job = self.db.get_job(job.id) or job
        self._kick()
        return job

    def add_hls(
        self,
        url: str,
        *,
        dest_dir: str | Path | None = None,
        title: str | None = None,
        variant: HlsVariant | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Job:
        """Queue an HLS/DASH stream for FFmpeg reassembly; ``variant`` pins a
        quality picked from the master playlist (F2.1). ``headers``
        (cookie/referer/user-agent from the browser handoff) are forwarded to
        every request FFmpeg makes - many CDNs check the Referer against the
        page that requested the stream and refuse it (or serve an HTML error
        FFmpeg can't parse) without one."""
        stem = Path(naming.filename_from_url(url)).stem or "stream"
        base = naming.sanitize_filename(title) if title else stem
        filename = naming.apply_rename_rules(f"{base}.mp4", self.settings.rename_rules)
        options: dict[str, Any] = {}
        if variant is not None:
            options = {
                "variant_url": variant.url,
                "audio_url": variant.audio_url,
                "quality_label": variant.label,
            }
        if headers:
            options["http_headers"] = dict(headers)
        job = self.db.create_job(
            url,
            self._dest_for(filename, dest_dir),
            filename,
            kind=JobKind.HLS,
            title=title,
            options=options,
            queue_id=self._queue_for(filename),
        )
        job = self._apply_add_defaults(job)
        self._kick()
        return job

    def add_torrent(
        self,
        source: str,
        *,
        dest_dir: str | Path | None = None,
        name: str | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> Job:
        """Queue a torrent: a magnet link, a local .torrent path, or an
        http(s) .torrent URL. ``name`` is the display name until metadata
        arrives (magnets learn their real name from the swarm)."""
        from app.engines.torrent import magnet_display_name

        if dest_dir is None:
            dest_dir = self.settings.torrent_dir or self.settings.download_dir
        if not name:
            if source.lower().startswith("magnet:"):
                name = magnet_display_name(source) or "magnet"
            else:
                name = Path(source.split("?")[0]).stem or "torrent"
        job = self.db.create_job(
            source,
            str(dest_dir),
            naming.sanitize_filename(name),
            kind=JobKind.TORRENT,
            title=name,
            options=dict(options or {}),
            queue_id=self._queue_for(name),
        )
        job = self._apply_add_defaults(job)
        self._kick()
        return job

    def add_cloud(
        self,
        url: str,
        *,
        dest_dir: str | Path | None = None,
        filename: str | None = None,
    ) -> Job:
        """Queue a cloud protocol download (ftp/ftps/sftp/scp/s3/webdav).
        Credentials are looked up from the store by host at run time."""
        from app.engines.cloud import suggested_filename

        name = naming.sanitize_filename(filename or suggested_filename(url))
        name = naming.apply_rename_rules(name, self.settings.rename_rules)
        job = self.db.create_job(
            url,
            self._dest_for(name, dest_dir),
            name,
            kind=JobKind.CLOUD,
            options={},
            queue_id=self._queue_for(name),
        )
        job = self._apply_add_defaults(job)
        self._kick()
        return job

    def list_cloud_folder(self, url: str) -> list[Any]:
        """Files inside a remote folder (for the 'download whole folder'
        flow). Returns cloud.RemoteFile entries."""
        from app.engines.cloud import list_folder

        return list_folder(url, self.credentials)

    # ------------------------------------------------------------ control

    def pause(self, job_id: int) -> None:
        with self._cond:
            active = self._active.get(job_id)
        if active is not None:
            active.pause()
            return
        job = self.db.get_job(job_id)
        if job is not None and job.status is JobStatus.QUEUED:
            self.db.set_job_status(job_id, JobStatus.PAUSED)

    def resume(self, job_id: int) -> None:
        job = self.db.get_job(job_id)
        if job is not None and job.status in (
            JobStatus.PAUSED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        ):
            # A manual resume overrides any pending auto-retry and its counter.
            self._retry_at.pop(job_id, None)
            self.db.set_retry_count(job_id, 0)
            self.db.set_job_status(job_id, JobStatus.QUEUED)
            self._kick()

    def cancel(self, job_id: int) -> None:
        with self._cond:
            active = self._active.get(job_id)
        if active is not None:
            active.cancel()
            return
        self._retry_at.pop(job_id, None)
        self._job_limiters.pop(job_id, None)
        self._fair_limiters.pop(job_id, None)
        self._fair_applied.pop(job_id, None)
        job = self.db.get_job(job_id)
        if job is not None and job.status in (
            JobStatus.QUEUED,
            JobStatus.PAUSED,
            JobStatus.FAILED,
        ):
            job.part_path.unlink(missing_ok=True)
            self.db.clear_segments(job_id)
            self.db.set_job_status(job_id, JobStatus.CANCELLED)

    def remove(self, job_id: int, *, force: bool = False) -> None:
        """Drop a job from the list/history, whatever state it is in.

        Completed files stay on disk. A running job has to be cancelled first
        and that is asynchronous, so ``force`` marks it: the worker deletes the
        row the moment it stops. Without ``force`` a running job is only
        cancelled and its row survives (the old behaviour).
        """
        self._retry_at.pop(job_id, None)
        self._job_limiters.pop(job_id, None)
        self._fair_limiters.pop(job_id, None)
        self._fair_applied.pop(job_id, None)
        with self._cond:
            active = self._active.get(job_id)
            if active is not None and force:
                self._pending_removal.add(job_id)
        if active is not None:
            active.cancel()
            return  # _run_job purges it once the worker actually stops
        self._purge_job(job_id)

    def _purge_job(self, job_id: int) -> None:
        """Delete the row and any partial file. Completed files stay on disk."""
        job = self.db.get_job(job_id)
        if job is None:
            return
        if job.status is not JobStatus.COMPLETED:
            job.part_path.unlink(missing_ok=True)
        self.db.delete_job(job_id)

    def snapshot(self) -> list[JobView]:
        with self._cond:
            active = dict(self._active)
        # What comes out of here is a pure function of the stored rows and the
        # live byte counters, so when neither has moved the previous answer is
        # still exactly right. Worth caching: the UI polls this twice a second
        # from two places, and rebuilding it re-reads every job row in history
        # and re-parses each one's JSON options - the main reason a long
        # download history made the whole app feel heavier.
        live = tuple(sorted((job_id, task.bytes_downloaded) for job_id, task in active.items()))
        token = (self.db.revision, live)
        cached = self._snapshot_cache
        if cached is not None and cached[0] == token:
            return list(cached[1])
        # One query for all segment progress instead of one per direct job:
        # the UI polls snapshot() every 500ms, so this is a real hot path.
        segment_progress = self.db.all_segment_progress()
        views: list[JobView] = []
        for job in self.db.list_jobs():
            task = active.get(job.id)
            if task is not None:
                downloaded = task.bytes_downloaded
            elif job.kind is JobKind.DIRECT:
                # No segment rows means they were pruned on completion; the
                # job row kept the total.
                downloaded = segment_progress.get(job.id) or job.downloaded
            else:
                downloaded = job.downloaded
            views.append(
                JobView(
                    id=job.id,
                    url=job.url,
                    filename=job.filename,
                    dest_dir=job.dest_dir,
                    status=job.status,
                    total_size=job.total_size,
                    downloaded=downloaded,
                    error=job.error,
                    kind=job.kind,
                    title=job.title,
                    speed_limit_kbps=int(job.options.get("speed_limit_kbps") or 0),
                    retry_count=job.retry_count,
                    tags=str(job.options.get("tags") or ""),
                    notes=str(job.options.get("notes") or ""),
                    queue_id=job.queue_id,
                )
            )
        self._snapshot_cache = (token, views)
        return list(views)

    def shutdown(self, timeout: float = 10.0) -> None:
        """Pause everything in flight so it resumes cleanly next launch."""
        with self._cond:
            self._running = False
            active = list(self._active.values())
            self._cond.notify_all()
        for task in active:
            task.pause()
        self._scheduler.join(timeout=timeout)
        with self._cond:
            threads = list(self._threads.values())
        for thread in threads:
            thread.join(timeout=timeout)
        # The torrent ratio ticker is a process-wide daemon that holds a
        # Settings reference; drop it so a closed test DB cannot trip it.
        TORRENT_SESSION.detach()

    # ---------------------------------------------------------- scheduler

    def _create_task(self, job: Job) -> DownloadTask:
        job_kbps = int(job.options.get("speed_limit_kbps") or 0)
        proxy = self.settings.proxy
        fair_limiter = self._fair_limiter_for(job.id)
        if job.kind is JobKind.SMART:
            # yt-dlp takes one number: the tighter of the global and per-job cap.
            # Fair-speed is applied live in the progress hook via fair_limiter.
            rates = [r for r in (self.limiter.rate, job_kbps * 1024) if r]
            return SmartDownload(
                self.db,
                job,
                ffmpeg_path=find_ffmpeg(self.settings),
                ratelimit=min(rates) if rates else None,
                fair_limiter=fair_limiter,
                proxy=proxy,
            )
        if job.kind is JobKind.HLS:
            # FFmpeg-driven jobs are not rate-limited (Phase 3 polish).
            return HlsDownload(self.db, job, ffmpeg_path=find_ffmpeg(self.settings), proxy=proxy)
        if job.kind is JobKind.TORRENT:
            return TorrentDownload(self.db, job, settings=self.settings)
        if job.kind is JobKind.CLOUD:
            return CloudDownload(self.db, job, credentials=self.credentials)
        # Fair sharing of the connection budget across simultaneous downloads.
        # TCP fairness is per-flow, so without this the first download's N flows
        # starve every later sibling. Each unpinned download instead runs its
        # equal share of the budget (_fair_share_connections), recomputed *live*
        # by the downloader's worker pool - so a lone job uses the whole budget,
        # two split it in half, and a download reclaims connections the moment a
        # sibling finishes. A per-download pin (right-click -> Connections...) is
        # an explicit choice that keeps its full count and stays out of the split.
        pinned = int(job.options.get("connections") or 0)
        # A pin keeps its full fixed count and stays out of the split; an
        # unpinned download runs its fair share, recomputed live by the pool.
        fixed = max(1, min(MAX_CONNECTIONS, pinned)) if pinned else self.connections
        if not pinned:
            # A previous run learned how much parallelism this host tolerates
            # before it starts refusing requests. Start there rather than
            # re-discovering the same block (and burning the retry budget) from
            # the full width every time.
            learned = int(job.options.get("learned_connections") or 0)
            if learned:
                fixed = max(1, min(fixed, learned))
        return SegmentedDownload(
            self.db,
            job,
            connections=fixed,
            connections_target=None if pinned else self._fair_share_connections,
            shares_budget=not pinned,
            limiter=self.limiter,
            job_limiter=self._job_limiter_for(job),
            host_limiter=self._host_limiter_for(job),
            fair_limiter=fair_limiter,
            proxy=proxy,
            headers=job.options.get("http_headers") or None,
            bypass_hosts=self.settings.proxy_bypass,
            user_agent=self.settings.user_agent or None,
        )

    def _fair_share_connections(self) -> int:
        """One unpinned download's live, equal slice of the connection budget:
        ``budget // (number of unpinned downloads running now)``, floored so no
        download starves and capped at the budget so a lone job gets it all.

        Shares never exceed the budget in aggregate: a floor above ``budget // n``
        would over-allocate sockets and recreate the hogging this exists to stop.
        """
        with self._cond:
            sharers = sum(
                1 for task in self._active.values() if getattr(task, "shares_budget", False)
            )
        budget = self.connections
        share = budget // max(1, sharers)
        return max(1, min(budget, share))

    def _kick(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def _loop(self) -> None:
        # One bad pass must never end scheduling. Without this guard a single
        # raise in here (a malformed imported proxy, a transient sqlite error)
        # killed the scheduler thread for the rest of the session: the app kept
        # looking alive while no queued job ever started again.
        while True:
            try:
                if not self._pass():
                    return
            except Exception:
                log.exception("scheduler pass failed; continuing")
                time.sleep(0.5)

    def _pass(self) -> bool:
        """One scheduler pass. False once the manager has stopped."""
        with self._cond:
            if not self._running:
                return False
            self._apply_global_rate()  # honor the nightly full-speed window
            self._apply_download_schedule()  # start/stop within the timed window
            self._promote_due_retries()  # re-queue failed jobs whose backoff elapsed
            if self.downloads_allowed_now() and len(self._active) < self.max_concurrent:
                job = self._next_queued()
                if job is not None:
                    self._start_job(job)
                    return True
            self._cond.wait(timeout=0.5)
        return True

    def _start_job(self, job: Job) -> None:
        """Build the task and hand it a thread. Caller holds _cond.

        A failure here (bad proxy, unbuildable client) belongs to the job, not
        the scheduler: mark it failed and move on rather than letting the
        exception escape into the loop.
        """
        try:
            task = self._create_task(job)
        except Exception as exc:
            log.exception("job %s could not be started", job.id)
            self.db.set_job_status(job.id, JobStatus.FAILED, f"could not start: {exc}")
            return
        thread = threading.Thread(
            target=self._run_job,
            args=(job, task),
            name=f"gl-job-{job.id}",
            daemon=True,
        )
        self._active[job.id] = task
        self._threads[job.id] = thread
        thread.start()

    def _next_queued(self) -> Job | None:
        """The next job allowed to start, honoring named queues: queue order
        (priority), pause, per-queue schedules, per-queue concurrency
        (1 = sequential mode), queue dependencies, and job-level
        'start B after A finishes' links."""
        jobs = self.db.list_jobs()
        queues = {queue.id: queue for queue in self.db.list_queues()}
        by_id = {job.id: job for job in jobs}
        active_in_queue: dict[int | None, int] = {}
        unfinished_in_queue: dict[int | None, int] = {}
        for job in jobs:
            if job.id in self._active:
                active_in_queue[job.queue_id] = active_in_queue.get(job.queue_id, 0) + 1
            if job.status in (
                JobStatus.QUEUED,
                JobStatus.DOWNLOADING,
                JobStatus.PAUSED,
            ) or (job.status is JobStatus.FAILED and job.id in self._retry_at):
                unfinished_in_queue[job.queue_id] = unfinished_in_queue.get(job.queue_id, 0) + 1

        def queue_position(job: Job) -> int:
            queue = queues.get(job.queue_id) if job.queue_id else None
            return queue.position if queue is not None else 0

        candidates = [
            job for job in jobs if job.status is JobStatus.QUEUED and job.id not in self._active
        ]
        # Stable sort: queue order first, job priority (list_jobs order) within.
        candidates.sort(key=queue_position)
        now = datetime.now()
        for job in candidates:
            if self._job_allowed(job, queues, by_id, active_in_queue, unfinished_in_queue, now):
                return job
        return None

    def _job_allowed(
        self,
        job: Job,
        queues: Mapping[int, Queue],
        by_id: Mapping[int, Job],
        active_in_queue: Mapping[int | None, int],
        unfinished_in_queue: Mapping[int | None, int],
        now: datetime,
    ) -> bool:
        start_at = job.options.get("start_at")
        if start_at:
            # Download later: hold until the chosen moment (bad values ignore).
            with contextlib.suppress(ValueError, TypeError):
                if now < datetime.fromisoformat(str(start_at)):
                    return False
        after = int(job.options.get("after_job") or 0)
        if after:
            dependency = by_id.get(after)
            # A deleted dependency no longer blocks; a cancelled one counts as
            # finished, everything else (incl. pending retries) still blocks.
            if dependency is not None and dependency.status not in (
                JobStatus.COMPLETED,
                JobStatus.CANCELLED,
            ):
                return False
        queue = queues.get(job.queue_id) if job.queue_id else None
        if queue is None:
            return True
        if queue.paused:
            return False
        if queue.schedule_enabled and not _in_window(now, queue.start_time, queue.stop_time):
            return False
        if queue.depends_on and unfinished_in_queue.get(queue.depends_on, 0) > 0:
            return False
        at_cap = (
            queue.max_concurrent > 0 and active_in_queue.get(queue.id, 0) >= queue.max_concurrent
        )
        return not at_cap

    def _promote_due_retries(self) -> None:
        if not self._retry_at:
            return
        now = time.monotonic()
        for job_id, deadline in list(self._retry_at.items()):
            if now < deadline:
                continue
            del self._retry_at[job_id]
            job = self.db.get_job(job_id)
            if job is not None and job.status is JobStatus.FAILED:
                log.info("auto-retrying job %s (attempt %s)", job_id, job.retry_count)
                self.db.set_job_status(job_id, JobStatus.QUEUED)

    def _schedule_retry(self, job_id: int) -> bool:
        """Queue an automatic retry for a transiently-failed job. Returns True
        if one was scheduled. A max of 0 means retry forever (with the backoff
        capped, so a dead network is re-probed every few minutes until it
        returns - this is what survives reboots of the router or a VPN)."""
        if not self.settings.auto_retry:
            return False
        job = self.db.get_job(job_id)
        if job is None or not _is_transient_error(job.error):
            return False
        max_retries = self.settings.auto_retry_max
        if max_retries and job.retry_count >= max_retries:
            return False
        count = job.retry_count + 1
        self.db.set_retry_count(job_id, count)
        delay = min(_RETRY_BASE_SECONDS * 2 ** (count - 1), _RETRY_CAP_SECONDS)
        self._retry_at[job_id] = time.monotonic() + delay
        log.info("job %s failed; auto-retry %s scheduled in %.0fs", job_id, count, delay)
        return True

    def _try_mirror(self, job_id: int) -> bool:
        """A job that failed for good on its URL switches to its next mirror
        (the alternate stream URLs the browser sniffed on the page) and starts
        over. Returns True if a mirror was queued."""
        job = self.db.get_job(job_id)
        if job is None:
            return False
        mirrors = [m for m in (job.options.get("mirrors") or []) if isinstance(m, str)]
        if not mirrors:
            return False
        next_url = mirrors.pop(0)
        options = dict(job.options)
        options["mirrors"] = mirrors
        self.db.update_job_options(job_id, options)
        # A different URL is a different file as far as resume is concerned:
        # drop the partial data and checkpoints so nothing gets stitched wrong.
        with contextlib.suppress(OSError):
            job.part_path.unlink(missing_ok=True)
        self.db.replace_segments(job_id, [])
        self.db.update_job_url(job_id, next_url)
        self.db.set_retry_count(job_id, 0)
        self.db.set_job_status(job_id, JobStatus.QUEUED)
        log.info("job %s failed on its URL; trying mirror %s", job_id, next_url)
        return True

    def torrent_upload_rate(self) -> float:
        """Live upload throughput to torrent peers (dashboard upload graph)."""
        return TORRENT_SESSION.upload_rate()

    def torrent_stats(self, job_id: int) -> TorrentStats | None:
        """Live swarm stats (seeds/peers/ratio/up/down) for a torrent job, for
        the detail panel's Peers tab. None when it isn't a torrent, has no
        recorded info-hash yet, or isn't currently in the session."""
        job = self.db.get_job(job_id)
        if job is None or job.kind is not JobKind.TORRENT:
            return None
        info_hash = job.options.get("info_hash")
        if not info_hash:
            return None
        return TORRENT_SESSION.stats_for(str(info_hash))

    def stat_totals(self) -> dict[str, int]:
        """Downloaded-bytes rollups for the dashboard: today / this week /
        this month / lifetime, plus the lifetime file count."""
        from datetime import timedelta

        today = datetime.now().date()
        week_start = today - timedelta(days=today.weekday())
        month_start = today.replace(day=1)
        lifetime, files = self.db.lifetime_bytes()
        return {
            "today": self.db.bytes_since(today.strftime("%Y-%m-%d")),
            "week": self.db.bytes_since(week_start.strftime("%Y-%m-%d")),
            "month": self.db.bytes_since(month_start.strftime("%Y-%m-%d")),
            "lifetime": lifetime,
            "files": files,
        }

    def stats_by_host(self, limit: int = 10) -> list[tuple[str, int, int]]:
        return self.db.bytes_by_host(limit)

    def stats_by_category(self) -> list[tuple[str, int, int]]:
        return self.db.bytes_by_category()

    def _record_completion(self, job_id: int) -> None:
        """Add a finished download's bytes to the dashboard stats, bucketed by
        its category and server host."""
        fresh = self.db.get_job(job_id)
        if fresh is None:
            return
        if not self.settings.stats_enabled:
            return
        byte_count = fresh.total_size or fresh.downloaded
        if byte_count <= 0:
            return
        category = categories.category_for(fresh.filename) or ""
        host = urlsplit(fresh.url).hostname or ""
        with contextlib.suppress(Exception):  # stats must never fail a download
            self.db.record_download(category, host, byte_count)

    def _run_job(self, job: Job, task: DownloadTask) -> None:
        status = JobStatus.FAILED
        try:
            status = task.run()
            log.info("job %s (%s) finished with status %s", job.id, job.filename, status.value)
        except Exception:
            # A bug in an engine must never strand a job: without this, the
            # thread died mid-flight and the row stayed "Downloading" forever
            # (the Windows fsync crash in the HLS finalizer did exactly that).
            log.exception("job %s (%s) crashed", job.id, job.filename)
            with contextlib.suppress(Exception):
                self.db.set_job_status(
                    job.id, JobStatus.FAILED, error="unexpected internal error (see log)"
                )
        finally:
            with self._cond:
                self._active.pop(job.id, None)
                self._threads.pop(job.id, None)
                if job.id in self._pending_removal:
                    # Force-removed mid-flight: drop it, don't retry or record.
                    self._pending_removal.discard(job.id)
                    self._purge_job(job.id)
                elif status is JobStatus.COMPLETED:
                    # Settle history back to insertion order and forget retries.
                    self.db.set_priority(job.id, 0)
                    self.db.set_retry_count(job.id, 0)
                    self._retry_at.pop(job.id, None)
                    self._job_limiters.pop(job.id, None)
                    self._fair_limiters.pop(job.id, None)
                    self._fair_applied.pop(job.id, None)
                    self._record_completion(job.id)
                elif status is JobStatus.FAILED:
                    if not self._schedule_retry(job.id):
                        self._try_mirror(job.id)
                self._cond.notify_all()
