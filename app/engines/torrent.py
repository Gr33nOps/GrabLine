"""The torrent engine, built on libtorrent (the library behind qBittorrent).

One shared session carries DHT, Peer Exchange, UPnP/NAT-PMP port mapping and
the rate limits; each GrabLine job is one torrent handle in it. A job is
COMPLETED when the data is on disk - seeding then continues in the background
until the seed-ratio limit (or immediately stops when seeding is off), so the
queue behaves like a download manager while the session behaves like a
torrent client.

Sources accepted: a magnet link, a local .torrent file, or an http(s) URL to
a .torrent (fetched with httpx first - clicking a .torrent link on a website
opens it here instead of saving a file).
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx

from app.core.errors import DownloadError
from app.core.models import Job, JobStatus
from app.core.settings import Settings
from app.db.database import Database

log = logging.getLogger(__name__)

_POLL_SECONDS = 0.5
_PERSIST_SECONDS = 0.3
_RATIO_TICK_SECONDS = 5.0
_DHT_ROUTERS = "router.bittorrent.com:6881,router.utorrent.com:6881,dht.transmissionbt.com:6881"


def _lt() -> Any:
    """Import libtorrent lazily so the app starts even if the wheel is
    missing; the first torrent action then raises a friendly error."""
    try:
        import libtorrent
    except ImportError as exc:  # pragma: no cover - packaging problem
        raise DownloadError(
            "torrent support needs the libtorrent package (pip install libtorrent)"
        ) from exc
    return libtorrent


@dataclass(frozen=True)
class TorrentFileEntry:
    index: int  # the libtorrent file index - priorities align to this
    path: str
    size: int


@dataclass(frozen=True)
class TorrentMeta:
    name: str
    total_size: int
    files: tuple[TorrentFileEntry, ...]
    comment: str = ""
    trackers: tuple[str, ...] = ()
    num_raw_files: int = 0  # including hidden pad files - priority list length

    def priorities_for(self, skipped: set[int]) -> list[int]:
        """A full libtorrent priority list: normal (4) everywhere, 0 for the
        real-file indices in ``skipped``."""
        priorities = [4] * max(self.num_raw_files, len(self.files))
        for index in skipped:
            if 0 <= index < len(priorities):
                priorities[index] = 0
        return priorities


def is_torrent_source(text: str) -> bool:
    """Does this string belong to the torrent engine? (magnet URI, .torrent
    path or URL)."""
    stripped = text.strip()
    if stripped.lower().startswith("magnet:"):
        return "xt=" in stripped
    return stripped.lower().split("?")[0].endswith(".torrent")


def magnet_display_name(magnet: str) -> str | None:
    """The dn= parameter of a magnet link, if present."""
    from urllib.parse import parse_qs, unquote, urlsplit

    query = parse_qs(urlsplit(magnet).query)
    names = query.get("dn")
    return unquote(names[0]) if names else None


def fetch_torrent_bytes(source: str, proxy: str | None = None) -> bytes:
    """The raw .torrent contents for a local path or http(s) URL."""
    if source.lower().startswith(("http://", "https://")):
        try:
            response = httpx.get(source, follow_redirects=True, timeout=30, proxy=proxy or None)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise DownloadError(f"could not fetch the .torrent file ({exc})") from exc
        return response.content
    path = Path(source)
    if not path.is_file():
        raise DownloadError(f"torrent file not found: {source}")
    return path.read_bytes()


def parse_torrent(data: bytes) -> TorrentMeta:
    """Name, files and sizes from raw .torrent bytes (the add dialog's
    preview and the file-priority list)."""
    lt = _lt()
    try:
        info = lt.torrent_info(lt.bdecode(data))
    except (RuntimeError, ValueError) as exc:
        raise DownloadError(f"not a valid torrent file ({exc})") from exc
    storage = info.files()
    # Hybrid v1/v2 torrents carry hidden .pad alignment files - skip them but
    # keep the real libtorrent index so priorities still line up.
    files = tuple(
        TorrentFileEntry(i, storage.file_path(i), storage.file_size(i))
        for i in range(storage.num_files())
        if not storage.file_flags(i) & lt.file_storage.flag_pad_file
    )
    return TorrentMeta(
        name=str(info.name()),
        total_size=sum(entry.size for entry in files),
        files=files,
        comment=str(info.comment() or ""),
        trackers=tuple(t.url for t in info.trackers()),
        num_raw_files=int(storage.num_files()),
    )


def magnet_from_torrent(data: bytes) -> str:
    """The magnet link equivalent of a .torrent (Copy magnet link)."""
    lt = _lt()
    try:
        info = lt.torrent_info(lt.bdecode(data))
    except (RuntimeError, ValueError) as exc:
        raise DownloadError(f"not a valid torrent file ({exc})") from exc
    return str(lt.make_magnet_uri(info))


def create_torrent_file(
    source: Path,
    *,
    trackers: tuple[str, ...] = (),
    web_seeds: tuple[str, ...] = (),
    comment: str = "",
    private: bool = False,
) -> bytes:
    """Build a .torrent for a file or folder (torrent creation)."""
    lt = _lt()
    if not source.exists():
        raise DownloadError(f"nothing to share at {source}")
    storage = lt.file_storage()
    lt.add_files(storage, str(source))
    if storage.num_files() == 0:
        raise DownloadError("the folder is empty, nothing to share")
    creator = lt.create_torrent(storage)
    creator.set_creator("GrabLine")
    if comment:
        creator.set_comment(comment)
    creator.set_priv(private)
    for tier, tracker in enumerate(trackers):
        creator.add_tracker(tracker, tier)
    for seed in web_seeds:
        creator.add_url_seed(seed)
    lt.set_piece_hashes(creator, str(source.parent))
    return bytes(lt.bencode(creator.generate()))


#: libtorrent proxy_type enum values (from the settings_pack docs).
_PROXY_TYPES = {"socks4": 1, "socks5": 2, "http": 3}


def _proxy_settings(proxy: str | None) -> dict[str, Any]:
    """Translate GrabLine's proxy URL into libtorrent session settings, so
    torrent peer/tracker traffic goes through the same proxy as everything
    else. SOCKS5/SOCKS4/HTTP are supported (with auth)."""
    if not proxy:
        return {}
    from urllib.parse import urlsplit

    parts = urlsplit(proxy)
    scheme = parts.scheme.lower().rstrip("h")  # socks5h -> socks5
    kind = _PROXY_TYPES.get("socks5" if scheme == "socks4a" else scheme)
    if kind is None or not parts.hostname:
        return {}
    pack: dict[str, Any] = {
        "proxy_type": kind,
        "proxy_hostname": parts.hostname,
        "proxy_port": parts.port or (1080 if scheme.startswith("socks") else 8080),
        "proxy_peer_connections": True,
        "proxy_tracker_connections": True,
    }
    if parts.username:
        pack["proxy_username"] = parts.username
        pack["proxy_password"] = parts.password or ""
    return pack


@dataclass(frozen=True)
class TorrentStats:
    """A live swarm snapshot for one torrent, for the detail panel's Peers tab.
    ``swarm_seeds``/``swarm_peers`` are -1 until a tracker has reported them."""

    seeds: int  # seeds we're connected to
    peers: int  # leechers we're connected to
    swarm_seeds: int  # seeds in the whole swarm (tracker scrape)
    swarm_peers: int
    down_rate: float
    up_rate: float
    downloaded: int  # bytes we have (libtorrent total_done / all-time)
    uploaded: int
    ratio: float
    seeding: bool


class TorrentSession:
    """The one libtorrent session shared by every torrent job."""

    def __init__(self) -> None:
        self._session: Any = None
        self._lock = threading.Lock()
        self._settings: Settings | None = None
        self._ticker: threading.Thread | None = None

    def configure(self, settings: Settings) -> None:
        """Apply (or re-apply, live) the GrabLine settings to the session."""
        with self._lock:
            self._settings = settings
            if self._session is not None:
                self._session.apply_settings(self._pack(settings))

    def _pack(self, settings: Settings) -> dict[str, Any]:
        port = settings.torrent_port
        pack = {
            "listen_interfaces": f"0.0.0.0:{port},[::]:{port}",
            "enable_dht": settings.torrent_dht,
            "enable_lsd": True,  # local peer discovery
            "enable_upnp": settings.torrent_upnp,
            "enable_natpmp": settings.torrent_natpmp,
            # When fair-speed is on, per-torrent handle limits own the split so
            # a session-wide cap would fight the equal-share rebalance.
            "download_rate_limit": (0 if settings.fair_speed else settings.speed_limit_kbps * 1024),
            "upload_rate_limit": settings.torrent_upload_kbps * 1024,
            "dht_bootstrap_nodes": _DHT_ROUTERS,
            "user_agent": "GrabLine",
        }
        # Peer encryption (Settings -> Torrent): prefer = enabled either way,
        # require = encrypted peers only, off = plaintext only.
        enc = {"prefer": (1, 1), "require": (0, 0), "off": (2, 2)}[settings.torrent_encryption]
        pack["out_enc_policy"], pack["in_enc_policy"] = enc
        pack.update(_proxy_settings(settings.proxy))
        return pack

    def session(self) -> Any:
        with self._lock:
            if self._session is None:
                lt = _lt()
                settings = self._settings
                if settings is None:
                    raise DownloadError("torrent session used before configure()")
                self._session = lt.session(self._pack(settings))
                self._ticker = threading.Thread(
                    target=self._tick, name="gl-torrent-ratio", daemon=True
                )
                self._ticker.start()
            return self._session

    def add(self, params: Any) -> Any:
        lt = _lt()
        # Re-adding the same torrent (a resume) must return the live handle,
        # not raise - clear the duplicate-is-error flag.
        params.flags &= ~lt.torrent_flags.duplicate_is_error
        return self.session().add_torrent(params)

    def remove(self, handle: Any, *, delete_files: bool = False) -> None:
        lt = _lt()
        with self._lock:
            if self._session is None:
                return
            if delete_files:
                self._session.remove_torrent(handle, lt.session.delete_files)
            else:
                self._session.remove_torrent(handle)

    def upload_rate(self) -> float:
        """Total bytes/sec this process is currently uploading to peers (the
        dashboard's upload graph); 0 when no session or nothing seeding."""
        with self._lock:
            session = self._session
        if session is None:
            return 0.0
        try:
            return float(sum(h.status().upload_rate for h in session.get_torrents()))
        except Exception:  # libtorrent's binding exceptions are opaque; a bad
            # handle read must not break the dashboard, but leave a trace.
            log.debug("upload-rate read failed", exc_info=True)
            return 0.0

    def stats_for(self, info_hash_hex: str) -> TorrentStats | None:
        """Live swarm stats for the torrent with this info-hash, or None if it
        isn't in the session (not downloading and not seeding). Matched by
        iterating the handles, so it works long after the download task ended."""
        target = info_hash_hex.strip().lower()
        if not target:
            return None
        with self._lock:
            session = self._session
        if session is None:
            return None
        try:
            for handle in session.get_torrents():
                status = handle.status()
                if str(status.info_hash).lower() != target:
                    continue
                uploaded = int(status.all_time_upload)
                done = int(status.total_done)
                # After pause-on-complete, libtorrent 2.x sometimes reports
                # all_time_download as 0 even though total_done / payload are
                # correct. Prefer the larger of the reliable counters.
                downloaded = max(
                    int(status.all_time_download),
                    int(status.total_payload_download),
                    done,
                )
                return TorrentStats(
                    seeds=int(status.num_seeds),
                    peers=int(status.num_peers),
                    swarm_seeds=int(status.num_complete),
                    swarm_peers=int(status.num_incomplete),
                    down_rate=float(status.download_rate),
                    up_rate=float(status.upload_rate),
                    downloaded=downloaded,
                    uploaded=uploaded,
                    ratio=uploaded / max(done, 1),  # same definition the ratio limit uses
                    seeding=bool(status.is_seeding),
                )
        except Exception:  # libtorrent's exceptions are opaque; a bad read must
            # never break the detail panel, but leave a trace.
            log.debug("torrent stats read failed", exc_info=True)
        return None

    def detach(self) -> None:
        """Drop the Settings pointer so the ratio ticker cannot touch a DB
        that tests (or a shutting-down app) have already closed. The libtorrent
        session itself stays up for process lifetime."""
        with self._lock:
            self._settings = None

    def _tick(self) -> None:
        """Seed-ratio enforcement: pause any seeding torrent whose ratio
        passed the limit (0 = seed forever)."""
        while True:
            time.sleep(_RATIO_TICK_SECONDS)
            # Settings reads + libtorrent reads share one try: the Settings
            # object may outlive a closed SQLite connection (test teardown /
            # app exit), and libtorrent's binding exceptions are opaque.
            try:
                settings = self._settings
                session = self._session
                if session is None or settings is None:
                    continue
                limit = settings.torrent_ratio_limit
                minutes = settings.torrent_seed_minutes
                if not settings.torrent_seed or (limit <= 0 and minutes <= 0):
                    continue
                for handle in session.get_torrents():
                    status = handle.status()
                    if not status.is_seeding or status.paused:
                        continue
                    done = max(int(status.total_done), 1)
                    if limit > 0 and int(status.all_time_upload) / done >= limit:
                        handle.pause()
                        continue
                    # Seeding-time limit (Settings -> Torrent): stop after N minutes.
                    if minutes > 0 and int(status.seeding_duration) >= minutes * 60:
                        handle.pause()
            except Exception:
                log.debug("seed-ratio tick failed", exc_info=True)


#: Module-level singleton - DHT, port mappings and peers live process-wide.
SESSION = TorrentSession()


@dataclass
class _State:
    downloaded: int = 0
    total: int | None = None


class TorrentDownload:
    """Runs one torrent job. One-shot object, like the other engine tasks.

    Job options (job.options):
        sequential: bool           stream-friendly in-order pieces
        first_last: bool           fetch first+last pieces of each file early
        file_priorities: [int]     0=skip, 1..7 per file index
        peers: ["ip:port"]         extra peers to try (also the test hook)
    """

    def __init__(self, db: Database, job: Job, *, settings: Settings) -> None:
        self.db = db
        self.job = job
        self.settings = settings
        self._pause_event = threading.Event()
        self._cancel_event = threading.Event()
        self._state = _State()
        self._named = False
        self._handle: Any = None
        self._download_limit = 0  # bytes/sec; 0 = unlimited

    # ------------------------------------------------------------- control

    def pause(self) -> None:
        self._pause_event.set()

    def cancel(self) -> None:
        self._cancel_event.set()

    def set_download_limit(self, bytes_per_sec: int) -> None:
        """Live fair-speed / global cap for this torrent (0 = unlimited)."""
        self._download_limit = max(0, int(bytes_per_sec))
        handle = self._handle
        if handle is not None:
            with contextlib.suppress(Exception):
                handle.set_download_limit(self._download_limit)

    @property
    def bytes_downloaded(self) -> int:
        return self._state.downloaded

    # ----------------------------------------------------------------- run

    def run(self) -> JobStatus:
        lt = _lt()
        self.db.set_job_status(self.job.id, JobStatus.DOWNLOADING)
        SESSION.configure(self.settings)
        try:
            params = self._build_params(lt)
        except DownloadError as exc:
            self.db.set_job_status(self.job.id, JobStatus.FAILED, error=str(exc))
            return JobStatus.FAILED
        handle = SESSION.add(params)
        self._handle = handle
        handle.resume()  # a re-added (resumed) torrent may still be paused
        if self._download_limit:
            with contextlib.suppress(Exception):
                handle.set_download_limit(self._download_limit)
        self._persist_info_hash(handle)
        self._apply_options(lt, handle)
        for peer in self.job.options.get("peers") or ():
            host, _, port = str(peer).rpartition(":")
            if host and port.isdigit():
                handle.connect_peer((host, int(port)))
        return self._poll(handle)

    def _build_params(self, lt: Any) -> Any:
        source = self.job.url
        if source.lower().startswith("magnet:"):
            try:
                params = lt.parse_magnet_uri(source)
            except RuntimeError as exc:
                raise DownloadError(f"not a valid magnet link ({exc})") from exc
        else:
            params = lt.add_torrent_params()
            params.ti = lt.torrent_info(lt.bdecode(fetch_torrent_bytes(source)))
        params.save_path = self.job.dest_dir
        return params

    def _persist_info_hash(self, handle: Any) -> None:
        """Record the torrent's info-hash on the job so the detail panel can find
        this handle's live stats later - including while it seeds, after this
        task has returned."""
        try:
            info_hash = str(handle.status().info_hash)
        except Exception:  # opaque libtorrent read; not worth failing the job
            return
        if not info_hash or set(info_hash) == {"0"}:  # unknown (v2-only magnet)
            return
        if self.job.options.get("info_hash") == info_hash:
            return
        merged = {**self.job.options, "info_hash": info_hash}
        self.db.update_job_options(self.job.id, merged)
        self.job = replace(self.job, options=merged)

    def _apply_options(self, lt: Any, handle: Any) -> None:
        options = self.job.options
        if options.get("sequential"):
            handle.set_flags(lt.torrent_flags.sequential_download)
        priorities = options.get("file_priorities")
        if priorities and handle.status().has_metadata:
            handle.prioritize_files([int(p) for p in priorities])

    def _first_last_pieces(self, handle: Any) -> None:
        """Bump the first and last pieces so previews/streaming start fast."""
        info = handle.torrent_file()
        if info is None:
            return
        last = info.num_pieces() - 1
        for piece in {0, last} | {min(1, last), max(last - 1, 0)}:
            handle.piece_priority(piece, 7)

    def _poll(self, handle: Any) -> JobStatus:
        lt = _lt()
        last_persist = 0.0
        metadata_seen = False
        while True:
            if self._cancel_event.is_set():
                SESSION.remove(handle, delete_files=True)
                self.db.update_job_downloaded(self.job.id, 0)
                self.db.set_job_status(self.job.id, JobStatus.CANCELLED)
                return JobStatus.CANCELLED
            if self._pause_event.is_set():
                handle.pause()
                self.db.update_job_downloaded(self.job.id, self._state.downloaded)
                self.db.set_job_status(self.job.id, JobStatus.PAUSED)
                return JobStatus.PAUSED

            status = handle.status()
            if status.errc.value() != 0:
                message = str(status.errc.message())
                self.db.set_job_status(self.job.id, JobStatus.FAILED, error=message)
                return JobStatus.FAILED

            if status.has_metadata and not metadata_seen:
                metadata_seen = True
                self._on_metadata(lt, handle)

            self._state.downloaded = int(status.total_done)
            total = int(status.total_wanted)
            if total > 0:
                self._state.total = total
            now = time.monotonic()
            if now - last_persist >= _PERSIST_SECONDS:
                last_persist = now
                self.db.update_job_downloaded(self.job.id, self._state.downloaded)
                if self._state.total:
                    self.db.update_job_total(self.job.id, self._state.total)

            if status.has_metadata and status.is_finished:
                return self._finish(handle)
            time.sleep(_POLL_SECONDS)

    def _on_metadata(self, lt: Any, handle: Any) -> None:
        """Metadata arrived (instant for .torrent, later for magnets): name
        the job properly and apply the file/piece options that need it."""
        info = handle.torrent_file()
        if info is not None and not self._named:
            self._named = True
            self.db.update_job_filename(self.job.id, str(info.name()))
            self.db.update_job_total(self.job.id, int(info.total_size()))
        priorities = self.job.options.get("file_priorities")
        if priorities:
            handle.prioritize_files([int(p) for p in priorities])
        if self.job.options.get("first_last"):
            self._first_last_pieces(handle)

    def _finish(self, handle: Any) -> JobStatus:
        self.db.update_job_downloaded(self.job.id, self._state.downloaded)
        if self._state.total:
            self.db.update_job_total(self.job.id, self._state.total)
        if not self.settings.torrent_seed:
            handle.pause()  # data done, no seeding wanted
        self.db.set_job_status(self.job.id, JobStatus.COMPLETED)
        return JobStatus.COMPLETED
