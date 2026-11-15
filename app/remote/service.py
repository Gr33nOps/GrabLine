"""Glue between the phone listener (its own thread) and the app (GUI thread).

``RemoteService`` lives on the GUI thread. It owns the ``RemoteListener``,
answers each validated request through ``RemoteHandlers`` (Qt queues the
listener's ``requestReady`` signal onto this thread), polls the manager's job
snapshot for live events only while a phone is subscribed, and serves the
main window's unattended add path (folder ids, add outcomes).

Settings (all in the app's settings table, none shown except the switch):
``remote_listener_on`` ("1"/"0", default on: the listener still opens no
socket until a phone is paired or a pairing code is showing),
``remote_port`` (default 47821), ``remote_loopback_test`` (default off; also
binds 127.0.0.1 and lists 10.0.2.2 for the Android emulator).
"""

from __future__ import annotations

import atexit
import logging
import time
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal, Slot

from app.remote import folders
from app.remote.devices import DeviceStore
from app.remote.events import EventTracker
from app.remote.handlers import RemoteHandlers, analysis_reply
from app.remote.listener import DEFAULT_PORT, RemoteListener, default_pc_name
from app.remote.requests_log import RequestLog
from app.remote.schema import Request, RequestError

if TYPE_CHECKING:
    from app.core.manager import DownloadManager
    from app.core.resolver import Resolver
    from app.core.settings import Settings
    from app.ui.remote_add import AddOutcome

log = logging.getLogger(__name__)

POLL_MS = 500
ANALYZE_TIMEOUT_MS = 30_000

KEY_ON = "remote_listener_on"
KEY_PORT = "remote_port"
KEY_LOOPBACK = "remote_loopback_test"


