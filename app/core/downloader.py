"""The segmented downloader: N parallel range connections with checkpointed,
crash-safe resume (F0.1, F0.2).

Crash-safety model
------------------
Workers write with unbuffered file handles (``buffering=0``), so every byte a
worker counts as downloaded has already been handed to the OS page cache -
which survives a kill -9 of this process. Segment progress is checkpointed to
SQLite (WAL) shortly *after* the bytes are written, never before, so a recorded
offset can only ever lag the file, and resuming from it merely rewrites a few
already-correct bytes. A crash therefore loses at most one checkpoint interval
of progress, never integrity.
"""

from __future__ import annotations

import logging
import os
import random
import re
import shutil
import threading
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import IO

import httpx

from app.core import naming, net
from app.core.errors import DownloadError
from app.core.models import Job, JobStatus, Segment
from app.core.probe import ProbeResult, probe
from app.core.ratelimit import RateLimiter
from app.db.database import Database

log = logging.getLogger(__name__)

MIN_SEGMENT_SIZE = 256 * 1024
DEFAULT_CONNECTIONS = 8
DEFAULT_CHUNK_SIZE = 64 * 1024

#: Statuses that mean "try this segment again" rather than "the job is dead":
#: rate limits, transient server faults, and the auth-flavored ones a refreshed
#: (re-signed) URL usually cures.
_RETRYABLE_STATUS = frozenset({403, 408, 409, 425, 429, 500, 502, 503, 504})

_CONTENT_RANGE_START = re.compile(r"\s*bytes\s+(\d+)-")


class StopReason(Enum):
    NONE = "none"
    PAUSE = "pause"
    CANCEL = "cancel"
    ERROR = "error"


class _Retry(Exception):
    """Internal: the current attempt failed but the segment may be retried."""


def _reserve_space(handle: IO[bytes], total: int) -> bool:
    """Ask the filesystem to set aside *total* bytes, up front, for this file.
    Returns False when it won't - the caller then falls back to a sparse file.

    ``os.fallocate``, deliberately, and not ``os.posix_fallocate``: the latter
    emulates itself by writing zeros over the whole file when the filesystem
    can't reserve space (exFAT, most network mounts), so a 20GB download would
    sit at 0% for minutes before its first byte landed. The raw syscall just
    reports that it can't.
    """
    reserve = getattr(os, "fallocate", None)  # Linux only
    if reserve is None:
        return False
    try:
        reserve(handle.fileno(), 0, 0, total)
    except OSError:
        return False
    return True


