"""``LinkService``: this PC's links to other PCs, for the GUI (PROTOCOL.md section 21).

Lives on the GUI thread, next to ``RemoteService`` (which is the other
direction: PCs and phones linked *to* this PC). Each link has a ``LinkRunner``
thread (``app.remote.client.session``) that does the connecting; everything it
learns reaches this object as a queued Qt signal, so all state is touched on
the GUI thread only.

Public API (all call from the GUI thread)
-----------------------------------------
``links()``, ``link(pc_id)``      ``LinkInfo`` of every link (and of links the
                                  host removed, until dismissed)
``is_online(pc_id)``
``host_jobs(pc_id)``              ``PhoneSnapshot`` of the host's downloads
                                  (live: snapshot at login, then events), or
                                  None until the first page arrived
``add_link(address, code)``       pair with the code the other PC shows;
                                  progress and result come as signals
``cancel_add()``
``remove_link(pc_id)``            ``unpair`` when online, key deleted either way
``retry(pc_id)``                  try now instead of waiting out the back-off
``send_control(pc_id, job_id, action)`` -> token
``send_download(pc_id, url, title=None)`` -> token
``send_links(pc_id, urls)`` -> token
``shared_setting()`` / ``set_shared(bool)``  "Let linked PCs see and control
                                  downloads on this PC" (default on)
``shutdown()``

Signals
-------
``linksChanged()``                the list or something shown about a link changed
``linkStateChanged(pc_id)``       a link went connecting/online/offline/...
``hostJobsChanged(pc_id)``        the host's downloads changed
``requestFinished(token, result, message)``
                                  outcome of a send: ``ok``, ``notFound``,
                                  ``conflict``, ``busy``, ``unsupported``,
                                  ``offline``, ``timeout`` or ``failed``
``linkProgress(step)``            ``connecting``, ``sending`` or ``confirm``
``linkFinished(outcome, pc_id)``  ``ok`` or a failure code (see ``pairing``;
                                  also ``invalidCode``, ``busy``, ``failed``)
"""

from __future__ import annotations

import contextlib
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from app import __version__
from app.remote import schema
from app.remote.client import keys, pairing, session
from app.remote.client.local import (
    FAILED,
    FAILED_TEXT,
    NOT_SHARED_TEXT,
    OK,
    REFUSED,
    LocalDownloads,
    MovePlan,
    Outcome,
)
from app.remote.client.session import LinkRecord, LinkRunner
from app.remote.client.transport import parse_address
from app.remote.devices import DeviceStore, clean_device_name
from app.remote.listener import default_pc_name
from app.remote.phonejobs import PhoneJob, PhoneSnapshot

if TYPE_CHECKING:
    from app.core.manager import DownloadManager

log = logging.getLogger(__name__)

KEY_SHARE = "remote_share_downloads"
REQUEST_TIMEOUT_S = 15.0
REPORT_INTERVAL_MS = 1000
MAX_HOST_JOBS = 300
PAGE_SIZE = 100

#: ``linkFinished`` codes the service itself adds to ``pairing``'s.
INVALID_CODE = "invalidCode"
BUSY = "busy"
FAILED_TO_SAVE = "failed"

_CONTROL_ACTIONS = frozenset({"pause", "resume", "cancel", "retry", "remove"})


@dataclass(frozen=True)
class LinkInfo:
    """What the UI shows about one link."""

    pc_id: str
    name: str
    state: str  # connecting, online, offline, identityChanged, removed, hostTooOld, clientTooOld
    address: str | None
    port: int
    paired_at: int
    last_seen_at: int
    features: frozenset[str] = frozenset()
    host_version: str = ""

    @property
    def online(self) -> bool:
        return self.state == session.ONLINE

    @property
    def shares_downloads(self) -> bool:
        """True when the host lists ``phoneJobs``: it can see this PC's downloads."""
        return "phoneJobs" in self.features


@dataclass
class _Reply:
    ok: bool
    type: str = ""
    body: dict[str, Any] = field(default_factory=dict)
    code: str = ""


@dataclass
class _Pending:
    pc_id: str
    done: Callable[[_Reply], None]
    deadline: float