class RemoteService(QObject):
    """Created by the main window; ``shutdown()`` at app exit."""

    #: Re-emitted on the GUI thread for the Settings "Phone" page.
    pairingStarted = Signal(str, str, int)
    pairingPending = Signal(str)
    pairingLockedWarning = Signal()
    pairingFinished = Signal(str)
    devicesChanged = Signal()
    stateChanged = Signal(bool)
    errorOccurred = Signal(str)

    def __init__(
        self,
        manager: DownloadManager,
        settings: Settings,
        resolver: Resolver,
        parent: QObject | None = None,
        *,
        identity_dir: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self._manager = manager
        self._db = manager.db
        self._settings = settings
        self._resolver = resolver
        self.store = DeviceStore(self._db)
        self.requests = RequestLog(self._db)
        self._identity_dir = identity_dir
        self._listener: RemoteListener | None = None
        self._shut = False
        self.handlers = RemoteHandlers(
            manager,
            settings,
            self.requests,
            identity=self._identity,
            live=lambda job_id: self.tracker.live(job_id),
            analyze=self._analyze,
        )
        self.tracker = EventTracker(self._job_object, self.handlers.global_state)
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)
        self._analyze_threads: list[Any] = []
        #: Devices removed in this session; requests already queued for them are dropped.
        self._removed: set[str] = set()
        # Safety net: a window that is never closed (tests, a crash path) must
        # not leave the listener thread running at interpreter exit.
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)
        ref = weakref.ref(self)
        atexit.register(_shutdown_ref, ref)

    # ------------------------------------------------------------ listener

    @property
    def listener(self) -> RemoteListener:
        """Created on first need (a paired phone or a pairing), so a PC that
        never pairs a phone never even starts the listener thread."""
        if self._listener is None:
            listener = RemoteListener(
                self.store,
                port=self.port_setting(),
                test_mode=self.loopback_setting(),
                identity_dir=self._identity_dir,
            )
            listener.requestReady.connect(self._on_request)
            listener.subscribersChanged.connect(self._on_subscribers)
            listener.pairingStarted.connect(self.pairingStarted)
            listener.pairingPending.connect(self.pairingPending)
            listener.pairingLockedWarning.connect(self.pairingLockedWarning)
            listener.pairingFinished.connect(self.pairingFinished)
            listener.devicesChanged.connect(self.devicesChanged)
            listener.stateChanged.connect(self.stateChanged)
            listener.errorOccurred.connect(self.errorOccurred)
            self._listener = listener
        return self._listener

    @property
    def listener_created(self) -> bool:
        return self._listener is not None

    def _identity(self) -> tuple[str, str]:
        if self._listener is not None:
            return self._listener.pc_id, self._listener.pc_name
        return self.store.pc_id(), default_pc_name()

    # ----------------------------------------------------------- settings

    def _get(self, key: str) -> str | None:
        return self._db.get_setting(key)

    def enabled_setting(self) -> bool:
        return self._get(KEY_ON) != "0"

    def port_setting(self) -> int:
        try:
            port = int(self._get(KEY_PORT) or DEFAULT_PORT)
        except ValueError:
            return DEFAULT_PORT
        return port if 1024 <= port <= 65535 else DEFAULT_PORT

    def loopback_setting(self) -> bool:
        return self._get(KEY_LOOPBACK) == "1"

    def set_enabled(self, on: bool) -> None:
        self._db.set_setting(KEY_ON, "1" if on else "0")
        if on:
            if self.store.count() > 0:
                self.listener.start()
        elif self._listener is not None:
            self._listener.stop()

    def set_port(self, port: int) -> None:
        self._db.set_setting(KEY_PORT, str(port))
        if self._listener is not None:
            self._listener.set_port(port)

    def set_loopback(self, on: bool) -> None:
        self._db.set_setting(KEY_LOOPBACK, "1" if on else "0")
        if self._listener is not None:
            self._listener.set_test_mode(on)

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self.enabled_setting() and self.store.count() > 0:
            self.listener.start()

    def shutdown(self) -> None:
        if self._shut:
            return
        self._shut = True
        self._timer.stop()
        for thread in list(self._analyze_threads):
            thread.wait(2000)
        if self._listener is not None:
            self._listener.shutdown()

    # ------------------------------------------------------------- pairing

    def start_pairing(self) -> None:
        if not self.enabled_setting():
            self._db.set_setting(KEY_ON, "1")
        self.listener.start()
        self.listener.start_pairing()

    def cancel_pairing(self) -> None:
        if self._listener is not None:
            self._listener.cancel_pairing()

    def approve_pairing(self) -> None:
        if self._listener is not None:
            self._listener.approve_pairing()

    def deny_pairing(self) -> None:
        if self._listener is not None:
            self._listener.deny_pairing()

    def remove_device(self, device_id: str) -> bool:
        """Revoke at once (QA-06): connections close before this returns."""
        self._removed.add(device_id)
        removed = (
            self._listener.revoke_device(device_id)
            if self._listener is not None
            else self.store.remove(device_id)
        )
        self.requests.forget_device(device_id)
        return removed

    # --------------------------------------------------- main window hooks

    def resolve_folder(self, folder_id: str) -> Path | None:
        return folders.resolve_folder(self._settings, self._db, folder_id)

    def add_outcome(self, outcome: AddOutcome) -> None:
        if outcome.device_id and outcome.request_id and outcome.job_ids:
            self.requests.record_jobs(outcome.device_id, outcome.request_id, outcome.job_ids)
        if outcome.result == "failed":
            # The message can hold the URL or a path: keep it out of the log.
            log.info("a phone link could not be added")

    # ------------------------------------------------------------ requests

    @Slot(int, str, object)
    def _on_request(self, token: int, device_id: str, request: object) -> None:
        if not isinstance(request, Request):
            self.listener.replyError(token, "internal", "internal error")
            return
        # PROTOCOL 8: a request queued before a revoke must not act after it.
        # The listener drops the reply; this stops the side effect too.
        if device_id in self._removed or self.store.get(device_id) is None:
            self.listener.replyError(token, "unauthorized", "unauthorized")
            return
        if request.type == "subscribe":
            self.listener.reply(
                token, {"type": "subscribed", "state": self.handlers.global_state()}
            )
            self._poll()  # prime the tracker so the first diff has a base
            return

        def respond(body: dict[str, Any]) -> None:
            self.listener.reply(token, body)

        def fail(error: RequestError) -> None:
            self.listener.replyError(token, error.code, error.message, error.extra or None)

        self.handlers.handle(device_id, request, respond, fail)

    def _job_object(self, job_id: int) -> dict[str, Any] | None:
        job = self._db.get_job(job_id)
        return self.handlers.job(job, None) if job is not None else None

    # --------------------------------------------------------------- events

    @Slot(int)
    def _on_subscribers(self, count: int) -> None:
        if count > 0 and not self._timer.isActive():
            self._timer.start()
        elif count == 0:
            self._timer.stop()

    def _poll(self) -> None:
        try:
            events = self.tracker.update(self._manager.snapshot(), time.monotonic())
        except Exception:
            log.exception("building phone events failed")
            return
        for event in events:
            self.listener.broadcast(event)

    # -------------------------------------------------------------- analyze

    def _analyze(
        self,
        url: str,
        respond: Any,
        fail: Any,
    ) -> None:
        from app.ui import work_threads

        thread = work_threads.ResolveThread(
            self._resolver, url, self._settings, None, None, (), None, self
        )
        done = {"sent": False}

        def finish_once(action: Any) -> None:
            if done["sent"]:
                return
            done["sent"] = True
            action()

        def resolved(resolution: object, *_rest: object) -> None:
            try:
                body = analysis_reply(url, resolution)
            except RequestError as exc:
                error = exc
                finish_once(lambda: fail(error))
                return
            finish_once(lambda: respond(body))

        def finished() -> None:
            if thread in self._analyze_threads:
                self._analyze_threads.remove(thread)
            thread.deleteLater()

        def timed_out() -> None:
            finish_once(
                lambda: fail(RequestError("failed", "analysis timed out", reason="timeout"))
            )

        thread.resolved.connect(resolved)
        thread.finished.connect(finished)
        self._analyze_threads.append(thread)
        QTimer.singleShot(ANALYZE_TIMEOUT_MS, self, timed_out)
        thread.start()


def _shutdown_ref(ref: weakref.ref[RemoteService]) -> None:
    service = ref()
    if service is not None:
        service.shutdown()