def plan_segments(total_size: int, connections: int) -> list[tuple[int, int | None]]:
    """Split [0, total_size) into contiguous inclusive byte ranges."""
    if total_size <= 0:
        return [(0, None)]
    count = max(1, min(connections, total_size // MIN_SEGMENT_SIZE))
    base, extra = divmod(total_size, count)
    spans: list[tuple[int, int | None]] = []
    offset = 0
    for index in range(count):
        length = base + (1 if index < extra else 0)
        spans.append((offset, offset + length - 1))
        offset += length
    return spans


class _Checkpointer:
    """Batches segment progress and flushes it to SQLite on an interval."""

    def __init__(self, db: Database, interval: float) -> None:
        self._db = db
        self._interval = interval
        self._dirty: dict[int, int] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="gl-checkpoint", daemon=True)
        self._started = False

    def start(self) -> None:
        self._started = True
        self._thread.start()

    def report(self, segment_id: int, downloaded: int) -> None:
        with self._lock:
            self._dirty[segment_id] = downloaded

    def flush(self) -> None:
        with self._lock:
            dirty, self._dirty = self._dirty, {}
        if not dirty:
            return
        try:
            self._db.update_segment_progress(dirty)
        except Exception:
            # Put the progress back so the next flush retries it. Dropping it
            # would silently stop checkpointing and turn a later crash into a
            # restart from the last good offset, minutes or hours back.
            with self._lock:
                for segment_id, downloaded in dirty.items():
                    self._dirty.setdefault(segment_id, downloaded)
            raise

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.flush()
            except Exception:
                # A failed write must not take the thread with it: without
                # this, one busy-timeout ends checkpointing for the whole
                # download and resume data quietly goes stale.
                log.warning("checkpoint flush failed; will retry", exc_info=True)

    def close(self) -> None:
        self._stop.set()
        if self._started and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self.flush()


class SegmentedDownload:
    """Runs one job to completion (or pause/cancel/failure). One-shot object."""

    def __init__(
        self,
        db: Database,
        job: Job,
        *,
        connections: int = DEFAULT_CONNECTIONS,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        max_retries: int = 5,
        retry_backoff: float = 0.25,
        checkpoint_interval: float = 0.3,
        limiter: RateLimiter | None = None,
        job_limiter: RateLimiter | None = None,
        host_limiter: RateLimiter | None = None,
        connections_target: Callable[[], int] | None = None,
        shares_budget: bool = False,
        proxy: str | None = None,
        headers: dict[str, str] | None = None,
        bypass_hosts: tuple[str, ...] = (),
        user_agent: str | None = None,
    ) -> None:
        self.db = db
        self.job = job
        self.connections = connections
        self.chunk_size = chunk_size
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.limiter = limiter
        # How many connections this download may run *right now*. With several
        # downloads active it returns each one's fair share of the budget, live,
        # so they split the line instead of the first one hogging it; None (CLI,
        # tests) means "use the full fixed count". ``shares_budget`` marks a
        # download that participates in that split (an unpinned one).
        self._connections_target_fn = connections_target
        self.shares_budget = shares_budget
        # Extra caps applied in series with the global one; the tightest wins,
        # which is exactly right. job_limiter = this download's own cap;
        # host_limiter = shared across every download from the same server.
        self.job_limiter = job_limiter
        self.host_limiter = host_limiter
        self._client = net.build_client(
            proxy=proxy,
            bypass_hosts=bypass_hosts,
            user_agent=user_agent,
            follow_redirects=True,
            # HTTP/1.1 on purpose. This is a segmented downloader: its whole
            # point is N range requests carried on N *separate* TCP connections,
            # each with its own congestion window, so they add up. HTTP/2 would
            # multiplex all N onto a single socket (one window), collapsing the
            # accelerator to roughly single-connection speed on any CDN that
            # offers h2 - the reason "8 connections" used to crawl. Keep the
            # pool large enough to hold one live connection per segment so a
            # high connection count doesn't churn through reconnects.
            http2=False,
            timeout=httpx.Timeout(30.0, connect=15.0),
            limits=httpx.Limits(
                max_connections=connections + 2,
                max_keepalive_connections=connections + 2,
            ),
            # identity so the bytes on the wire are the bytes of the file: with
            # transparent gzip, httpx decompresses while Content-Length and the
            # byte ranges still describe the compressed stream, which stitches
            # segments at wrong offsets and trips the final size check.
            headers={"Accept-Encoding": "identity", **(headers or {})},
        )
        self._checkpointer = _Checkpointer(db, checkpoint_interval)
        self._segments: list[Segment] = []
        self._stop_event = threading.Event()
        self._reason = StopReason.NONE
        self._reason_lock = threading.Lock()
        self._steal_lock = threading.Lock()
        self._error: str | None = None
        #: Cleared when the resolved URL stops working (expired signature), so
        #: requests fall back to the original URL and its redirect chain.
        self._final_url_ok = True
        # Worker pool: segment ids currently owned by a worker, the live worker
        # count, and the threads. The pool size tracks the connection target.
        self._claimed: set[int] = set()
        self._worker_lock = threading.Lock()
        self._active_workers = 0
        self._pool: list[threading.Thread] = []
        self._worker_seq = 0

    # ------------------------------------------------------------ control

    def pause(self) -> None:
        self._request_stop(StopReason.PAUSE)

    def cancel(self) -> None:
        self._request_stop(StopReason.CANCEL)

    @property
    def bytes_downloaded(self) -> int:
        return sum(segment.downloaded for segment in self._segments)

    # ---------------------------------------------------------------- run

    def run(self) -> JobStatus:
        self.db.set_job_status(self.job.id, JobStatus.DOWNLOADING)
        try:
            return self._run()
        except DownloadError as exc:
            return self._finish_failed(str(exc))
        except Exception:
            log.exception("unexpected error while downloading job %s", self.job.id)
            return self._finish_failed("unexpected internal error (see log)")
        finally:
            self._checkpointer.close()
            self._client.close()

    def _run(self) -> JobStatus:
        self._prepare()
        if self._stop_event.is_set():
            return self._settle_stopped()
        if any(not segment.is_complete for segment in self._segments):
            self._checkpointer.start()
            self._run_workers()
            self._checkpointer.close()
        if self._reason is StopReason.ERROR:
            return self._finish_failed(self._error or "download failed")
        if self._reason is not StopReason.NONE:
            return self._settle_stopped()
        return self._finalize()

    # -------------------------------------------------------- preparation

    def _prepare(self) -> None:
        job = self.job
        segments = self.db.segments_for(job.id)
        # Resolve already probed this URL seconds ago. Reusing that result on a
        # fresh job skips a second DNS+TLS+Range round-trip (often the whole
        # multi-second "nothing happening" gap before workers start). Resume
        # always re-probes so ETag / size changes still force a restart.
        if segments:
            result = probe(self._client, job.url)
        else:
            cached = self._probe_from_job()
            if cached is not None:
                log.debug("job %s: reusing resolve-time probe", job.id)
                result = cached
            else:
                result = probe(self._client, job.url)
        part = job.part_path
        if segments and self._must_restart(result, segments, part):
            log.info("job %s: remote file changed or part missing; restarting", job.id)
            part.unlink(missing_ok=True)
            segments = []
        if not segments:
            if result.filename:
                job.filename = naming.sanitize_filename(result.filename)
                part = job.part_path
            job.final_url = result.final_url
            job.total_size = result.total_size
            job.resumable = result.resumable
            job.etag = result.etag
            job.last_modified = result.last_modified
            self.db.update_job_probe(job)
            if result.resumable and result.total_size:
                spans = plan_segments(result.total_size, self.connections)
            else:
                end = result.total_size - 1 if result.total_size is not None else None
                spans = [(0, end)]
            segments = self.db.replace_segments(job.id, spans)
        self._segments = segments
        self._preallocate(part)

    def _probe_from_job(self) -> ProbeResult | None:
        """Rebuild a ProbeResult from fields written at add/resolve time."""
        job = self.job
        if job.final_url is None:
            return None
        return ProbeResult(
            final_url=job.final_url,
            resumable=job.resumable,
            total_size=job.total_size,
            etag=job.etag,
            last_modified=job.last_modified,
            filename=None,
            content_type=None,
        )

    def _must_restart(self, result: ProbeResult, segments: list[Segment], part: Path) -> bool:
        job = self.job
        if not part.exists() and any(segment.downloaded for segment in segments):
            return True
        if not result.resumable and any(segment.downloaded for segment in segments):
            return True  # server no longer honors ranges; offsets are unusable
        if job.etag and result.etag:
            return job.etag != result.etag
        if job.last_modified and result.last_modified:
            return job.last_modified != result.last_modified
        if job.total_size is not None and result.total_size is not None:
            return job.total_size != result.total_size
        return False

    #: Refuse to fill the disk to the brim (S6).
    DISK_SPACE_MARGIN = 64 * 1024 * 1024

    def _preallocate(self, part: Path) -> None:
        part.parent.mkdir(parents=True, exist_ok=True)
        total = self.job.total_size
        if total:
            already = part.stat().st_size if part.exists() else 0
            free = shutil.disk_usage(part.parent).free
            if free < total - already + self.DISK_SPACE_MARGIN:
                raise DownloadError(
                    "not enough free disk space for this download "
                    f"(need {total} bytes plus headroom)"
                )
        if not part.exists():
            part.touch()
        if total:
            with open(part, "r+b") as handle:
                handle.seek(0, os.SEEK_END)
                current = handle.tell()
                if current > total:
                    # fallocate never shrinks, so an oversized leftover part
                    # must be truncated or _finalize fails its size check.
                    handle.truncate(total)
                elif current < total and not _reserve_space(handle, total):
                    handle.truncate(total)  # sparse, and instant

    # ------------------------------------------------------------ workers

    def _target_connections(self) -> int:
        """This download's connection budget right now - its fair share when
        siblings run, the full fixed count otherwise."""
        if self._connections_target_fn is None:
            return self.connections
        try:
            return max(1, self._connections_target_fn())
        except Exception:  # a broken callback must never stall the download
            log.debug("connection-target callback failed", exc_info=True)
            return self.connections

    def _run_workers(self) -> None:
        """Supervise a pool of connection workers whose size follows the live
        target. Each worker claims an incomplete segment (or steals a tail) and
        retires itself when the target drops, so concurrent downloads share the
        connection budget fairly - and reclaim it as siblings finish."""
        while not self._stop_event.is_set():
            with self._worker_lock:
                self._pool = [worker for worker in self._pool if worker.is_alive()]
            if all(segment.is_complete for segment in self._segments):
                break
            self._spawn_to_target()
            self._stop_event.wait(0.25)
        for worker in list(self._pool):
            worker.join()

    def _spawn_to_target(self) -> None:
        # Read the target and probe for work outside the worker lock, so it never
        # nests with the steal lock or the manager's scheduler lock.
        while not self._stop_event.is_set():
            target = self._target_connections()
            if not self._has_claimable_work():
                return
            with self._worker_lock:
                if self._active_workers >= target:
                    return
                self._active_workers += 1  # reserve the slot before the thread runs
                self._worker_seq += 1
                worker = threading.Thread(
                    target=self._worker_loop,
                    name=f"gl-seg-{self.job.id}-{self._worker_seq}",
                    daemon=True,
                )
                self._pool.append(worker)
            worker.start()

    def _worker_loop(self) -> None:
        retired = False
        try:
            with open(self.job.part_path, "r+b", buffering=0) as handle:
                while not self._stop_event.is_set():
                    segment = self._next_work()
                    if segment is None:
                        break  # nothing left to claim or steal
                    try:
                        self._download_segment(handle, segment)
                    finally:
                        with self._steal_lock:
                            self._claimed.discard(segment.id)
                    if self._stop_event.is_set() or self._retire_if_over_target():
                        retired = True
                        break
        except DownloadError as exc:
            self._record_error(str(exc))
        except Exception as exc:
            log.exception("job %s worker crashed", self.job.id)
            self._record_error(str(exc))
        finally:
            if not retired:  # _retire_if_over_target already released the slot
                with self._worker_lock:
                    self._active_workers -= 1

    def _retire_if_over_target(self) -> bool:
        """Give a connection back to a sibling download when this one is over its
        fair share. Decrements the count under the lock so two workers can't both
        decide to leave and overshoot."""
        with self._worker_lock:
            if self._active_workers > self._target_connections():
                self._active_workers -= 1
                return True
        return False

    def _next_work(self) -> Segment | None:
        """The next segment for a free worker: an unclaimed incomplete segment
        if one exists (covers a pool smaller than the segment count), otherwise
        the tail split off the biggest in-progress segment."""
        with self._steal_lock:
            for segment in self._segments:
                if segment.id not in self._claimed and not segment.is_complete:
                    self._claimed.add(segment.id)
                    return segment
        # _steal_segment claims the tail it creates before releasing the lock.
        # Claiming out here instead would leave the new segment visible and
        # unclaimed for an instant, long enough for another worker's loop above
        # to take it too - two workers sharing one Segment then race its
        # `downloaded` counter and write their bytes at each other's offsets.
        return self._steal_segment()

    def _has_claimable_work(self) -> bool:
        """Is there work a newly-spawned worker could pick up - an unclaimed
        incomplete segment, or a claimed one big enough to split?"""
        with self._steal_lock:
            for segment in self._segments:
                if segment.id not in self._claimed and not segment.is_complete:
                    return True
            for segment in self._segments:
                if segment.end is None:
                    continue
                remaining = segment.end - (segment.start + segment.downloaded) + 1
                if remaining >= self.STEAL_THRESHOLD:
                    return True
        return False

    def _download_segment(self, handle: IO[bytes], segment: Segment) -> None:
        attempts = 0
        while not self._stop_event.is_set():
            downloaded_before = segment.downloaded
            try:
                # An unsized segment can't be range-requested even on a server
                # that honors ranges (a 206 with "bytes 0-0/*" reports resumable
                # with no total). Streaming it whole is the only way it can ever
                # finish; the alternative used to be an instant hard failure.
                if self.job.resumable and segment.end is not None:
                    self._stream_range(handle, segment)
                else:
                    self._stream_full(handle, segment)
                return
            except (httpx.TransportError, _Retry) as exc:
                if segment.downloaded > downloaded_before:
                    attempts = 0  # forward progress earns fresh retries
                attempts += 1
                if attempts >= 2:
                    # Repeated failures with nothing to show for them suggest
                    # the resolved URL itself has gone bad (expired signature,
                    # dead CDN node); the original URL will redirect afresh.
                    self._distrust_final_url()
                if attempts > self.max_retries:
                    raise DownloadError(
                        f"segment {segment.index}: giving up after "
                        f"{self.max_retries} retries ({exc})"
                    ) from exc
                # Exponential backoff with jitter: spreads out retries
                # so many segments failing at once don't hammer in sync.
                capped = min(self.retry_backoff * 2 ** (attempts - 1), 5.0)
                delay = capped * (0.5 + random.random() * 0.5)
                self._stop_event.wait(delay)

    #: A segment must have at least this much left to be worth splitting; each
    #: half then stays above MIN_SEGMENT_SIZE.
    STEAL_THRESHOLD = 2 * MIN_SEGMENT_SIZE

    def _steal_segment(self) -> Segment | None:
        """Split the tail off the segment with the most bytes remaining and
        return it as fresh work, so a finished connection keeps pulling."""
        if not self.job.resumable:
            return None  # a single non-range stream cannot be split
        with self._steal_lock:
            victim: Segment | None = None
            best = 0
            for seg in self._segments:
                if seg.end is None:
                    continue
                remaining = seg.end - (seg.start + seg.downloaded) + 1
                if remaining > best:
                    best, victim = remaining, seg
            if victim is None or victim.end is None or best < self.STEAL_THRESHOLD:
                return None
            old_end = victim.end
            mid = victim.start + victim.downloaded + best // 2
            # Shrink the busy segment (it re-reads .end each chunk and stops);
            # the tail becomes a new segment this connection takes over.
            victim.end = mid - 1
            self.db.set_segment_end(victim.id, mid - 1)
            new_index = max(seg.index for seg in self._segments) + 1
            new_segment = self.db.add_segment(self.job.id, new_index, mid, old_end)
            self._segments.append(new_segment)
            self._claimed.add(new_segment.id)  # claimed before it becomes visible
            log.debug("job %s: split segment %s at %s", self.job.id, victim.index, mid)
            return new_segment

    def _request_url(self) -> str:
        """Where segment requests go.

        The probe already followed the redirect chain and stored where it
        landed, so asking for job.url again makes every connection - and every
        retry, and every steal - re-pay that chain (a DNS + TCP + TLS + RTT per
        hop) before its first payload byte. Signed CDN URLs do expire, though,
        so a failure downgrades us to the original URL, which hands out a fresh
        one.
        """
        if self._final_url_ok and self.job.final_url:
            return self.job.final_url
        return self.job.url

    def _distrust_final_url(self) -> None:
        if self._final_url_ok and self.job.final_url:
            log.debug("job %s: falling back to the original URL", self.job.id)
            self._final_url_ok = False

    def _reject_status(self, status: int, segment: Segment) -> None:
        """Never returns: raise the right kind of failure for a bad status.

        Retryable statuses must not become a DownloadError - that stops every
        other connection on the job, and any 4xx also matches the manager's
        "permanent failure" marker, so one flaky CDN response would throw away
        a nearly-complete download for good.
        """
        if status in _RETRYABLE_STATUS:
            self._distrust_final_url()
            raise _Retry(f"segment {segment.index}: HTTP {status}")
        raise DownloadError(f"server responded with HTTP {status} for segment {segment.index}")

    @staticmethod
    def _check_range_start(response: httpx.Response, offset: int, segment: Segment) -> None:
        """A 206 that answers a *different* range than we asked for would be
        written at the requested offset, silently corrupting the file."""
        match = _CONTENT_RANGE_START.match(response.headers.get("content-range", ""))
        if match is None:
            return  # no parsable header: trust the status, as before
        if int(match.group(1)) != offset:
            raise _Retry(
                f"segment {segment.index}: server answered byte {match.group(1)}, expected {offset}"
            )

    def _stream_range(self, handle: IO[bytes], segment: Segment) -> None:
        end = segment.end
        if end is None:  # resumable jobs always have sized segments
            raise DownloadError(f"segment {segment.index} has no end offset")
        offset = segment.start + segment.downloaded
        if offset > end:
            return
        headers = {"Range": f"bytes={offset}-{end}"}
        with self._client.stream("GET", self._request_url(), headers=headers) as response:
            if response.status_code != 206:
                self._reject_status(response.status_code, segment)
            self._check_range_start(response, offset, segment)
            for chunk in response.iter_bytes(self.chunk_size):
                if self._stop_event.is_set():
                    return
                if not chunk:
                    continue
                # Re-read .end each chunk: a steal may have shrunk this segment,
                # in which case we stop at the new boundary.
                cap = segment.end if segment.end is not None else end
                remaining = cap - (segment.start + segment.downloaded) + 1
                if remaining <= 0:
                    return
                data = chunk[:remaining]
                self._write_at(handle, data, segment.start + segment.downloaded)
                segment.downloaded += len(data)
                self._checkpointer.report(segment.id, segment.downloaded)
                self._throttle(len(data))
        final_end = segment.end if segment.end is not None else end
        if segment.start + segment.downloaded <= final_end:
            raise _Retry("server closed the connection early")

    def _throttle(self, amount: int) -> None:
        # Hand the stop event to each limiter: at a tight cap the sleep here
        # dwarfs the transfer, and Pause has to land during it, not after.
        for limiter in (self.limiter, self.job_limiter, self.host_limiter):
            if limiter is not None:
                limiter.throttle(amount, self._stop_event)

    def _stream_full(self, handle: IO[bytes], segment: Segment) -> None:
        """Single-connection fallback for servers without range support.

        Not resumable: an interrupted attempt restarts from byte zero.
        """
        if segment.downloaded:
            segment.downloaded = 0
            self._checkpointer.report(segment.id, 0)
        with self._client.stream("GET", self._request_url()) as response:
            if response.status_code != 200:
                self._reject_status(response.status_code, segment)
            for chunk in response.iter_bytes(self.chunk_size):
                if self._stop_event.is_set():
                    return
                if not chunk:
                    continue
                self._write_at(handle, chunk, segment.start + segment.downloaded)
                segment.downloaded += len(chunk)
                self._checkpointer.report(segment.id, segment.downloaded)
                self._throttle(len(chunk))
        if segment.end is None:
            # Stream ended cleanly: now we finally know the size.
            segment.end = segment.downloaded - 1
            self.db.set_segment_end(segment.id, segment.end)
            handle.truncate(segment.downloaded)
        elif segment.downloaded < segment.end - segment.start + 1:
            raise _Retry("server closed the connection early")

    @staticmethod
    def _write_at(handle: IO[bytes], data: bytes, position: int) -> None:
        handle.seek(position)
        view = memoryview(data)
        while view:
            written = handle.write(view)  # raw handles may write partially
            view = view[written:]

    # --------------------------------------------------------- completion

    def _finalize(self) -> JobStatus:
        job = self.job
        incomplete = [segment for segment in self._segments if not segment.is_complete]
        if incomplete:
            return self._finish_failed(
                f"{len(incomplete)} segment(s) ended incomplete; please retry"
            )
        part = job.part_path
        naming.fsync_before_rename(part)
        actual_size = part.stat().st_size
        if job.total_size is None:
            job.total_size = actual_size
            self.db.update_job_total(job.id, actual_size)
        elif actual_size != job.total_size:
            return self._finish_failed(
                f"size mismatch after download (expected {job.total_size}, got {actual_size})"
            )
        dest = naming.unique_path(job.dest_path)
        os.replace(part, dest)
        if dest.name != job.filename:
            job.filename = dest.name
            self.db.update_job_filename(job.id, dest.name)
        # The job's own row keeps the final size, so its segment rows have
        # nothing left to say. Dropping them keeps the per-poll
        # all_segment_progress() summing over live downloads instead of over
        # every segment of every file ever downloaded.
        self.db.update_job_downloaded(job.id, job.total_size or actual_size)
        self.db.clear_segments(job.id)
        self.db.set_job_status(job.id, JobStatus.COMPLETED)
        return JobStatus.COMPLETED

    def _settle_stopped(self) -> JobStatus:
        if self._reason is StopReason.CANCEL:
            self.job.part_path.unlink(missing_ok=True)
            self.db.clear_segments(self.job.id)
            self.db.set_job_status(self.job.id, JobStatus.CANCELLED)
            return JobStatus.CANCELLED
        self.db.set_job_status(self.job.id, JobStatus.PAUSED)
        return JobStatus.PAUSED

    def _finish_failed(self, message: str) -> JobStatus:
        log.warning("job %s failed: %s", self.job.id, message)
        self.db.set_job_status(self.job.id, JobStatus.FAILED, error=message)
        return JobStatus.FAILED

    # ------------------------------------------------------------- helpers

    def _request_stop(self, reason: StopReason) -> None:
        with self._reason_lock:
            if self._reason is StopReason.NONE:
                self._reason = reason
        self._stop_event.set()

    def _record_error(self, message: str) -> None:
        with self._reason_lock:
            if self._reason is StopReason.NONE:
                self._reason = StopReason.ERROR
                self._error = message
        self._stop_event.set()