@dataclass
class _Link:
    record: LinkRecord
    runner: LinkRunner | None = None
    token: int = 0
    state: str = session.CONNECTING
    features: frozenset[str] = frozenset()
    host_version: str = ""
    last_seq: int = 0
    jobs: dict[int, PhoneJob] = field(default_factory=dict)
    jobs_received: int = 0
    loaded: bool = False
    load_gen: int = 0
    loading: bool = False
    touched: set[int] = field(default_factory=set)
    snapshot: PhoneSnapshot | None = None
    last_report: list[dict[str, Any]] | None = None
    report_dirty: bool = False
    sent_unshared: bool = False


class _Bridge(QObject):
    """Where the worker threads hand their results to the GUI thread."""

    runner_event = Signal(str, str, object, int)  # pc_id, kind, payload, runner token
    progress = Signal(str)
    paired = Signal(object)


def _int(value: object, low: int = 0) -> int | None:
    return value if type(value) is int and value >= low else None


def host_job(raw: object) -> PhoneJob | None:
    """One job object of the host (section 14) as a ``PhoneJob``, or None when
    it is malformed. Strings are neutralised by ``PhoneJob.from_fields``."""
    if not isinstance(raw, dict):
        return None
    job_id = _int(raw.get("id"), 1)
    downloaded = _int(raw.get("downloaded"))
    created = _int(raw.get("createdAt"))
    name, url, status, kind = raw.get("name"), raw.get("url"), raw.get("status"), raw.get("kind")
    total, eta, speed = raw.get("total"), raw.get("eta"), raw.get("speed")
    error = raw.get("error")
    if (
        job_id is None
        or downloaded is None
        or not isinstance(name, str)
        or not isinstance(url, str)
        or not isinstance(status, str)
        or not isinstance(kind, str)
        or (total is not None and _int(total) is None)
        or (eta is not None and _int(eta) is None)
        or (error is not None and not isinstance(error, str))
    ):
        return None
    shown = {"queued", "downloading", "paused", "completed", "failed", "cancelled"}
    return PhoneJob.from_fields(
        {
            "id": job_id,
            "name": name.strip() or "download",
            "url": url,
            "status": status if status in shown else "unknown",
            "kind": kind,
            "downloaded": downloaded,
            "total": total,
            "speed": _int(speed) or 0,
            "eta": eta,
            "createdAt": created or 0,
            "error": error or None,
        }
    )


