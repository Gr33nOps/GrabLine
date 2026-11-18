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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal, Slot

from app.remote import folders, phonejobs, schema
from app.remote.devices import DeviceStore
from app.remote.events import EventTracker
from app.remote.handlers import RemoteHandlers, analysis_reply
from app.remote.listener import DEFAULT_PORT, RemoteListener, default_pc_name
from app.remote.phonejobs import PhoneSnapshot
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
#: PROTOCOL.md section 20: a command the phone has not answered in 15 s.
COMMAND_TIMEOUT_MS = 15_000

KEY_ON = "remote_listener_on"
KEY_PORT = "remote_port"
KEY_LOOPBACK = "remote_loopback_test"


@dataclass
class _PendingCommand:
    device_id: str
    timer: QTimer


class RemoteService(QObject):
    """Created by the main window; ``shutdown()`` at app exit."""

    #: Re-emitted on the GUI thread for the Settings "Phone" page.
    pairingStarted = Signal(str, str, int)
    pairingPending = Signal(str, str)
    pairingLockedWarning = Signal()
    pairingFinished = Signal(str)
    devicesChanged = Signal()
    stateChanged = Signal(bool)
    errorOccurred = Signal(str)
    #: Section 20: a phone reported its downloads (``deviceId``); read them
    #: with ``phone_jobs()``.
    phoneJobsChanged = Signal(str)
    #: A command sent with ``send_phone_command`` has its outcome:
    #: ``(commandId, result, message)``. ``result`` is what the phone said
    #: (``ok``, ``notFound``, ``refused``, ``failed``) or what the PC
    #: concluded (``timeout``, ``offline``, ``busy``); ``message`` is the
    #: phone's plain-words note or "".
    commandFinished = Signal(int, str, str)
    #: A device got its first connection or lost its last one (``deviceId``).
    deviceOnlineChanged = Signal(str)

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
        self.handlers.on_phone_jobs = self.phoneJobsChanged.emit
        self.handlers.on_phone_result = self._on_phone_result
        self._commands: dict[int, _PendingCommand] = {}
        self._next_command_id = 1
        #: Milliseconds a command may wait for its result (tests shorten it).
        self.command_timeout_ms = COMMAND_TIMEOUT_MS
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
            listener.devicesChanged.connect(self._on_devices_changed)
            listener.presenceChanged.connect(self.deviceOnlineChanged)
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
        self._drop_commands(None, phonejobs.RESULT_OFFLINE)
        for thread in list(self._analyze_threads):
            thread.wait(2000)
        self.handlers.close()
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

    def pending_kind(self) -> str:
        """``phone`` or ``pc``: what the device behind the Allow prompt is."""
        return self._listener.pending_kind if self._listener is not None else "phone"

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
        self.handlers.outbox.forget_device(device_id)
        self._forget_phone(device_id)
        return removed

    @Slot()
    def _on_devices_changed(self) -> None:
        """A device was paired, removed (here or by the phone itself) or seen.
        A device that is gone loses its list and its waiting commands."""
        try:
            known = {device.device_id for device in self.store.list()}
        except Exception:
            known = None
        if known is not None:
            for device_id in self.handlers.phone.retain(known):
                self._drop_commands(device_id, phonejobs.RESULT_OFFLINE)
        self.devicesChanged.emit()

    def _forget_phone(self, device_id: str) -> None:
        self.handlers.phone.forget(device_id)
        self._drop_commands(device_id, phonejobs.RESULT_OFFLINE)

    # ---------------------------------------------- the phone's own downloads

    def phone_jobs(self, device_id: str) -> PhoneSnapshot | None:
        """The newest list this phone reported (``shared``, ``jobs``,
        ``received_at``), or None if it never sent one (an older app). Memory
        only: gone when the phone is removed or the app exits."""
        if self.store.get(device_id) is None:
            return None
        return self.handlers.phone.get(device_id)

    def is_online(self, device_id: str) -> bool:
        """True while the phone has an open connection to this PC."""
        return self._listener is not None and device_id in self._listener.online_devices()

    def send_phone_command(self, device_id: str, job_id: int, action: str) -> int:
        """Ask a phone to pause, resume, cancel, retry or remove one of its
        downloads, or to download it on this PC instead (``moveToPc``). Call it
        only for a user's action.

        Returns the command id, or 0 if ``action`` or ``device_id`` is not
        valid (nothing was sent). The outcome always arrives later through
        ``commandFinished(commandId, result, message)``, also for the cases
        that fail at once (``offline``, ``busy``, ``notFound``)."""
        if action not in phonejobs.ACTIONS or device_id in self._removed:
            return 0
        if self.store.get(device_id) is None:
            return 0
        command_id = self._next_command_id
        self._next_command_id += 1
        snapshot = self.handlers.phone.get(device_id)
        if snapshot is None or snapshot.job(job_id) is None:
            self._finish_soon(command_id, "notFound")
            return command_id
        if self._listener is None:
            self._finish_soon(command_id, phonejobs.RESULT_OFFLINE)
            return command_id
        # Tracked before it is sent, so the timeout covers exactly what was sent.
        self._track_command(command_id, device_id)
        outcome = self._listener.send_phone_command(device_id, command_id, action, job_id)
        if outcome != "sent":
            self._untrack_command(command_id)
            self._finish_soon(
                command_id,
                phonejobs.RESULT_BUSY if outcome == "busy" else phonejobs.RESULT_OFFLINE,
            )
        return command_id

    def _track_command(self, command_id: int, device_id: str) -> None:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(max(1, int(self.command_timeout_ms)))
        timer.timeout.connect(lambda cid=command_id: self._command_timed_out(cid))
        self._commands[command_id] = _PendingCommand(device_id, timer)
        timer.start()

    def _untrack_command(self, command_id: int) -> _PendingCommand | None:
        pending = self._commands.pop(command_id, None)
        if pending is not None:
            pending.timer.stop()
            pending.timer.deleteLater()
        return pending

    def _finish_soon(self, command_id: int, result: str, message: str = "") -> None:
        """Report an outcome once the caller has the command id in hand."""
        QTimer.singleShot(0, self, lambda: self.commandFinished.emit(command_id, result, message))

    def _command_timed_out(self, command_id: int) -> None:
        if self._untrack_command(command_id) is not None:
            self.commandFinished.emit(command_id, phonejobs.RESULT_TIMEOUT, "")

    def _on_phone_result(
        self, device_id: str, command_id: int, result: str, message: str | None
    ) -> None:
        """``phoneCommandResult``: only the device the command went to may
        answer it, and once; anything else is ignored (the reply is still ``ok``)."""
        pending = self._commands.get(command_id)
        if pending is None or pending.device_id != device_id:
            return
        self._untrack_command(command_id)
        # The phone's words are shown to the user: no bidi tricks in them.
        self.commandFinished.emit(command_id, result, phonejobs.neutralise(message or ""))

    def _drop_commands(self, device_id: str | None, result: str) -> None:
        """End every waiting command of one device (or of all) at once."""
        for command_id in [
            cid for cid, pending in self._commands.items() if device_id in (None, pending.device_id)
        ]:
            if self._untrack_command(command_id) is not None:
                self.commandFinished.emit(command_id, result, "")

    # ---------------------------------------------------------- send to phone

    def queue_for_phone(
        self,
        device_id: str | None,
        url: str,
        title: str | None = None,
        quality: str | None = None,
    ) -> int | None:
        """ "Send to my phone": queue ``url`` for one paired device, or for any
        phone (``None``), and tell subscribed phones. Returns the item id, or
        None if the link is not one the protocol carries."""
        clean = schema.valid_url(url)
        if clean is None or (device_id is not None and self.store.get(device_id) is None):
            return None
        outbox = self.handlers.outbox
        item_id = outbox.add(device_id, clean, title[:512] if title else None, quality)
        if self._listener is not None:
            # Only phones pull the outbox; a linked PC never asks for it.
            targets = (
                [device_id]
                if device_id is not None
                else [d.device_id for d in self.store.list() if d.kind == "phone"]
            )
            for target in targets:
                self._listener.broadcast(
                    {"event": "outbox.added", "count": outbox.count_for(target), "_device": target}
                )
        return item_id

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
