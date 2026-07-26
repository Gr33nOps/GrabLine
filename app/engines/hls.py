"""HLS/DASH reassembly (F2.1): FFmpeg copies the stream into a clean .mp4.

Robustness beyond the Phase 1 core:
- a chosen master-playlist variant (``options["variant_url"]``) is downloaded
  instead of letting FFmpeg pick, and a separate audio rendition
  (``options["audio_url"]``) is muxed in alongside it;
- ``-progress`` output plus the playlist's summed #EXTINF durations give a
  self-correcting total-size estimate, so the UI can show a real percentage;
- one automatic retry on transient failures (nonzero exit or a stall), since
  a CDN hiccup should not kill a 40-minute reassembly for good.

The native fetch (the default for a resolvable media playlist) is resumable at
segment granularity: pausing keeps the segments already on disk and the next
run fetches only what is missing, so a paused multi-GB stream does not start
over. The fallback where FFmpeg fetches the stream directly is not resumable
and restarts from the beginning.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import IO
from urllib.parse import urljoin

import httpx

from app.core import naming, net, proc
from app.core.models import Job, JobStatus
from app.db.database import Database
from app.engines.manifest import is_master_playlist, playlist_duration

#: The URI="..." on an #EXT-X-KEY / #EXT-X-MAP tag.
_TAG_URI = re.compile(r'URI="([^"]*)"')
#: Local segment fetch concurrency. An HLS stream is thousands of small parts,
#: so real parallelism is what makes it fast; 8 was too few to hide the latency
#: of a slow CDN. A hung part must never stall the rest (see the retry below).
_SEGMENT_WORKERS = 16
#: Per-segment retry budget. A stalled or dropped connection is abandoned and
#: retried on a fresh one instead of holding a worker for the whole read timeout
#: - the "0-byte .part sitting for minutes" that collapsed throughput to a crawl.
_SEGMENT_RETRIES = 4
#: Read timeout per segment: short, so a server that accepts the connection then
#: sends nothing is dropped in seconds and retried, not after a full minute.
_SEGMENT_READ_TIMEOUT = 20.0

log = logging.getLogger(__name__)

_ESTIMATE_MIN_SECONDS = 5.0  # muxed seconds before the size estimate is trusted

#: FFmpeg input protocols a remote HLS/DASH stream legitimately uses - and
#: nothing more. http/https for manifest+segments, tcp/tls underneath, crypto
#: for AES-128 keys, data for inline base64 keys. No file:, concat:, subfile:,
#: pipe: or exotic protocols. See _command (CWE-668 / CWE-918).
_INPUT_PROTOCOLS = "http,https,tcp,tls,crypto,data"


class HlsDownload:
    """Runs one HLS job via FFmpeg. One-shot object."""

    def __init__(
        self,
        db: Database,
        job: Job,
        *,
        ffmpeg_path: str | None,
        persist_interval: float = 0.5,
        stall_timeout: float = 90.0,
        max_attempts: int = 2,
        proxy: str | None = None,
    ) -> None:
        self.db = db
        self.job = job
        self.ffmpeg_path = ffmpeg_path
        self.persist_interval = persist_interval
        self.stall_timeout = stall_timeout
        self.max_attempts = max_attempts
        self.proxy = proxy
        self._stop_event = threading.Event()
        self._cancelled = False
        self._downloaded = 0
        self._out_time = 0.0  # seconds muxed so far, from -progress
        self._size_ema: float | None = None  # smoothed total-size estimate
        self._est_ref: tuple[int, float] | None = None  # (bytes, out_time) anchor
        self._duration: float | None = None
        self._failure = "FFmpeg could not process this stream"
        options = job.options or {}
        self._input_url = str(options.get("variant_url") or job.url)
        audio = options.get("audio_url")
        self._audio_url = str(audio) if audio else None
        # Cookie/Referer/User-Agent from a browser handoff. Many CDNs check
        # Referer against the page that requested the stream and refuse it -
        # or serve an HTML error page FFmpeg can't parse - without one, so
        # these ride along into both the manifest fetch and every FFmpeg
        # request (see _command).
        raw_headers = options.get("http_headers")
        self._headers: dict[str, str] = dict(raw_headers) if isinstance(raw_headers, dict) else {}

    def pause(self) -> None:
        self._stop_event.set()

    def cancel(self) -> None:
        self._cancelled = True
        self._stop_event.set()

    @property
    def bytes_downloaded(self) -> int:
        return self._downloaded

    def run(self) -> JobStatus:
        self.db.set_job_status(self.job.id, JobStatus.DOWNLOADING)
        if not self.ffmpeg_path:
            return self._finish_failed(
                "FFmpeg is required to save this stream. Install it from Settings"
            )
        fetched = self._fetch_playlist()
        playlist_text = fetched[0] if fetched else None
        base_url = fetched[1] if fetched else self._input_url
        live_error = self._detect_live_playlist(playlist_text)
        if live_error:
            return self._finish_failed(live_error)
        if playlist_text is not None:
            self._duration = playlist_duration(playlist_text)
        part = self.job.part_path
        part.parent.mkdir(parents=True, exist_ok=True)

        # Preferred path for a resolvable HLS media playlist: fetch the manifest,
        # every segment, the AES key and the init segment with the app's own HTTP
        # client - the one that applies the browser headers, follows redirects and
        # speaks HTTP/2 - then let FFmpeg remux the *local* copies. FFmpeg makes
        # no network request, so a gated CDN its own client can't satisfy (the
        # "Invalid data found when processing input" failures) is out of the loop.
        # If this can't run, fall through to letting FFmpeg fetch as before.
        if (
            playlist_text is not None
            and "#EXTM3U" in playlist_text
            and not is_master_playlist(playlist_text)
        ):
            status = self._attempt_native(playlist_text, base_url, part)
            if status is not None:
                return status
            log.info("hls job %s: native fetch unavailable, letting FFmpeg fetch", self.job.id)

        for attempt in range(1, self.max_attempts + 1):
            status = self._attempt(part)
            if status is not None:
                return status
            if attempt < self.max_attempts:
                delay = min(2.0 * 2 ** (attempt - 1), 15.0)
                log.info(
                    "hls job %s attempt %d failed (%s) - retrying in %.0fs",
                    self.job.id,
                    attempt,
                    self._failure,
                    delay,
                )
                # Interruptible: a pause/cancel during the wait aborts the retry.
                if self._stop_event.wait(delay):
                    return self._settle_stopped(part)
        return self._finish_failed(self._failure)

    # ------------------------------------------------------------ one attempt

    def _ffmpeg_headers(self) -> str | None:
        """The browser's headers as one CRLF-joined block, FFmpeg's -headers
        format - or None when there aren't any, so the flag is omitted rather
        than sent empty."""
        if not self._headers:
            return None
        return "".join(f"{key}: {value}\r\n" for key, value in self._headers.items())

    def _command(self, part: Path) -> list[str]:
        assert self.ffmpeg_path is not None
        header_block = self._ffmpeg_headers()
        command = [
            self.ffmpeg_path,
            "-y",
            "-nostdin",
            "-loglevel",
            "error",
            "-nostats",
            "-progress",
            "pipe:1",
            # Confine what protocols the (remote, attacker-controlled) manifest
            # may open. Without this a crafted playlist can point a segment at
            # file:// (read the user's disk into the output) or use concat:/
            # subfile:/gopher: - FFmpeg's known local-file-disclosure and SSRF
            # vectors. Modern FFmpeg blocks cross-protocol reads by default, but
            # GrabLine runs whatever ffmpeg is on PATH, so we pin it: only the
            # protocols real HLS/DASH needs. The input is always a remote URL,
            # so file is intentionally absent. Must precede -i to bind to it.
            "-protocol_whitelist",
            _INPUT_PROTOCOLS,
        ]
        if header_block:
            # Per-input option, like -protocol_whitelist: must precede each -i
            # it applies to, or FFmpeg attaches it to the wrong stream.
            command += ["-headers", header_block]
        command += ["-i", self._input_url]
        if self._audio_url:
            command += ["-protocol_whitelist", _INPUT_PROTOCOLS]
            if header_block:
                command += ["-headers", header_block]
            command += ["-i", self._audio_url, "-map", "0", "-map", "1"]
        command += ["-c", "copy", "-f", "mp4", str(part)]
        return command

    def _attempt(self, part: Path) -> JobStatus | None:
        """One FFmpeg run. None means: transient failure, caller may retry."""
        self._downloaded = 0
        self._out_time = 0.0
        env = None
        if self.proxy:
            # FFmpeg reads http(s)_proxy from the environment for http inputs.
            env = {**os.environ, "http_proxy": self.proxy, "https_proxy": self.proxy}
        try:
            process = subprocess.Popen(  # argument list only - no shell (S1)
                self._command(part),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                **proc.hidden(),
            )
        except OSError as exc:
            self._failure = f"could not start FFmpeg: {exc}"
            return self._finish_failed(self._failure)
        assert process.stdout is not None
        reader = threading.Thread(target=self._read_progress, args=(process.stdout,), daemon=True)
        reader.start()

        last_persist = 0.0
        last_estimate = 0
        last_growth = time.monotonic()
        stalled = False
        while process.poll() is None:
            if self._stop_event.is_set():
                self._terminate(process)
                break
            now = time.monotonic()
            if part.exists():
                size = part.stat().st_size
                if size != self._downloaded:
                    self._downloaded = size
                    last_growth = now
                if now - last_persist >= self.persist_interval:
                    last_persist = now
                    self.db.update_job_downloaded(self.job.id, self._downloaded)
                    last_estimate = self._persist_estimate(last_estimate)
            # A live or broken playlist makes FFmpeg poll forever without
            # producing data; never let a job spin indefinitely.
            if now - last_growth > self.stall_timeout:
                self._terminate(process)
                stalled = True
                break
            time.sleep(0.2)

        reader.join(timeout=5)
        stderr_tail = ""
        if process.stderr is not None:
            lines = process.stderr.read().strip().splitlines()
            process.stderr.close()
            if lines:
                stderr_tail = lines[-1]

        if self._stop_event.is_set():
            return self._settle_stopped(part)
        if stalled:
            # A stall at the very end is usually not a failure: the CDN holds
            # a trailing connection open after the last segment, the file stops
            # growing at 99%, and the guard fires. Everything is muxed - and
            # FFmpeg writes the MP4 trailer on the SIGTERM we just sent - so
            # keep the result instead of throwing away a finished download.
            if self._looks_complete(part):
                log.info("hls job %s: stalled after the last segment - keeping", self.job.id)
                return self._finalize(part)
            _discard(part)
            self._failure = (
                f"the stream stalled (no data for {self.stall_timeout:.0f}s). "
                "It may be live or the server may be down"
            )
            return None
        if process.returncode != 0:
            # FFmpeg often exits nonzero on a fully downloaded stream because a
            # trailing segment 404s or the connection drops after the last byte.
            # If we actually muxed the whole thing, keep it instead of failing.
            if self._looks_complete(part):
                log.info("hls job %s: nonzero exit but stream is complete, keeping", self.job.id)
                return self._finalize(part)
            _discard(part)
            detail = f" ({stderr_tail})" if stderr_tail else ""
            self._failure = f"FFmpeg could not process this stream{detail}"
            return None
        return self._finalize(part)

    def _looks_complete(self, part: Path) -> bool:
        """A stream is 'done' when we muxed ~all of the playlist's duration."""
        if not part.exists() or part.stat().st_size <= 0:
            return False
        if not self._duration:
            return False
        return self._out_time >= self._duration * 0.98

    def _read_progress(self, stream: IO[str]) -> None:
        # FFmpeg's out_time_ms is microseconds too (bug-compatible forever).
        for raw in stream:
            line = raw.strip()
            if line.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    value = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                self._out_time = max(self._out_time, value / 1_000_000)
        stream.close()

    def _persist_estimate(self, last_estimate: int) -> int:
        """Total-size estimate from the stream's steady-state bitrate.

        Scaling bytes-so-far by muxed/total duration reads absurdly high at the
        start: FFmpeg writes the container header and reads several segments
        ahead before out_time has moved, so a few MB over a fraction of a second
        extrapolates to hundreds of GB (the '500 GB that became 2 GB' report).
        Instead we anchor a reference point once past that startup, measure the
        bitrate only over what's muxed *after* it, and extrapolate the bytes
        still to come. That is accurate from the first estimate; an EMA irons
        out VBR wobble, it never drops below what's on disk, and it is persisted
        in step with the downloaded bytes so both progress bars agree. The real
        size replaces it at finalize.
        """
        if not self._duration or self._downloaded <= 0 or self._out_time >= self._duration:
            return last_estimate
        # Anchor once the container startup is behind us; measure from there.
        if self._est_ref is None:
            if self._out_time >= _ESTIMATE_MIN_SECONDS:
                self._est_ref = (self._downloaded, self._out_time)
            return last_estimate
        ref_bytes, ref_time = self._est_ref
        span = self._out_time - ref_time
        if span < _ESTIMATE_MIN_SECONDS:  # need a window to read a steady rate
            return last_estimate
        rate = (self._downloaded - ref_bytes) / span  # bytes per muxed second
        estimate = self._downloaded + (self._duration - self._out_time) * rate
        if self._size_ema is None:
            self._size_ema = estimate
        else:
            self._size_ema += (estimate - self._size_ema) * 0.2
        published = max(int(self._size_ema), self._downloaded)
        if published != last_estimate:
            self.db.update_job_total(self.job.id, published)
        return published

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    # -------------------------------------------------------------- playlist

    def _fetch_playlist(self) -> tuple[str, str] | None:
        """The input manifest's text and its final URL after redirects (the base
        for resolving relative segment/key URIs), or None when it can't be
        fetched. A fetch error is swallowed so FFmpeg can report the real one."""
        result = self._fetch_url_text(self._input_url)
        return result

    def _fetch_url_text(self, url: str) -> tuple[str, str] | None:
        try:
            with net.build_client(
                proxy=self.proxy, follow_redirects=True, http2=False, timeout=15
            ) as client:
                response = client.get(url, headers=self._headers or None)
                if response.status_code != 200:
                    return None
                return response.text, str(response.url)
        except httpx.HTTPError:
            return None

    def _detect_live_playlist(self, text: str | None) -> str | None:
        """Refuse live-in-progress HLS clearly instead of recording forever.

        Only a direct media playlist can be judged here; master playlists pass
        through (the stall guard still bounds the worst case).
        """
        if text is None:
            return None
        if "#EXTM3U" not in text or "#EXT-X-STREAM-INF" in text:
            return None
        if "#EXTINF" in text and "#EXT-X-ENDLIST" not in text:
            return (
                "This looks like a live stream that is still in progress. "
                "GrabLine cannot save it yet. Try again once it has ended."
            )
        return None

    # --------------------------------------------------------- native fetch

    def _attempt_native(self, text: str, base_url: str, part: Path) -> JobStatus | None:
        """Fetch the media playlist's parts with the app's HTTP client and remux
        the local copies. Returns a terminal JobStatus on success or a stop;
        None means native could not finish, so the caller lets FFmpeg fetch."""
        self._downloaded = 0
        work = part.parent / f".{part.name}.hls"
        # Keep whatever a previous run left in this work dir so a paused stream
        # resumes instead of refetching gigabytes. The dir is keyed by the part
        # name, so it belongs to this job alone. Only a pause preserves it (set
        # below); every other exit clears it in the finally.
        keep_work = False
        try:
            work.mkdir(parents=True, exist_ok=True)
            _hide_dir(work)  # a big stream's thousands of parts shouldn't clutter the folder
            video = self._prepare_local_manifest(text, base_url, work, "video")
            if video is None:
                if self._stop_event.is_set():
                    keep_work = not self._cancelled
                    return self._settle_stopped(part, keep_progress=not self._cancelled)
                return None
            inputs = [video]
            if self._audio_url:
                audio = self._fetch_url_text(self._audio_url)
                if audio is not None:
                    audio_manifest = self._prepare_local_manifest(audio[0], audio[1], work, "audio")
                    if audio_manifest is None:
                        if self._stop_event.is_set():
                            keep_work = not self._cancelled
                            return self._settle_stopped(part, keep_progress=not self._cancelled)
                        return None
                    inputs.append(audio_manifest)
            if self._stop_event.is_set():
                keep_work = not self._cancelled
                return self._settle_stopped(part, keep_progress=not self._cancelled)
            status = self._remux_local(inputs, part)
            if status is not None:
                return status  # remuxed into the final MP4 - done
            if self._stop_event.is_set():
                keep_work = not self._cancelled
                return self._settle_stopped(part, keep_progress=not self._cancelled)
            # Every segment was fetched, but FFmpeg could not combine them. Do NOT
            # fall through to a full FFmpeg re-download: the gated CDN that needed
            # our native fetch won't serve FFmpeg either, and re-downloading would
            # throw away gigabytes of an already-complete stream (the "finished,
            # then restarted from scratch" bug). Keep the fetched parts so a retry
            # just re-combines them, and fail cleanly.
            keep_work = True
            return self._finish_failed(
                self._failure or "downloaded the stream but could not combine its parts into an MP4"
            )
        except (OSError, httpx.HTTPError) as exc:
            log.info("hls job %s: native fetch failed (%s)", self.job.id, exc)
            if self._headers:
                # We sent the browser's headers and a part was still refused: say
                # so, in case the FFmpeg fallback's error is no clearer.
                self._failure = (
                    "the server refused part of this stream (the page's login or "
                    "referer may have expired - try grabbing it again from the browser)"
                )
            return None
        finally:
            # A successful remux, a cancel, or a fall-through to the FFmpeg
            # fallback makes the local segments useless, so clear them. A pause
            # keeps them for the resume.
            if not keep_work:
                shutil.rmtree(work, ignore_errors=True)

    def _prepare_local_manifest(
        self, text: str, base_url: str, work: Path, prefix: str
    ) -> Path | None:
        """Rewrite a media playlist to point at local files, fetch them, and
        write the local manifest. None if there is nothing to fetch or a stop."""
        rewritten, downloads = self._localize(text, base_url, prefix)
        if not any(name.endswith(".ts") for _, name in downloads):
            return None  # no media segments - not a playlist we can fetch locally
        if not self._fetch_segments(downloads, work):
            return None  # stopped part-way (a failed part raises, caught above)
        manifest = work / f"{prefix}.m3u8"
        manifest.write_text(rewritten, encoding="utf-8")
        return manifest

    def _localize(self, text: str, base_url: str, prefix: str) -> tuple[str, list[tuple[str, str]]]:
        """Rewrite every segment / key / init URI to a local filename, collecting
        the (absolute_url, local_name) pairs to download. Same URL maps to one
        file, so byte-range segments and repeated keys fetch once."""
        local_of: dict[str, str] = {}
        downloads: list[tuple[str, str]] = []

        def local_name(uri: str, suffix: str) -> str:
            absolute = urljoin(base_url, uri.strip())
            if absolute not in local_of:
                name = f"{prefix}-{len(local_of):05d}{suffix}"
                local_of[absolute] = name
                downloads.append((absolute, name))
            return local_of[absolute]

        out: list[str] = []
        for line in text.splitlines():
            if line.startswith(("#EXT-X-KEY", "#EXT-X-SESSION-KEY")):
                line = _TAG_URI.sub(lambda m: f'URI="{local_name(m.group(1), ".key")}"', line)
            elif line.startswith("#EXT-X-MAP"):
                line = _TAG_URI.sub(lambda m: f'URI="{local_name(m.group(1), ".mp4")}"', line)
            elif line.strip() and not line.startswith("#"):
                line = local_name(line, ".ts")
            out.append(line)
        return "\n".join(out) + "\n", downloads

    def _fetch_segments(self, downloads: list[tuple[str, str]], work: Path) -> bool:
        """Download every (url, local_name) into ``work`` with the browser
        headers, in parallel. Raises on a refused part; returns False if the job
        was paused/cancelled part-way, True when all parts are on disk.

        Resumable at segment granularity: a fully fetched segment is renamed
        into place atomically, so a later run counts and skips the ones already
        on disk instead of re-downloading them. A segment interrupted mid-write
        stays a throwaway ``.part`` and is fetched again."""
        total = len(downloads)
        lock = threading.Lock()

        # Resume: trust only segments a previous run renamed into their final
        # name, and refetch everything else. This is what lets pausing a big HLS
        # download keep its progress instead of starting from zero.
        pending: list[tuple[str, str]] = []
        confirmed = 0  # bytes of parts fully fetched and renamed into place
        for seg_url, seg_name in downloads:
            existing = work / seg_name
            if existing.exists():
                confirmed += existing.stat().st_size
            else:
                pending.append((seg_url, seg_name))
        done = total - len(pending)
        inflight = 0  # bytes streamed for parts not yet confirmed (for a smooth bar)
        last_write = 0.0
        self._downloaded = confirmed
        if not pending:
            return not self._stop_event.is_set()

        def fetch_one(client: httpx.Client, url: str, name: str) -> None:
            nonlocal confirmed, inflight, done, last_write
            dest = work / name
            tmp = work / f"{name}.part"
            for attempt in range(_SEGMENT_RETRIES + 1):
                if self._stop_event.is_set():
                    return
                wrote = 0
                try:
                    with client.stream("GET", url, headers=self._headers or None) as response:
                        response.raise_for_status()
                        with open(tmp, "wb") as fh:
                            for chunk in response.iter_bytes(65536):
                                if self._stop_event.is_set():
                                    with lock:
                                        inflight -= wrote  # abandon this part's bytes
                                    return  # leave the .part; it is refetched next run
                                fh.write(chunk)
                                wrote += len(chunk)
                                with lock:
                                    inflight += len(chunk)
                                    self._downloaded = confirmed + inflight
                                    now = time.monotonic()
                                    # Report streamed bytes as they arrive (not only
                                    # on whole-part completion), so the speed graph is
                                    # steady instead of spiking once per segment.
                                    if now - last_write >= 0.5:
                                        last_write = now
                                        self.db.update_job_downloaded(self.job.id, self._downloaded)
                                        if done:
                                            self.db.update_job_total(
                                                self.job.id, int(confirmed / done * total)
                                            )
                    os.replace(tmp, dest)  # atomic: only a complete segment becomes `name`
                    with lock:
                        confirmed += dest.stat().st_size
                        inflight -= wrote  # its bytes are now counted as confirmed
                        done += 1
                        self._downloaded = confirmed + inflight
                    return
                except (httpx.HTTPError, OSError):
                    with lock:
                        inflight -= wrote  # drop this attempt's partial bytes
                    tmp.unlink(missing_ok=True)
                    if attempt >= _SEGMENT_RETRIES or self._stop_event.is_set():
                        raise  # every retry exhausted - fail the fetch (caught above)
                    # Back off briefly, then retry on a fresh connection; the
                    # stalled or dropped one is gone and no longer holds a worker.
                    self._stop_event.wait(min(0.5 * 2**attempt, 4.0))

        with (
            net.build_client(
                proxy=self.proxy,
                follow_redirects=True,
                # HTTP/1.1 so the segment workers are real parallel TCP flows,
                # not multiplexed onto one h2 socket - the latter throttled the
                # native fetch to a crawl. One kept-alive connection per worker.
                http2=False,
                # A short read timeout so a server that accepts the connection and
                # then sends nothing is dropped and retried in seconds, instead of
                # pinning a worker for a whole minute and collapsing parallelism.
                timeout=httpx.Timeout(_SEGMENT_READ_TIMEOUT, connect=15.0, pool=30.0),
                limits=httpx.Limits(
                    max_connections=_SEGMENT_WORKERS + 4,
                    max_keepalive_connections=_SEGMENT_WORKERS + 4,
                ),
            ) as client,
            ThreadPoolExecutor(max_workers=_SEGMENT_WORKERS) as pool,
        ):
            futures = [pool.submit(fetch_one, client, url, name) for url, name in pending]
            for future in futures:
                future.result()  # re-raises a part that failed every retry
        # Final flush so the persisted progress matches what is on disk.
        with lock:
            self.db.update_job_downloaded(self.job.id, confirmed)
        return not self._stop_event.is_set()

    def _remux_local(self, inputs: list[Path], part: Path) -> JobStatus | None:
        """Remux the already-downloaded local manifest(s) into an MP4. No network
        - only file/crypto/data, so FFmpeg just muxes and (for AES-128) decrypts
        with the local key.

        Two tries: a fast lossless stream copy, then, if that fails, copy the
        video but re-encode the audio to AAC and regenerate timestamps. The
        second handles the usual HLS-to-MP4 snags (ADTS-AAC audio and timestamp
        gaps that a plain ``-c copy`` rejects) that would otherwise waste an
        already-downloaded stream. None on failure - the caller keeps the fetched
        parts for a retry rather than re-downloading them.
        """
        assert self.ffmpeg_path is not None
        base = [
            self.ffmpeg_path,
            "-y",
            "-nostdin",
            "-loglevel",
            "error",
            "-nostats",
            "-allowed_extensions",
            "ALL",
            "-protocol_whitelist",
            "file,crypto,data",
        ]
        input_args: list[str] = []
        for manifest in inputs:
            input_args += ["-i", str(manifest)]
        maps: list[str] = []
        if len(inputs) > 1:
            for index in range(len(inputs)):
                maps += ["-map", str(index)]
        attempts = [
            [*base, *input_args, *maps, "-c", "copy", "-f", "mp4", str(part)],
            [
                *base,
                "-fflags",
                "+genpts",
                *input_args,
                *maps,
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-f",
                "mp4",
                str(part),
            ],
        ]
        last_error = ""
        for command in attempts:
            try:
                result = subprocess.run(
                    command, capture_output=True, text=True, timeout=1800, **proc.hidden()
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                log.info("hls job %s: local remux could not run (%s)", self.job.id, exc)
                self._failure = f"could not run FFmpeg to combine the stream ({exc})"
                return None
            if result.returncode == 0 and part.exists() and part.stat().st_size > 0:
                return self._finalize(part)
            tail = (result.stderr or "").strip().splitlines()
            last_error = tail[-1] if tail else ""
            log.info(
                "hls job %s: local remux attempt failed%s",
                self.job.id,
                f" ({last_error})" if last_error else "",
            )
            _discard(part)  # clear the half-written output before the next try
        self._failure = "downloaded the whole stream but FFmpeg could not combine the parts" + (
            f" ({last_error})" if last_error else ""
        )
        return None

    # ------------------------------------------------------------- outcomes

    def _finalize(self, part: Path) -> JobStatus:
        # Never a raw read-only fsync here: on Windows that raises, and the
        # raise used to skip everything below - the downloaded stream sat at
        # "Downloading" forever with its .gl-part never renamed.
        naming.fsync_before_rename(part)
        size = part.stat().st_size
        if size == 0:
            _discard(part)
            return self._finish_failed("the stream produced an empty file")
        dest = naming.unique_path(self.job.dest_path)
        os.replace(part, dest)
        if dest.name != self.job.filename:
            self.job.filename = dest.name
            self.db.update_job_filename(self.job.id, dest.name)
        self.job.total_size = size
        self.db.update_job_total(self.job.id, size)
        self.db.update_job_downloaded(self.job.id, size)
        self.db.set_job_status(self.job.id, JobStatus.COMPLETED)
        return JobStatus.COMPLETED

    def _settle_stopped(self, part: Path, *, keep_progress: bool = False) -> JobStatus:
        # The .gl-part output is always disposable - the remux has not produced
        # a usable MP4 yet. But when the native fetch is paused its downloaded
        # segments are kept (see _attempt_native), so keep the progress count
        # too rather than snapping the bar back to zero.
        _discard(part)
        if not keep_progress:
            self.db.update_job_downloaded(self.job.id, 0)
        if self._cancelled:
            self.db.set_job_status(self.job.id, JobStatus.CANCELLED)
            return JobStatus.CANCELLED
        self.db.set_job_status(self.job.id, JobStatus.PAUSED)
        return JobStatus.PAUSED

    def _finish_failed(self, message: str) -> JobStatus:
        log.warning("hls job %s failed: %s", self.job.id, message)
        self.db.set_job_status(self.job.id, JobStatus.FAILED, error=message)
        return JobStatus.FAILED


def _hide_dir(path: Path) -> None:
    """Best-effort: hide the scratch segment folder on Windows so a big stream's
    thousands of .ts parts don't clutter the download folder while it runs. The
    folder is deleted on completion regardless; a leading '.' already hides it on
    Unix, so this only changes how it looks mid-download on Windows."""
    if sys.platform == "win32":  # pragma: no cover - windows-only
        try:
            import ctypes

            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x02)  # FILE_ATTRIBUTE_HIDDEN
        except Exception:
            pass


def _discard(part: Path) -> None:
    """Delete a leftover .gl-part, retrying briefly: on Windows FFmpeg may
    still hold the handle for a moment after it exits (WinError 32)."""
    for attempt in range(5):
        try:
            part.unlink(missing_ok=True)
            return
        except OSError:
            time.sleep(0.2 * (attempt + 1))
    log.warning("could not remove leftover part file: %s", part)