class LinkService(QObject):
    linksChanged = Signal()
    linkStateChanged = Signal(str)
    hostJobsChanged = Signal(str)
    requestFinished = Signal(int, str, str)
    linkProgress = Signal(str)
    linkFinished = Signal(str, str)

    def __init__(
        self,
        manager: DownloadManager,
        parent: QObject | None = None,
        *,
        key_dir: Path | None = None,
        device_name: str | None = None,
        backoff: Callable[[int], float] | None = None,
        connect_timeout: float = 2.0,
    ) -> None:
        super().__init__(parent)
        self._manager = manager
        self._db = manager.db
        self._key_dir = key_dir
        self._device_name = clean_device_name(device_name or "") or default_pc_name()
        #: This PC's own pcId (the id its listener sends as ``pcId``), told to
        #: the host at pairing so it can tell which link is this PC (section 21).
        self._own_pc_id = DeviceStore(self._db).pc_id()
        self._backoff = backoff
        self._connect_timeout = connect_timeout
        self._links: dict[str, _Link] = {}
        self._ghosts: dict[str, LinkInfo] = {}
        self._pending: dict[str, _Pending] = {}
        self._next_id = 1
        self._next_token = 1
        self._tokens: dict[str, int] = {}
        self._runner_tokens = 0
        self._shut = False
        self.local = LocalDownloads(manager)
        #: Seconds a request may wait for its reply (tests shorten it).
        self.request_timeout_s = REQUEST_TIMEOUT_S

        self._bridge = _Bridge(self)
        self._bridge.runner_event.connect(self._on_runner_event)
        self._bridge.progress.connect(self.linkProgress)
        self._bridge.paired.connect(self._on_paired)
        self._add_thread: threading.Thread | None = None
        self._add_cancel: threading.Event | None = None

        self._sweep = QTimer(self)
        self._sweep.setInterval(1000)
        self._sweep.timeout.connect(self._expire_requests)
        self._report_timer = QTimer(self)
        self._report_timer.setInterval(REPORT_INTERVAL_MS)
        self._report_timer.timeout.connect(self._report_tick)

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Open every stored link. A link whose key file is gone can never
        sign in again, so it is dropped like a removed pairing."""
        for row in self._db.list_remote_links():
            record = LinkRecord.from_row(row)
            key = keys.load(record.key_file, self._key_dir)
            if key is None:
                self._db.delete_remote_link(record.pc_id)
                keys.delete(record.key_file, self._key_dir)
                continue
            self._open(record, key)
        self.linksChanged.emit()

    def shutdown(self) -> None:
        if self._shut:
            return
        self._shut = True
        self._sweep.stop()
        self._report_timer.stop()
        if self._add_cancel is not None:
            self._add_cancel.set()
        for link in self._links.values():
            if link.runner is not None:
                link.runner.stop()
        for link in self._links.values():
            if link.runner is not None:
                link.runner.join(2.0)
        if self._add_thread is not None:
            self._add_thread.join(2.0)
        self._fail_all("offline")

    def _open(self, record: LinkRecord, key: keys.DeviceKey) -> _Link:
        self._runner_tokens += 1
        token = self._runner_tokens
        pc_id = record.pc_id

        def post(kind: str, payload: Any) -> None:
            if self._shut:
                return
            with contextlib.suppress(RuntimeError):  # the Qt object is gone (app exit)
                self._bridge.runner_event.emit(pc_id, kind, payload, token)

        kwargs: dict[str, Any] = {
            "app_version": __version__,
            "client_pc_id": self._own_pc_id,
            "connect_timeout": self._connect_timeout,
        }
        if self._backoff is not None:
            kwargs["backoff"] = self._backoff
        runner = LinkRunner(record, key, post, **kwargs)
        link = _Link(record=record, runner=runner, token=token)
        self._links[pc_id] = link
        runner.start()
        return link

    # ------------------------------------------------------------ information

    def _info(self, link: _Link) -> LinkInfo:
        record = link.record
        return LinkInfo(
            pc_id=record.pc_id,
            name=record.name,
            state=link.state,
            address=record.last_address,
            port=record.port,
            paired_at=record.paired_at,
            last_seen_at=record.last_seen_at,
            features=link.features,
            host_version=link.host_version,
        )

    def links(self) -> list[LinkInfo]:
        """Every link, oldest first, then the ones the host removed."""
        infos = [self._info(link) for link in self._links.values()]
        infos.sort(key=lambda info: (info.paired_at, info.pc_id))
        return infos + list(self._ghosts.values())

    def link(self, pc_id: str) -> LinkInfo | None:
        link = self._links.get(pc_id)
        if link is not None:
            return self._info(link)
        return self._ghosts.get(pc_id)

    def is_online(self, pc_id: str) -> bool:
        link = self._links.get(pc_id)
        return link is not None and link.state == session.ONLINE

    def host_jobs(self, pc_id: str) -> PhoneSnapshot | None:
        """The host's downloads, newest first. None until the first list
        arrived (and for a link that never connected)."""
        link = self._links.get(pc_id)
        if link is None or not link.loaded:
            return None
        if link.snapshot is None:
            jobs = tuple(sorted(link.jobs.values(), key=lambda j: j.id, reverse=True))
            link.snapshot = PhoneSnapshot(True, jobs, link.jobs_received)
        return link.snapshot

    # --------------------------------------------------------------- settings

    def shared_setting(self) -> bool:
        return self._db.get_setting(KEY_SHARE) != "0"

    def set_shared(self, on: bool) -> None:
        """Turn "Let linked PCs see and control downloads on this PC" on or
        off; every connected host is told at once."""
        self._db.set_setting(KEY_SHARE, "1" if on else "0")
        for link in self._links.values():
            if link.state != session.ONLINE or "phoneJobs" not in link.features:
                continue
            if on:
                link.sent_unshared = False
                self._send_report(link, force=True)
            else:
                self._send_unshared(link)
        self._update_report_timer()

    # ----------------------------------------------------------------- adding

    def add_link(self, address: str, code: str) -> bool:
        """Link to the PC at ``address`` with the code it shows. Returns False
        (and reports through ``linkFinished`` at once) when it cannot start."""
        if self._add_thread is not None and self._add_thread.is_alive():
            self._later(lambda: self.linkFinished.emit(BUSY, ""))
            return False
        if pairing.normalise_code(code) is None:
            self._later(lambda: self.linkFinished.emit(INVALID_CODE, ""))
            return False
        if parse_address(address) is None:
            self._later(lambda: self.linkFinished.emit(pairing.BAD_ADDRESS, ""))
            return False
        cancel = threading.Event()
        self._add_cancel = cancel
        bridge = self._bridge

        def work() -> None:
            result = pairing.pair(
                address,
                code,
                self._device_name,
                app_version=__version__,
                client_pc_id=self._own_pc_id,
                on_progress=lambda step: self._emit_safely(bridge.progress, step),
                cancel=cancel,
            )
            self._emit_safely(bridge.paired, result)

        self._add_thread = threading.Thread(target=work, name="grabline-link-pair", daemon=True)
        self._add_thread.start()
        return True

    def cancel_add(self) -> None:
        if self._add_cancel is not None:
            self._add_cancel.set()

    @staticmethod
    def _emit_safely(signal: Any, value: object) -> None:
        with contextlib.suppress(RuntimeError):
            signal.emit(value)

    def _later(self, call: Callable[[], None]) -> None:
        QTimer.singleShot(0, self, call)

    @Slot(object)
    def _on_paired(self, result: object) -> None:
        if self._shut:
            return
        if not isinstance(result, pairing.Paired):
            code = result.code if isinstance(result, pairing.PairFailed) else FAILED_TO_SAVE
            self.linkFinished.emit(code, "")
            return
        try:
            name = keys.save(result.key, self._key_dir)
        except OSError:
            self.linkFinished.emit(FAILED_TO_SAVE, "")  # nothing was stored
            return
        old = self._links.pop(result.pc_id, None)
        self._ghosts.pop(result.pc_id, None)
        if old is not None:
            self._retire(old)
        now = int(time.time())
        record = LinkRecord(
            pc_id=result.pc_id,
            name=result.pc_name,
            pin=result.pin,
            device_id=result.device_id,
            addresses=(result.address,),
            port=result.port,
            last_address=result.address,
            key_file=name,
            paired_at=now,
            last_seen_at=now,
        )
        try:
            self._db.upsert_remote_link(_row(record))
        except Exception:
            keys.delete(name, self._key_dir)
            self.linkFinished.emit(FAILED_TO_SAVE, "")
            return
        self._open(record, result.key)
        self.linkFinished.emit("ok", result.pc_id)
        self.linksChanged.emit()

    def _retire(self, link: _Link) -> None:
        """Stop a link that is being replaced or removed and delete its key."""
        runner = link.runner
        if runner is not None:
            if link.state == session.ONLINE:
                runner.send({"id": self._request_id(), "type": "unpair"})
                runner.stop(flush=True)
            else:
                runner.stop()
        self._fail_pending(link.record.pc_id, "offline")
        keys.delete(link.record.key_file, self._key_dir)

    # --------------------------------------------------------------- removing

    def remove_link(self, pc_id: str) -> bool:
        """Forget a link: ``unpair`` goes to the host when it is online, and
        the key is deleted whether or not the host answers."""
        link = self._links.pop(pc_id, None)
        ghost = self._ghosts.pop(pc_id, None)
        if link is None and ghost is None:
            return False
        if link is not None:
            self._retire(link)
        self._db.delete_remote_link(pc_id)
        self._update_report_timer()
        self.linksChanged.emit()
        return True

    def retry(self, pc_id: str) -> None:
        """Try again now. Only for a link that is waiting or whose protocol
        version problem may be fixed by now; a changed identity or a removed
        pairing needs a new link."""
        link = self._links.get(pc_id)
        if link is None or link.state in (session.IDENTITY_CHANGED, session.REMOVED):
            return
        if link.runner is not None and link.runner.alive:
            link.runner.wake()
            return
        key = keys.load(link.record.key_file, self._key_dir)
        if key is None:
            return
        self._links.pop(pc_id, None)
        self._open(link.record, key)
        self.linksChanged.emit()

    # ------------------------------------------------------- runner callbacks

    @Slot(str, str, object, int)
    def _on_runner_event(self, pc_id: str, kind: str, payload: object, token: int) -> None:
        link = self._links.get(pc_id)
        if link is None or link.token != token or self._shut:
            return  # an old runner of a link that was replaced or removed
        if kind == "state" and isinstance(payload, str):
            self._on_state(link, payload)
        elif kind == "online" and isinstance(payload, dict):
            self._on_online(link, payload)
        elif kind == "message" and isinstance(payload, dict):
            self._on_message(link, payload)

    def _on_state(self, link: _Link, state: str) -> None:
        pc_id = link.record.pc_id
        if state == session.REMOVED:
            self._links.pop(pc_id, None)
            self._db.delete_remote_link(pc_id)
            keys.delete(link.record.key_file, self._key_dir)
            self._fail_pending(pc_id, "offline")
            self._ghosts[pc_id] = replace(self._info(link), state=session.REMOVED)
            self._update_report_timer()
            self.linksChanged.emit()
            self.linkStateChanged.emit(pc_id)
            return
        was_online = link.state == session.ONLINE
        link.state = state
        if state != session.ONLINE:
            link.last_report = None
            link.sent_unshared = False
            link.last_seq = 0
            self._fail_pending(pc_id, "offline")
            if was_online:
                self._touch(link)
        self._update_report_timer()
        self.linksChanged.emit()
        self.linkStateChanged.emit(pc_id)
        if state != session.ONLINE:
            self.hostJobsChanged.emit(pc_id)  # the page greys a list it cannot refresh

    def _touch(self, link: _Link) -> None:
        now = int(time.time())
        link.record = replace(link.record, last_seen_at=now)
        self._db.update_remote_link(link.record.pc_id, last_seen_at=now)

    def _on_online(self, link: _Link, payload: dict[str, Any]) -> None:
        auth_ok = payload.get("authOk")
        if not isinstance(auth_ok, dict):
            return
        pc_id = link.record.pc_id
        listed = auth_ok.get("features")
        link.features = (
            frozenset(f for f in listed if isinstance(f, str))
            if isinstance(listed, list)
            else frozenset()
        )
        version = auth_ok.get("appVersion")
        link.host_version = version[:32] if isinstance(version, str) else ""
        changes: dict[str, Any] = {"last_seen_at": int(time.time())}
        name = clean_device_name(auth_ok.get("pcName") or "")
        if name:
            changes["name"] = name
        address = payload.get("address")
        if isinstance(address, str) and address:
            changes["last_address"] = address
        addresses = auth_ok.get("addresses")
        if isinstance(addresses, list):
            fresh = tuple(a for a in addresses if isinstance(a, str) and a)[:8]
            if fresh:
                changes["addresses"] = fresh
        port = auth_ok.get("port")
        if type(port) is int and 1024 <= port <= 65535:
            changes["port"] = port
        link.record = replace(link.record, **changes)
        self._db.update_remote_link(pc_id, **changes)
        link.state = session.ONLINE
        link.last_seq = 0
        link.sent_unshared = False
        link.last_report = None
        self._after_login(link)
        self._update_report_timer()
        self.linksChanged.emit()
        self.linkStateChanged.emit(pc_id)

    def _after_login(self, link: _Link) -> None:
        topics = [
            topic
            for topic, feature in (("jobs", "jobs"), ("global", "global"), ("phone", "phoneJobs"))
            if feature in link.features
        ]
        if "events" in link.features and topics:
            self._request(link, {"type": "subscribe", "topics": topics}, lambda _reply: None)
        if "jobs" in link.features:
            self._load_jobs(link)
        if "phoneJobs" in link.features:
            if self.shared_setting():
                self._send_report(link, force=True)
            else:
                self._send_unshared(link)

    # ------------------------------------------------------------ requests

    def _request_id(self) -> str:
        self._next_id += 1
        return f"q{self._next_id}"

    def _request(self, link: _Link, fields: dict[str, Any], done: Callable[[_Reply], None]) -> bool:
        runner = link.runner
        if runner is None or link.state != session.ONLINE:
            return False
        request_id = self._request_id()
        message = {"id": request_id, **fields}
        if not runner.send(message):
            return False
        self._pending[request_id] = _Pending(
            link.record.pc_id, done, time.monotonic() + self.request_timeout_s
        )
        if not self._sweep.isActive():
            self._sweep.start()
        return True

    def _expire_requests(self) -> None:
        now = time.monotonic()
        for request_id in [r for r, p in self._pending.items() if p.deadline <= now]:
            pending = self._pending.pop(request_id, None)
            if pending is not None:
                pending.done(_Reply(False, code="timeout"))
        if not self._pending:
            self._sweep.stop()

    def _fail_pending(self, pc_id: str, code: str) -> None:
        for request_id in [r for r, p in self._pending.items() if p.pc_id == pc_id]:
            pending = self._pending.pop(request_id, None)
            if pending is not None:
                pending.done(_Reply(False, code=code))

    def _fail_all(self, code: str) -> None:
        for pc_id in {p.pc_id for p in self._pending.values()}:
            self._fail_pending(pc_id, code)

    def _on_message(self, link: _Link, message: dict[str, Any]) -> None:
        kind = message.get("type")
        request_id = message.get("id")
        if isinstance(request_id, str):
            pending = self._pending.pop(request_id, None)
            if pending is not None and pending.pc_id == link.record.pc_id:
                if kind == "error":
                    code = message.get("code")
                    pending.done(
                        _Reply(False, "error", message, code if isinstance(code, str) else "")
                    )
                else:
                    pending.done(_Reply(True, str(kind), message))
            return
        if kind == "event":
            self._on_host_event(link, message)

    # ------------------------------------------------------- the host's jobs

    def _load_jobs(self, link: _Link) -> None:
        link.load_gen += 1
        generation = link.load_gen
        link.loading = True
        link.touched = set()
        collected: dict[int, PhoneJob] = {}

        def page(cursor: str | None) -> None:
            fields: dict[str, Any] = {"type": "jobs", "limit": PAGE_SIZE, "filter": "all"}
            if cursor is not None:
                fields["cursor"] = cursor

            def done(reply: _Reply) -> None:
                if link.load_gen != generation or link.record.pc_id not in self._links:
                    return
                if not reply.ok:
                    link.loading = False
                    return
                for raw in reply.body.get("jobs") or []:
                    job = host_job(raw)
                    if job is not None and len(collected) < MAX_HOST_JOBS:
                        collected[job.id] = job
                after = reply.body.get("nextCursor")
                if isinstance(after, str) and after and len(collected) < MAX_HOST_JOBS:
                    page(after)
                    return
                self._finish_load(link, collected)

            if not self._request(link, fields, done):
                link.loading = False

        page(None)

    def _finish_load(self, link: _Link, collected: dict[int, PhoneJob]) -> None:
        merged = {job_id: job for job_id, job in collected.items() if job_id not in link.touched}
        for job_id in link.touched:  # what an event said meanwhile is newer
            if job_id in link.jobs:
                merged[job_id] = link.jobs[job_id]
        link.jobs = merged
        link.loading = False
        link.touched = set()
        link.loaded = True
        link.jobs_received = int(time.time())
        link.snapshot = None
        self.hostJobsChanged.emit(link.record.pc_id)

    def _changed_jobs(self, link: _Link) -> None:
        link.snapshot = None
        link.jobs_received = int(time.time())
        self.hostJobsChanged.emit(link.record.pc_id)

    def _on_host_event(self, link: _Link, message: dict[str, Any]) -> None:
        seq = message.get("seq")
        if type(seq) is int:
            gap = link.last_seq != 0 and seq != link.last_seq + 1
            link.last_seq = seq
            if gap and "jobs" in link.features:
                self._load_jobs(link)  # events were skipped: start from a fresh list
        name = message.get("event")
        if name == "phone.command":
            self._on_command(link, message)
            return
        if name in ("job.added", "job.updated", "job.finished", "job.failed"):
            job = host_job(message.get("job"))
            if job is None:
                return
            link.jobs[job.id] = job
            if link.loading:
                link.touched.add(job.id)
            if link.loaded or link.loading:
                self._changed_jobs(link)
        elif name == "job.progress":
            self._apply_progress(link, message)
        elif name == "job.removed":
            job_id = _int(message.get("jobId"), 1)
            if job_id is not None:
                link.jobs.pop(job_id, None)
                if link.loading:
                    link.touched.add(job_id)
                self._changed_jobs(link)

    def _apply_progress(self, link: _Link, message: dict[str, Any]) -> None:
        job_id = _int(message.get("jobId"), 1)
        old = link.jobs.get(job_id) if job_id is not None else None
        downloaded = _int(message.get("downloaded"))
        status = message.get("status")
        if old is None or downloaded is None or not isinstance(status, str):
            return
        total = message.get("total")
        eta = message.get("eta")
        link.jobs[old.id] = PhoneJob(
            id=old.id,
            name=old.name,
            url=old.url,
            status=status if status in {"queued", "downloading", "paused"} else old.status,
            kind=old.kind,
            downloaded=downloaded,
            total=total if _int(total) is not None else old.total,
            speed=_int(message.get("speed")) or 0,
            eta=eta if _int(eta) is not None else None,
            created_at=old.created_at,
            error=old.error,
        )
        if link.loading:
            link.touched.add(old.id)
        self._changed_jobs(link)

    # ------------------------------------------------------------- sending

    def _token(self) -> int:
        token = self._next_token
        self._next_token += 1
        return token

    def _send(
        self,
        pc_id: str,
        fields: dict[str, Any],
        *,
        feature: str | None,
        interpret: Callable[[_Reply], tuple[str, str]],
    ) -> int:
        """The shared body of ``send_control``, ``send_download`` and
        ``send_links``: returns a token, and ``requestFinished`` follows."""
        link = self._links.get(pc_id)
        if link is None:
            return 0
        token = self._token()

        def finish(result: str, message: str = "") -> None:
            self.requestFinished.emit(token, result, message)

        if link.state != session.ONLINE:
            self._later(lambda: finish("offline"))
            return token
        if feature is not None and feature not in link.features:
            self._later(lambda: finish("unsupported"))
            return token

        def done(reply: _Reply) -> None:
            result, message = interpret(reply)
            finish(result, message)

        if not self._request(link, fields, done):
            self._later(lambda: finish("offline"))
        return token

    @staticmethod
    def _outcome(reply: _Reply) -> tuple[str, str]:
        if reply.ok:
            return "ok", ""
        mapped = {
            "notFound": "notFound",
            "conflict": "conflict",
            "rateLimited": "busy",
            "unsupported": "unsupported",
            "timeout": "timeout",
            "offline": "offline",
            "forbidden": "failed",
        }
        return mapped.get(reply.code, "failed"), ""

    def send_control(self, pc_id: str, job_id: int, action: str) -> int:
        """Pause, resume, cancel, retry or remove one of the host's downloads.
        Returns a token (0 if ``action`` or ``pc_id`` is not valid, nothing
        sent); ``requestFinished`` answers every other call, also at once for
        ``offline``."""
        if action not in _CONTROL_ACTIONS or type(job_id) is not int or job_id < 1:
            return 0
        return self._send(
            pc_id,
            {"type": "control", "jobId": job_id, "action": action},
            feature="control",
            interpret=self._outcome,
        )

    def send_download(self, pc_id: str, url: str, title: str | None = None) -> int:
        """Ask the host to download ``url`` (unattended, quality ``best``).
        0 if the link is not one the protocol carries."""
        clean = schema.valid_url(url)
        if clean is None:
            return 0
        fields: dict[str, Any] = {
            "type": "download",
            "url": clean,
            "requestId": secrets.token_urlsafe(16),
            "quality": "best",
        }
        if title:
            fields["title"] = title[:512]

        def interpret(reply: _Reply) -> tuple[str, str]:
            if reply.ok:
                return "ok", "duplicate" if reply.body.get("duplicate") is True else ""
            return self._outcome(reply)

        return self._send(pc_id, fields, feature=None, interpret=interpret)

    def send_links(self, pc_id: str, urls: list[str]) -> int:
        """Ask the host to download several links. 0 if none is valid."""
        clean = [u for u in (schema.valid_url(url) for url in urls) if u is not None]
        if not clean or len(clean) > 300:
            return 0
        fields: dict[str, Any] = {
            "type": "links",
            "urls": clean,
            "requestId": secrets.token_urlsafe(16),
            "quality": "best",
        }
        return self._send(pc_id, fields, feature=None, interpret=self._outcome)

    # ------------------------------------------------ this PC's own downloads

    def _update_report_timer(self) -> None:
        needed = self.shared_setting() and any(
            link.state == session.ONLINE and "phoneJobs" in link.features
            for link in self._links.values()
        )
        if needed and not self._report_timer.isActive():
            self._report_timer.start()
        elif not needed and self._report_timer.isActive():
            self._report_timer.stop()

    def _send_unshared(self, link: _Link) -> None:
        if link.sent_unshared:
            return
        link.sent_unshared = True
        link.last_report = None
        self._request(link, {"type": "phoneJobs", "shared": False, "jobs": []}, lambda _r: None)

    def _send_report(self, link: _Link, *, force: bool = False) -> None:
        try:
            jobs = self.local.report()
        except Exception:
            log.exception("could not build the downloads report")
            return
        self._deliver_report(link, jobs, force)

    def _deliver_report(self, link: _Link, jobs: list[dict[str, Any]], force: bool) -> None:
        if not force and not link.report_dirty and jobs == link.last_report:
            return
        link.report_dirty = False
        link.last_report = jobs
        self._request(link, {"type": "phoneJobs", "shared": True, "jobs": jobs}, lambda _r: None)

    def _report_tick(self) -> None:
        links = [
            link
            for link in self._links.values()
            if link.state == session.ONLINE and "phoneJobs" in link.features
        ]
        if not links or not self.shared_setting():
            return
        try:
            jobs = self.local.report()
        except Exception:
            log.exception("could not build the downloads report")
            return
        for link in links:
            self._deliver_report(link, jobs, False)

    # ------------------------------------------------- commands from the host

    def _on_command(self, link: _Link, message: dict[str, Any]) -> None:
        """Section 20 ``phone.command`` from the host: run it on this PC's own
        downloads and answer with ``phoneCommandResult``."""
        command_id = message.get("commandId")
        action = message.get("action")
        job_id = message.get("jobId")
        if (
            "phoneJobs" not in link.features
            or type(command_id) is not int
            or not 1 <= command_id <= schema.MAX_COMMAND_ID
            or not isinstance(action, str)
            or type(job_id) is not int
        ):
            return
        if not self.shared_setting():
            self._answer(link, command_id, Outcome(REFUSED, NOT_SHARED_TEXT))
            return
        try:
            outcome = self.local.run(action, job_id)
        except Exception:
            log.exception("a command from a linked PC failed")
            outcome = Outcome(FAILED, FAILED_TEXT)
        if isinstance(outcome, MovePlan):
            self._move(link, command_id, outcome)
            return
        self._answer(link, command_id, outcome)

    def _move(self, link: _Link, command_id: int, plan: MovePlan) -> None:
        """``moveToPc``: ``download`` to the host, and only after ``queued``
        stop the local download."""
        fields: dict[str, Any] = {
            "type": "download",
            "url": plan.url,
            "requestId": secrets.token_urlsafe(16),
            "quality": "best",
        }
        if plan.title:
            fields["title"] = plan.title

        def done(reply: _Reply) -> None:
            if reply.ok:
                try:
                    self.local.finish_move(plan.job_id)
                except Exception:
                    log.exception("could not stop a moved download")
                self._answer(link, command_id, Outcome(OK, None, changed=True))
            else:
                self._answer(link, command_id, Outcome(FAILED, FAILED_TEXT))

        if not self._request(link, fields, done):
            self._answer(link, command_id, Outcome(FAILED, FAILED_TEXT))

    def _answer(self, link: _Link, command_id: int, outcome: Outcome) -> None:
        self._request(
            link,
            {
                "type": "phoneCommandResult",
                "commandId": command_id,
                "result": outcome.result,
                "message": outcome.message,
            },
            lambda _reply: None,
        )
        if outcome.changed:
            link.report_dirty = True  # the next tick (within a second) reports it


def _row(record: LinkRecord) -> dict[str, Any]:
    return {
        "pc_id": record.pc_id,
        "name": record.name,
        "pin": record.pin,
        "device_id": record.device_id,
        "addresses": list(record.addresses),
        "port": record.port,
        "last_address": record.last_address,
        "key_file": record.key_file,
        "paired_at": record.paired_at,
        "last_seen_at": record.last_seen_at,
    }


__all__ = ["KEY_SHARE", "LinkInfo", "LinkService", "host_job"]
