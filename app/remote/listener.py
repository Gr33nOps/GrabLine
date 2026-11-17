"""The phone-remote listener: TLS WebSocket server for paired phones.

PROTOCOL.md sections 2, 3, 5-9, 15 and 16. ``RemoteListener`` is a QObject
that moves itself to its own QThread; nothing here ever runs on the UI thread
except the thin public methods, which only post work to that thread.

Transport
---------
Sockets are plain ``QTcpServer``/``QTcpSocket`` objects, one server per
private or link-local address (``app.remote.addresses``), all on one port.
TLS is Python's ``ssl`` module driven through memory BIOs and the WebSocket
framing is ``app.remote.wire``. Qt's own TLS is NOT used: on Windows the
Schannel backend cannot serve an ECDSA P-256 server key ("No credentials"),
and the OpenSSL backend loads whatever ``libssl`` is on PATH. Python's
OpenSSL serves the PROTOCOL.md section 4 identity on every platform. What
that buys, compared with ``QWebSocketServer``:

* the peer address is checked on the accepted TCP socket before one TLS byte
  is read or written (section 3 "before the TLS handshake");
* a wrong path gets a real HTTP 404 and an ``Origin`` header a real 403;
* no extension is negotiated, so ``permessage-deflate`` is impossible;
* an oversized frame is refused from its header (close 1009), never buffered;
* the phone's WebSocket pings are seen, so the 60 s idle rule needs no
  PC-side pings;
* TLS 1.2 or newer, 1.3 when the phone offers it, also on Windows 10.

Order of checks on a frame after login, all on this thread before anything
is handed to the GUI (QA-10): JSON, ``schema.validate``, the device's token
bucket (an invalid request costs 1), flood detection, the 32-unanswered cap.

GUI-facing API (every method is thread-safe; call them from the GUI thread)
--------------------------------------------------------------------------
``start()`` / ``stop()``        the user's on/off switch. On does not mean
                                listening: sockets exist only while a device
                                is paired or a pairing is open (QA-11).
``shutdown()``                  app exit: closes everything, ends the thread.
``set_test_mode(bool)``, ``set_port(int)``, ``set_pc_name(str)``
``start_pairing(pc_name=None)`` -> ``pairingStarted(qr_uri, manual_text,
                                expires_at)`` or ``pairingFinished("failed")``
``cancel_pairing()``, ``approve_pairing()``, ``deny_pairing()``
``revoke_device(device_id)``    blocks until the row is deleted and the
                                device's connections are closed with 4403.
``reply(token, body)``, ``replyError(token, code, message, extra=None)``
                                answer a ``requestReady``; the listener adds
                                ``id`` and ``protocolVersion`` and drops the
                                answer if the connection or device is gone.
``broadcast(event)``            one event dict from ``EventTracker``.
``send_phone_command(device_id, command_id, action, job_id)``
                                one ``phone.command`` event (section 20) to
                                the device's newest connection that
                                subscribed to the ``phone`` topic; returns
                                ``sent``, ``offline``, ``busy`` or ``failed``.
``online_devices()``            ids of devices with an open connection.
``running``, ``port``, ``pc_id``, ``pc_name``, ``bound_addresses()``

Signals (emitted on the listener thread; connect them to slots of QObjects
that live in the GUI thread so Qt queues them - a bare function or lambda
would run on the listener thread):

``requestReady(int token, str device_id, object request)``
    a validated ``schema.Request`` to pass to ``RemoteHandlers.handle``.
    ``subscribe`` arrives here too (the listener has already recorded the
    topics and fills in ``type`` and ``topics`` of the answer; the GUI only
    supplies ``state``). ``unsubscribe`` and ``unpair`` never arrive.
``stateChanged(bool running)``, ``errorOccurred(str code)`` with
    ``portInUse``, ``noAddress`` or ``identity``
``pairingStarted(str, str, int)``, ``pairingPending(str device_name)``,
``pairingLockedWarning()``, ``pairingFinished(str outcome)`` with
    ``approved``, ``denied``, ``expired``, ``locked``, ``cancelled``, ``failed``
``devicesChanged()``            a device was paired, removed or seen
``presenceChanged(str device_id)``  that device got its first connection or
                                lost its last one
``subscribersChanged(int)``     connections subscribed to events right now

Nothing here logs codes, nonces, proofs, signatures, keys or device ids.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import socket as _socket
import ssl
import stat
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, QThread, QTimer, Signal, Slot
from PySide6.QtNetwork import QAbstractSocket, QHostAddress, QNetworkInformation, QTcpServer

from app import __version__
from app.native_host.host import PROTOCOL_VERSION
from app.remote import addresses, certs, fetch, schema, wire
from app.remote.devices import DeviceStore, b64url_encode, clean_device_name
from app.remote.pairing import (
    APPROVAL_TIMEOUT_SECONDS,
    PairingSession,
    PairingState,
    ProofOutcome,
)
from app.remote.ratelimit import CommandLimiter, DeviceLimiter, FailureTracker

log = logging.getLogger(__name__)

DEFAULT_PORT = 47821  # PROTOCOL.md section 1
MIN_CLIENT_VERSION = 6
FRAME_LIMIT = 1_048_576
PREAUTH_FRAME_LIMIT = 4096
PREAUTH_MAX_MESSAGES = 4
AUTH_TIMEOUT_S = 10.0
IDLE_TIMEOUT_S = 60.0
MAX_UNAUTHENTICATED = 8
MAX_UNAUTHENTICATED_PER_ADDRESS = 3
MAX_CONNECTIONS_PER_DEVICE = 2
MAX_UNANSWERED = 32
MAX_ANALYZE_IN_FLIGHT = 2
EVENTS_PER_SECOND = 20
REFRESH_INTERVAL_MS = 5000
TICK_MS = 250
LINGER_S = 1.0  # how long a closed connection may take to go away
MAX_TLS_HANDSHAKE_BYTES = 65536
EVENT_BACKLOG_BYTES = 1_048_576  # unsent bytes above which events are skipped
MAX_BACKLOG_BYTES = 8 * 1_048_576  # unsent bytes above which the peer is dropped
DEFAULT_FEATURES = (
    "jobs",
    "control",
    "global",
    "queues",
    "analyze",
    "events",
    "files",
    "fetch",
    "outbox",
    "phoneJobs",
)

CLOSE_MALFORMED = 4400
CLOSE_UNAUTHORIZED = 4401
CLOSE_REMOVED = 4403
CLOSE_AUTH_TIMEOUT = 4408
CLOSE_REPLACED = 4409
CLOSE_TOO_OLD = 4426
CLOSE_FLOOD = 4429

_TOPICS = ("jobs", "global", "outbox", "phone")
_EVENT_TOPICS = {"job": "jobs", "global": "global", "outbox": "outbox", "phone": "phone"}
_COALESCED = ("job.updated", "global.updated", "outbox.added")
_FEATURE_OF = {
    "analyze": "analyze",
    "files": "files",
    "fetch": "fetch",
    "fetchCancel": "fetch",
    "fileHash": "fetch",
    "outbox": "outbox",
    "outboxAck": "outbox",
    "phoneJobs": "phoneJobs",
    "phoneCommandResult": "phoneJobs",
}
# Before login nothing about the request is echoed back (QA-05).
_PREAUTH_MESSAGE = "bad request"


def default_pc_name() -> str:
    """The host name, made safe to show; never empty."""
    try:
        name = clean_device_name(_socket.gethostname())
    except OSError:
        name = None
    return name or "GrabLine PC"


def _dumps(message: dict[str, Any]) -> str | None:
    """Compact JSON, or None if it cannot be sent (not JSON, or too large)."""
    try:
        text = json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        size = len(text.encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        return None
    return text if size <= FRAME_LIMIT else None


class _Stream:
    """One ``fetch`` in progress on a connection: the open file and the
    flow-control counters (all absolute file offsets)."""

    def __init__(self, stream_id: int, handle: Any, ticket: fetch.FileTicket, now: float) -> None:
        self.id = stream_id
        self.file = handle
        self.end = ticket.offset + ticket.length
        self.next = ticket.offset  # first byte not yet queued
        self.acked = ticket.offset  # first byte the phone has not confirmed
        self.last_ack = now

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.file.close()


class _Channel(QObject):
    """One accepted connection: TCP socket, TLS state, WebSocket framing and
    the protocol state the listener keeps for it. Listener thread only."""

    def __init__(
        self, owner: RemoteListener, sock: Any, context: ssl.SSLContext, peer: str, now: float
    ) -> None:
        super().__init__()
        self._owner = owner
        self._sock = sock
        self._incoming = ssl.MemoryBIO()
        self._outgoing = ssl.MemoryBIO()
        self._tls = context.wrap_bio(self._incoming, self._outgoing, server_side=True)
        sock.setParent(self)
        self.peer = peer
        self.stage = "tls"  # tls -> http -> open -> closing -> dead
        self._handshake_bytes = 0
        self._head = bytearray()
        self._parser = wire.FrameParser(PREAUTH_FRAME_LIMIT, CLOSE_MALFORMED)
        self._ended = False
        self.linger_until = 0.0
        # Protocol state, owned by the listener.
        self.nonce = b""
        self.deadline: float | None = now + AUTH_TIMEOUT_S
        self.last_rx = now
        self.device_id: str | None = None
        self.preauth_messages = 0
        self.auth_used = False
        self.pair_request_id: str | None = None
        self.pending: dict[str, int] = {}
        self.topics: set[str] = set()
        self.seq = 0
        self.gap = False
        self.sent_at: deque[float] = deque()
        self.deferred: dict[tuple[str, object], dict[str, Any]] = {}
        self.stream: _Stream | None = None
        self.stream_seq = 0
        self.ack_times: deque[float] = deque()
        self.pumping = False
        sock.readyRead.connect(self._on_ready_read)
        sock.bytesWritten.connect(self._on_bytes_written)
        sock.disconnected.connect(self._on_disconnected)

    # ----------------------------------------------------------- reading

    @Slot()
    def _on_ready_read(self) -> None:
        if self.stage == "dead":
            return
        data = bytes(self._sock.readAll().data())
        if not data or self.stage == "closing":
            return  # after our close frame the peer's bytes are discarded
        try:
            self._incoming.write(data)
            if self.stage == "tls":
                self._handshake_bytes += len(data)
                if self._handshake_bytes > MAX_TLS_HANDSHAKE_BYTES:
                    self.abort()
                    return
                try:
                    self._tls.do_handshake()
                except ssl.SSLWantReadError:
                    self._flush()
                    return
                self.stage = "http"
            while self.stage in ("http", "open"):
                try:
                    chunk = self._tls.read(65536)
                except ssl.SSLWantReadError:
                    break
                if not chunk:
                    self.abort()  # TLS closed by the peer
                    return
                self._plain(chunk)
            self._flush()
        except (ssl.SSLError, OSError, ValueError):
            self.abort()

    @Slot()
    def _on_bytes_written(self) -> None:
        if self.stream is not None:
            self._owner.channel_writable(self)

    def _plain(self, chunk: bytes) -> None:
        if self.stage == "open":
            self._frames(chunk)
            return
        self._head += chunk
        end = self._head.find(b"\r\n\r\n")
        if end < 0:
            if len(self._head) > wire.MAX_UPGRADE_BYTES:
                self.abort()
            return
        if end > wire.MAX_UPGRADE_BYTES:
            self.abort()
            return
        rest = bytes(self._head[end + 4 :])
        status, accept = wire.parse_upgrade(bytes(self._head[:end]))
        self._head.clear()
        self._tls.write(wire.upgrade_response(status, accept))
        if status != 101:
            self._end()
            return
        self.stage = "open"
        self._owner.channel_opened(self)
        if rest and self.stage == "open":
            self._frames(rest)

    def _frames(self, data: bytes) -> None:
        for kind, value in self._parser.feed(data):
            if self.stage != "open":
                return
            self.last_rx = time.monotonic()
            if kind == "text" and isinstance(value, str):
                self._owner.channel_text(self, value)
            elif kind == "ping" and isinstance(value, bytes):
                self._tls.write(wire.encode_frame(wire.OP_PONG, value))
            elif kind == "close":
                self.close(wire.CLOSE_NORMAL)
            elif kind == "fail":
                self.close(value if isinstance(value, int) else wire.CLOSE_PROTOCOL_ERROR)

    # ----------------------------------------------------------- writing

    def _flush(self) -> None:
        data = self._outgoing.read()
        if data and self.stage != "dead":
            self._sock.write(data)

    def backlog(self) -> int:
        return int(self._sock.bytesToWrite())

    def set_frame_limit(self, limit: int) -> None:
        self._parser.max_message = limit

    def send_binary(self, payload: bytes) -> bool:
        if self.stage != "open":
            return False
        try:
            self._tls.write(wire.encode_frame(wire.OP_BINARY, payload))
            self._flush()
        except (ssl.SSLError, OSError, ValueError):
            self.abort()
            return False
        return True

    def send_text(self, text: str) -> bool:
        if self.stage != "open":
            return False
        if self.backlog() > MAX_BACKLOG_BYTES:
            self.abort()  # the peer stopped reading
            return False
        try:
            self._tls.write(wire.encode_frame(wire.OP_TEXT, text.encode("utf-8")))
            self._flush()
        except (ssl.SSLError, OSError, ValueError):
            self.abort()
            return False
        return True

    # ----------------------------------------------------------- closing

    def close(self, code: int) -> None:
        """Send a close frame and stop serving. The socket is left for the
        peer to close (so the frame is not lost to a reset) and is aborted
        after ``LINGER_S`` if it does not."""
        if self.stage != "open":
            if self.stage != "closing":
                self.abort()
            return
        with contextlib.suppress(ssl.SSLError, OSError, ValueError):
            self._tls.write(wire.encode_close(code))
        self._end()

    def _end(self) -> None:
        self.stage = "closing"
        self.linger_until = time.monotonic() + LINGER_S
        with contextlib.suppress(ssl.SSLError, OSError, ValueError):
            self._flush()
            self._sock.flush()
        self._notify()

    def abort(self) -> None:
        if self.stage == "dead":
            return
        self.stage = "dead"
        self._sock.abort()
        self._notify()
        self._owner.channel_gone(self)

    @Slot()
    def _on_disconnected(self) -> None:
        if self.stage == "dead":
            return
        self.stage = "dead"
        self._notify()
        self._owner.channel_gone(self)

    def _notify(self) -> None:
        if not self._ended:
            self._ended = True
            self._owner.channel_ended(self)


class RemoteListener(QObject):
    """See the module docstring for the API."""

    requestReady = Signal(int, str, object)
    stateChanged = Signal(bool)
    errorOccurred = Signal(str)
    pairingStarted = Signal(str, str, int)
    pairingPending = Signal(str)
    pairingLockedWarning = Signal()
    pairingFinished = Signal(str)
    devicesChanged = Signal()
    presenceChanged = Signal(str)
    subscribersChanged = Signal(int)
    _invoke = Signal(object)

    def __init__(
        self,
        store: DeviceStore,
        *,
        port: int = DEFAULT_PORT,
        pc_name: str | None = None,
        test_mode: bool = False,
        identity_dir: Path | None = None,
        features: tuple[str, ...] = DEFAULT_FEATURES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__()
        self._store = store
        self._port = port
        self._pc_name = (clean_device_name(pc_name) if pc_name else None) or default_pc_name()
        self._pc_id = store.pc_id()
        self._test_mode = bool(test_mode)
        self._identity_dir = identity_dir
        self._features = tuple(features)
        self._clock = clock

        self._alive = True
        self._ident: int | None = None
        self._enabled = False
        self._active = False
        self._running = False
        self._bound_port = 0
        self._bound: tuple[str, ...] = ()
        self._last_error = ""
        self._pin = ""
        self._tls_context: ssl.SSLContext | None = None

        self._servers: dict[str, QTcpServer] = {}
        self._channels: set[_Channel] = set()
        self._lingering: set[_Channel] = set()
        self._by_device: dict[str, list[_Channel]] = {}
        self._limiters: dict[str, DeviceLimiter] = {}
        self._commands = CommandLimiter()
        #: Devices with an open connection; replaced whole so the GUI thread
        #: can read it without a lock.
        self._online: frozenset[str] = frozenset()
        self._failures = FailureTracker()
        self._tokens: dict[int, tuple[_Channel, str, str]] = {}
        self._next_token = 1
        self._subscribers = 0

        self._session: PairingSession | None = None
        self._pair_channel: _Channel | None = None
        self._lock_reported = False
        self._opening_pairing = False

        self._tick_timer: QTimer | None = None
        self._refresh_timer: QTimer | None = None
        self._ticks = 0

        self._thread = QThread()
        self._thread.setObjectName("grabline-remote")
        self.moveToThread(self._thread)
        self._invoke.connect(self._run_posted)
        self._network_info: QNetworkInformation | None = None
        try:
            if QNetworkInformation.loadDefaultBackend():
                self._network_info = QNetworkInformation.instance()
            if self._network_info is not None:
                self._network_info.reachabilityChanged.connect(self._on_network_changed)
        except (RuntimeError, AttributeError):
            self._network_info = None
        self._thread.start()

    # ------------------------------------------------------- thread plumbing

    @Slot(object)
    def _run_posted(self, call: Callable[[], None]) -> None:
        self._ident = threading.get_ident()
        call()

    def _post(self, call: Callable[[], None]) -> None:
        if not self._alive:
            return
        if threading.get_ident() == self._ident:
            call()
        else:
            self._invoke.emit(call)

    def _post_wait(self, call: Callable[[], Any], timeout: float = 5.0) -> Any:
        """Run ``call`` on the listener thread and wait for its result."""
        if not self._alive:
            return None
        if threading.get_ident() == self._ident:
            return call()
        done = threading.Event()
        box: list[Any] = [None]

        def run() -> None:
            try:
                box[0] = call()
            finally:
                done.set()

        self._invoke.emit(run)
        done.wait(timeout)
        return box[0]

    # ------------------------------------------------------------ public API

    @property
    def running(self) -> bool:
        """True while at least one socket is listening."""
        return self._running

    @property
    def port(self) -> int:
        """The port in use (the configured one until a socket is bound)."""
        return self._bound_port or self._port

    @property
    def pc_id(self) -> str:
        return self._pc_id

    @property
    def pc_name(self) -> str:
        return self._pc_name

    def bound_addresses(self) -> list[str]:
        """The local addresses listened on right now."""
        return list(self._bound)

    def start(self) -> None:
        self._post(lambda: self._set_enabled(True))

    def stop(self) -> None:
        self._post(lambda: self._set_enabled(False))

    def shutdown(self) -> None:
        """Close everything and end the thread. The object is dead after."""
        if not self._alive:
            return
        self._post_wait(self._shutdown_in_thread)
        self._alive = False
        if self._network_info is not None:
            with contextlib.suppress(RuntimeError, TypeError):
                self._network_info.reachabilityChanged.disconnect(self._on_network_changed)
        self._thread.quit()
        self._thread.wait(5000)

    def set_test_mode(self, enabled: bool) -> None:
        self._post(lambda: self._set_test_mode(bool(enabled)))

    def set_port(self, port: int) -> None:
        self._post(lambda: self._set_port(port))

    def set_pc_name(self, name: str) -> None:
        cleaned = clean_device_name(name)
        if cleaned is not None:
            self._pc_name = cleaned

    def start_pairing(self, pc_name: str | None = None) -> None:
        self._post(lambda: self._start_pairing(pc_name))

    def cancel_pairing(self) -> None:
        self._post(self._cancel_pairing)

    def approve_pairing(self) -> None:
        self._post(self._approve_pairing)

    def deny_pairing(self) -> None:
        self._post(self._deny_pairing)

    def revoke_device(self, device_id: str) -> bool:
        """Remove a device. Returns after its row is deleted and each of its
        connections has been closed with 4403; True if it existed."""
        return bool(self._post_wait(lambda: self._revoke(device_id)))

    @Slot(int, object)
    def reply(self, token: int, body: dict[str, Any]) -> None:
        self._post(lambda: self._reply(token, body))

    @Slot(int, str, str, object)
    def replyError(
        self, token: int, code: str, message: str, extra: dict[str, Any] | None = None
    ) -> None:
        self._post(lambda: self._reply_error(token, code, message, extra))

    @Slot(object)
    def broadcast(self, event: dict[str, Any]) -> None:
        self._post(lambda: self._broadcast(event))

    def online_devices(self) -> frozenset[str]:
        """Devices with at least one open, logged-in connection right now."""
        return self._online

    def send_phone_command(self, device_id: str, command_id: int, action: str, job_id: int) -> str:
        """Section 20: one ``phone.command`` to the device's newest connection
        that subscribed to ``phone``. Returns ``sent``, ``offline`` (no such
        connection), ``busy`` (over 10 a second to this device) or ``failed``
        (it could not be written)."""
        result = self._post_wait(lambda: self._phone_command(device_id, command_id, action, job_id))
        return result if isinstance(result, str) else "failed"

    # ------------------------------------------------------------ on and off

    def _set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        if not enabled:
            self._drop_session("cancelled")
        self._evaluate()

    def _set_test_mode(self, enabled: bool) -> None:
        if enabled == self._test_mode:
            return
        self._test_mode = enabled
        if self._active:
            if not enabled:
                for channel in list(self._channels):
                    if not addresses.is_allowed_peer(channel.peer, False):
                        channel.close(wire.CLOSE_GOING_AWAY)
            self._refresh()

    def _set_port(self, port: int) -> None:
        if type(port) is not int or not 0 <= port <= 65535 or port == self._port:
            return
        self._port = port
        if self._active:
            self._close_sockets()
            self._refresh()

    def _device_count(self) -> int:
        try:
            return self._store.count()
        except Exception:  # a closed or broken database means "nobody is paired"
            return 0

    def _evaluate(self) -> None:
        wanted = self._enabled and (
            self._session is not None or self._opening_pairing or self._device_count() > 0
        )
        if wanted and not self._active:
            self._open()
        elif not wanted and self._active:
            self._close()

    def _report(self, code: str) -> None:
        if code != self._last_error:
            self._last_error = code
            if code:
                self.errorOccurred.emit(code)

    def _open(self) -> None:
        try:
            identity = certs.load_or_create(self._pc_id, self._identity_dir)
            folder = self._identity_dir if self._identity_dir is not None else certs.remote_dir()
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.verify_mode = ssl.CERT_NONE
            context.options |= ssl.OP_NO_COMPRESSION | getattr(ssl, "OP_NO_RENEGOTIATION", 0)
            context.load_cert_chain(
                str(folder / certs.CERT_FILENAME), str(folder / certs.KEY_FILENAME)
            )
        except Exception:
            log.warning("phone remote: the TLS identity could not be loaded")
            self._report("identity")
            return
        self._pin = identity.pin
        self._tls_context = context
        self._active = True
        if self._tick_timer is None:
            self._tick_timer = QTimer()
            self._tick_timer.setInterval(TICK_MS)
            self._tick_timer.timeout.connect(self._tick)
            self._refresh_timer = QTimer()
            self._refresh_timer.setInterval(REFRESH_INTERVAL_MS)
            self._refresh_timer.timeout.connect(self._refresh)
        self._tick_timer.start()
        if self._refresh_timer is not None:
            self._refresh_timer.start()
        self._refresh()

    def _close(self) -> None:
        """Turn off: every connection gets 1001, every socket is closed."""
        self._active = False
        for channel in list(self._channels):
            channel.close(wire.CLOSE_GOING_AWAY)
        self._close_sockets()
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
        self._tls_context = None
        self._last_error = ""
        self._limiters.clear()

    def _close_sockets(self) -> None:
        for server in self._servers.values():
            server.close()
            server.deleteLater()
        self._servers.clear()
        self._bound_port = 0
        self._publish()

    def _publish(self) -> None:
        self._bound = tuple(self._servers)
        running = bool(self._servers)
        if running != self._running:
            self._running = running
            self.stateChanged.emit(running)

    @Slot()
    def _on_network_changed(self) -> None:
        if self._active:
            self._refresh()

    @Slot()
    def _refresh(self) -> None:
        """Make the listening sockets match the interface list (section 3)."""
        if not self._active:
            return
        try:
            wanted = addresses.bind_addresses(self._test_mode)
        except Exception:
            wanted = []
        for address in [a for a in self._servers if a not in wanted]:
            server = self._servers.pop(address)
            server.close()
            server.deleteLater()
        in_use = False
        for address in wanted:
            if address in self._servers or not addresses.is_bindable(address, self._test_mode):
                continue
            server = QTcpServer()
            server.setMaxPendingConnections(MAX_UNAUTHENTICATED * 2)
            if server.listen(QHostAddress(address), self._bound_port or self._port):
                self._bound_port = self._bound_port or int(server.serverPort())
                server.newConnection.connect(self._on_new_connection)
                self._servers[address] = server
            else:
                if server.serverError() == QAbstractSocket.SocketError.AddressInUseError:
                    in_use = True
                server.deleteLater()
        if not self._servers:
            self._bound_port = 0
        self._report("portInUse" if in_use else ("" if self._servers else "noAddress"))
        self._publish()

    # ---------------------------------------------------------------- accept

    @Slot()
    def _on_new_connection(self) -> None:
        for server in list(self._servers.values()):
            while server.hasPendingConnections():
                self._accept(server.nextPendingConnection())

    def _accept(self, sock: Any) -> None:
        peer = sock.peerAddress().toString()
        context = self._tls_context
        unauthenticated = [c for c in self._channels if c.device_id is None]
        unauthenticated.extend(self._lingering)
        same_peer = sum(1 for c in unauthenticated if c.peer == peer)
        if (
            not self._active
            or context is None
            # Section 3: outside the private ranges, dropped before any TLS byte.
            or not addresses.is_allowed_peer(peer, self._test_mode)
            or self._failures.refused(peer)
            or len(unauthenticated) >= MAX_UNAUTHENTICATED
            or same_peer >= MAX_UNAUTHENTICATED_PER_ADDRESS
        ):
            sock.abort()
            sock.deleteLater()
            return
        sock.setReadBufferSize(2 * FRAME_LIMIT)
        channel = _Channel(self, sock, context, peer, time.monotonic())
        self._channels.add(channel)
        if sock.bytesAvailable():
            channel._on_ready_read()

    # ----------------------------------------------- channel callbacks

    def channel_opened(self, channel: _Channel) -> None:
        """The WebSocket handshake is done: say hello (section 6)."""
        channel.nonce = secrets.token_bytes(32)
        channel.deadline = time.monotonic() + AUTH_TIMEOUT_S
        self._send(
            channel,
            {
                "type": "hello",
                "protocolVersion": PROTOCOL_VERSION,
                "minClientVersion": MIN_CLIENT_VERSION,
                "pcId": self._pc_id,
                "pcName": self._pc_name,
                "nonce": b64url_encode(channel.nonce),
            },
        )

    def channel_ended(self, channel: _Channel) -> None:
        """The connection stopped being served (closed by either side)."""
        self._channels.discard(channel)
        if channel.stage == "closing":
            self._lingering.add(channel)
        device_id = channel.device_id
        if device_id is not None:
            peers = self._by_device.get(device_id, [])
            if channel in peers:
                peers.remove(channel)
            if not peers:
                self._by_device.pop(device_id, None)
                # "Last seen" is when the last connection went away.
                with contextlib.suppress(Exception):
                    self._store.touch(device_id)
            self._sync_presence(device_id)
        for token in channel.pending.values():
            self._tokens.pop(token, None)
        channel.pending.clear()
        channel.deferred.clear()
        if channel.stream is not None:
            channel.stream.close()
            channel.stream = None
        if channel.topics:
            channel.topics.clear()
            self._count_subscribers()
        if channel is self._pair_channel:
            # The phone left during the Allow prompt: nothing is stored.
            self._pair_channel = None
            self._drop_session("cancelled")
            self._evaluate()

    def channel_gone(self, channel: _Channel) -> None:
        """The socket is closed; forget the object."""
        self._channels.discard(channel)
        self._lingering.discard(channel)
        channel.deleteLater()

    def channel_text(self, channel: _Channel, text: str) -> None:
        deep = False
        message: object = None
        try:
            message = json.loads(text)
        except RecursionError:
            deep = True
        except ValueError:
            channel.close(CLOSE_MALFORMED)
            return
        if not deep and not isinstance(message, dict):
            channel.close(CLOSE_MALFORMED)
            return
        if channel.device_id is None:
            self._preauth(channel, None if deep else message)
        else:
            self._authed(channel, None if deep else message)

    # ---------------------------------------------------------------- sending

    def _send(self, channel: _Channel, message: dict[str, Any]) -> bool:
        text = _dumps(message)
        return text is not None and channel.send_text(text)

    def _send_error(
        self,
        channel: _Channel,
        request_id: str | None,
        code: str,
        message: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        reply: dict[str, Any] = {}
        if request_id is not None:
            reply["id"] = request_id
        reply.update(type="error", protocolVersion=PROTOCOL_VERSION, code=code, message=message)
        for key, value in (extra or {}).items():
            reply.setdefault(key, value)
        if _dumps(reply) is None:
            reply = {k: reply[k] for k in ("id", "type", "protocolVersion", "code") if k in reply}
            reply["message"] = "error"
        self._send(channel, reply)

    # ------------------------------------------------------ before authOk

    def _refuse(self, channel: _Channel, request_id: str | None) -> None:
        """The one answer for anything not allowed before login (QA-05)."""
        self._failures.fail(channel.peer)
        self._send_error(channel, request_id, "unauthorized", "not authorized")
        channel.close(CLOSE_UNAUTHORIZED)

    def _preauth(self, channel: _Channel, message: object) -> None:
        channel.preauth_messages += 1
        request_id = schema.request_id(message)
        kind = message.get("type") if isinstance(message, dict) else None
        if (
            channel.preauth_messages > PREAUTH_MAX_MESSAGES
            or not isinstance(kind, str)
            or kind not in ("pair", "auth")
            or channel.pair_request_id is not None
        ):
            self._refuse(channel, request_id)
            return
        try:
            request = schema.validate(message)
        except schema.RequestError as exc:
            self._failures.fail(channel.peer)
            self._send_error(channel, request_id, exc.code, _PREAUTH_MESSAGE)
            return
        except Exception:
            self._refuse(channel, request_id)
            return
        try:
            if kind == "auth":
                self._auth(channel, request)
            else:
                self._pair(channel, request)
        except Exception:
            # Fail closed on anything unexpected in the login or pairing path.
            log.warning("phone remote: a pre-login request failed unexpectedly")
            if channel is self._pair_channel:
                self._pair_channel = None
                self._drop_session("failed")
            self._refuse(channel, request_id)
            self._evaluate()

    def _auth(self, channel: _Channel, request: schema.Request) -> None:
        if channel.auth_used:
            self._refuse(channel, request.id)
            return
        channel.auth_used = True  # the nonce is good for one attempt
        fields = request.fields
        device = self._store.authenticate(
            fields["deviceId"], channel.nonce, self._pin, fields["signature"]
        )
        if device is None:
            self._refuse(channel, request.id)
            return
        if fields["protocolVersion"] < MIN_CLIENT_VERSION:
            self._too_old(channel, request.id)
            return
        device_id = device.device_id
        limiter = self._limiters.setdefault(device_id, DeviceLimiter())
        refused_ms = limiter.refused_ms()
        if refused_ms:
            self._send_error(
                channel, request.id, "rateLimited", "slow down", {"retryAfterMs": refused_ms}
            )
            channel.close(CLOSE_FLOOD)
            return
        channel.device_id = device_id
        channel.deadline = None
        channel.set_frame_limit(FRAME_LIMIT)
        peers = self._by_device.setdefault(device_id, [])
        peers.append(channel)
        while len(peers) > MAX_CONNECTIONS_PER_DEVICE:
            oldest = peers[0]
            oldest.close(CLOSE_REPLACED)  # removes itself from ``peers``
            if oldest in peers:
                peers.remove(oldest)
        with contextlib.suppress(Exception):
            self._store.touch(device_id)
        self._sync_presence(device_id)
        self._send(
            channel,
            {
                "id": request.id,
                "type": "authOk",
                "protocolVersion": PROTOCOL_VERSION,
                "pcId": self._pc_id,
                "pcName": self._pc_name,
                "appVersion": __version__,
                "appRunning": True,
                "serverTime": int(self._clock()),
                "addresses": addresses.advertised(self._bound, self._test_mode),
                "port": self._bound_port,
                "features": list(self._features),
            },
        )
        self.devicesChanged.emit()

    def _too_old(self, channel: _Channel, request_id: str) -> None:
        self._send_error(
            channel,
            request_id,
            "versionTooOld",
            "this phone app is too old for this PC",
            {"minClientVersion": MIN_CLIENT_VERSION},
        )
        channel.close(CLOSE_TOO_OLD)

    def _pair(self, channel: _Channel, request: schema.Request) -> None:
        session = self._session
        if session is None:
            self._failures.fail(channel.peer)
            self._send_error(channel, request.id, "pairingExpired", "no pairing is open")
            return
        fields = request.fields
        result = session.submit_proof(
            nonce=channel.nonce,
            public_key=fields["publicKey"],
            code_proof=fields["codeProof"],
            device_name=fields["deviceName"],
        )
        outcome = result.outcome
        if outcome is ProofOutcome.ACCEPTED:
            if fields["protocolVersion"] < MIN_CLIENT_VERSION:
                self._drop_session("failed")
                self._too_old(channel, request.id)
                self._evaluate()
                return
            channel.pair_request_id = request.id
            channel.deadline = None  # the prompt's 60 s applies instead
            self._pair_channel = channel
            name = session.pending_device_name or ""
            self._send(
                channel,
                {
                    "type": "pairPending",
                    "protocolVersion": PROTOCOL_VERSION,
                    "timeoutSeconds": APPROVAL_TIMEOUT_SECONDS,
                },
            )
            self.pairingPending.emit(name)
            return
        self._failures.fail(channel.peer)
        if outcome is ProofOutcome.BAD_CODE:
            self._send_error(
                channel,
                request.id,
                "badCode",
                "wrong pairing code",
                {"attemptsLeft": result.attempts_left or 0},
            )
        elif outcome is ProofOutcome.PAIRING_LOCKED:
            self._send_error(channel, request.id, "pairingLocked", "too many wrong codes")
            self._report_lock()
        elif outcome is ProofOutcome.PAIRING_EXPIRED:
            self._send_error(channel, request.id, "pairingExpired", "no pairing is open")
        elif outcome is ProofOutcome.CONFLICT:
            self._send_error(channel, request.id, "conflict", "too many devices are paired")
            self._drop_session("failed")
            self._evaluate()
        else:
            self._send_error(channel, request.id, "badRequest", _PREAUTH_MESSAGE)

    # --------------------------------------------------------------- pairing

    def _start_pairing(self, pc_name: str | None) -> None:
        if pc_name:
            self.set_pc_name(pc_name)
        self._drop_session("cancelled")
        self._enabled = True
        self._opening_pairing = True
        try:
            self._evaluate()
            listed = addresses.advertised(self._bound, self._test_mode)
            if not self._servers or not listed:
                raise ValueError("nothing to listen on")
            session = PairingSession(
                pc_id=self._pc_id,
                pc_name=self._pc_name,
                addresses=listed,
                port=self._bound_port,
                pin=self._pin,
                store=self._store,
                clock=self._clock,
            )
            uri = session.qr_uri()
            manual = session.manual_entry_text()
            if uri is None or manual is None:
                raise ValueError("the session ended at once")
        except Exception:
            self._opening_pairing = False
            if not self._last_error:
                self._report("noAddress")
            self.pairingFinished.emit("failed")
            self._evaluate()
            return
        self._opening_pairing = False
        self._session = session
        self._lock_reported = False
        self.pairingStarted.emit(uri, manual, session.expires_at)

    def _drop_session(self, outcome: str) -> None:
        """End the pairing session, if any, storing nothing. A phone waiting
        on the prompt is told ``pairingExpired`` and closed with 4403."""
        session = self._session
        if session is None:
            return
        self._session = None
        channel, self._pair_channel = self._pair_channel, None
        session.cancel()
        if channel is not None:
            self._send_error(channel, channel.pair_request_id, "pairingExpired", "pairing ended")
            channel.pair_request_id = None
            channel.close(CLOSE_REMOVED)
        if not self._lock_reported:
            self.pairingFinished.emit(outcome)
        self._lock_reported = False

    def _report_lock(self) -> None:
        if self._session is not None and not self._lock_reported:
            self._lock_reported = True
            self.pairingLockedWarning.emit()
            self.pairingFinished.emit("locked")

    def _cancel_pairing(self) -> None:
        self._drop_session("cancelled")
        self._evaluate()

    def _approve_pairing(self) -> None:
        session, channel = self._session, self._pair_channel
        if session is None or channel is None:
            return
        paired = session.approve()
        if paired is None:
            self._drop_session("expired")
            self._evaluate()
            return
        self._session = None
        self._pair_channel = None
        request_id, channel.pair_request_id = channel.pair_request_id, None
        channel.deadline = time.monotonic() + AUTH_TIMEOUT_S
        self._send(
            channel,
            {
                "id": request_id,
                "type": "paired",
                "protocolVersion": PROTOCOL_VERSION,
                "deviceId": paired.device.device_id,
                "pcId": self._pc_id,
                "pcName": self._pc_name,
                "serverProof": b64url_encode(paired.server_proof),
            },
        )
        self.pairingFinished.emit("approved")
        self.devicesChanged.emit()
        self._evaluate()

    def _deny_pairing(self) -> None:
        session, channel = self._session, self._pair_channel
        if session is None or channel is None:
            return
        if not session.deny():
            self._drop_session("expired")
            self._evaluate()
            return
        self._session = None
        self._pair_channel = None
        self._send_error(
            channel, channel.pair_request_id, "pairingDenied", "pairing was declined on the PC"
        )
        channel.pair_request_id = None
        channel.close(CLOSE_REMOVED)
        self.pairingFinished.emit("denied")
        self._evaluate()

    def _check_session(self) -> None:
        session = self._session
        if session is None:
            return
        if self._lock_reported:
            # A locked session stays to answer ``pairingLocked`` until the
            # code would have expired anyway.
            if self._clock() >= session.expires_at:
                self._drop_session("locked")
                self._evaluate()
            return
        state = session.state
        if state is PairingState.LOCKED:
            self._report_lock()
        elif state is PairingState.EXPIRED:
            self._drop_session("expired")
            self._evaluate()
        elif state not in (PairingState.OPEN, PairingState.AWAITING_APPROVAL):
            self._drop_session("failed")
            self._evaluate()

    # -------------------------------------------------------- after authOk

    def _authed(self, channel: _Channel, message: object) -> None:
        device_id = channel.device_id
        if device_id is None:
            return
        if isinstance(message, dict) and message.get("type") == "fetchAck":
            self._ack(channel, message)  # a notification: no reply, no token
            return
        limiter = self._limiters.setdefault(device_id, DeviceLimiter())
        request_id = schema.request_id(message)
        request: schema.Request | None = None
        error = schema.RequestError("badRequest", "message: nested too deeply")
        if message is not None:
            try:
                request = schema.validate(message)
            except schema.RequestError as exc:
                error = exc
            except Exception:
                error = schema.RequestError("badRequest", "message: invalid")
        # Everything is charged, valid or not, before anything else happens.
        wait_ms = limiter.take(request.cost if request is not None else 1)
        if not wait_ms and request is not None:
            if len(channel.pending) >= MAX_UNANSWERED:
                wait_ms = 250
            elif request.type == "analyze" and self._analyze_in_flight(device_id) >= (
                MAX_ANALYZE_IN_FLIGHT
            ):
                wait_ms = 1000
        if wait_ms:
            if limiter.note_limited():
                for peer in list(self._by_device.get(device_id, [])):
                    peer.close(CLOSE_FLOOD)
                return
            self._send_error(
                channel, request_id, "rateLimited", "slow down", {"retryAfterMs": wait_ms}
            )
            return
        if request is None:
            self._send_error(channel, request_id, error.code, error.message, error.extra)
            return
        kind = request.type
        if request.id in channel.pending:
            self._send_error(channel, request.id, "badRequest", "id: already in use")
        elif kind in ("pair", "auth"):
            self._send_error(channel, request.id, "badRequest", "type: already logged in")
        elif kind in _FEATURE_OF and _FEATURE_OF[kind] not in self._features:
            self._send_error(channel, request.id, "unsupported", "not served by this PC")
        elif kind == "unsubscribe":
            channel.topics.clear()
            channel.deferred.clear()
            self._count_subscribers()
            self._send(
                channel, {"id": request.id, "type": "ok", "protocolVersion": PROTOCOL_VERSION}
            )
        elif kind == "unpair":
            self._unpair(channel, request.id)
        elif kind == "fetchCancel":
            self._cancel_stream(channel, request)
        elif kind == "fetch" and self._streams_busy(channel, device_id):
            self._send_error(channel, request.id, "conflict", "a transfer is already running")
        else:
            if kind == "subscribe":
                channel.topics = set(request.fields["topics"])
                self._count_subscribers()
            token = self._next_token
            self._next_token += 1
            channel.pending[request.id] = token
            self._tokens[token] = (channel, request.id, kind)
            self.requestReady.emit(token, device_id, request)

    def _analyze_in_flight(self, device_id: str) -> int:
        return sum(
            1
            for channel, _rid, kind in self._tokens.values()
            if kind == "analyze" and channel.device_id == device_id
        )

    # --------------------------------------------------------------- fetch

    def _streams_busy(self, channel: _Channel, device_id: str) -> bool:
        """Section 9: one stream per connection, two per device. A ``fetch``
        the GUI is still answering counts as a stream."""

        def busy(peer: _Channel) -> bool:
            return peer.stream is not None or any(
                owner is peer and kind == "fetch" for owner, _rid, kind in self._tokens.values()
            )

        if busy(channel):
            return True
        peers = self._by_device.get(device_id, [])
        return sum(1 for peer in peers if busy(peer)) >= fetch.MAX_STREAMS_PER_DEVICE

    def _open_stream(
        self, channel: _Channel, request_id: str, ticket: object, now: float
    ) -> _Stream | None:
        """Open the file a ``FileTicket`` names. The path came from the PC's
        own job table, never from the phone; the handle is checked again
        because the file may have changed since the GUI looked."""
        if not isinstance(ticket, fetch.FileTicket):
            self._send_error(channel, request_id, "internal", "internal error")
            return None
        if channel.stream is not None:
            self._send_error(channel, request_id, "conflict", "a transfer is already running")
            return None
        try:
            handle = open(ticket.path, "rb")  # noqa: SIM115 - held for the stream
        except OSError:
            self._send_error(channel, request_id, "notFound", "the file is gone")
            return None
        try:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise FileNotFoundError
            if info.st_size != ticket.size or fetch.etag_of(info) != ticket.etag:
                handle.close()
                self._send_error(channel, request_id, "conflict", "the file changed")
                return None
            handle.seek(ticket.offset)
        except OSError:
            handle.close()
            self._send_error(channel, request_id, "notFound", "the file is gone")
            return None
        channel.stream_seq += 1
        return _Stream(channel.stream_seq, handle, ticket, now)

    def _cancel_stream(self, channel: _Channel, request: schema.Request) -> None:
        stream = channel.stream
        self._send(channel, {"id": request.id, "type": "ok", "protocolVersion": PROTOCOL_VERSION})
        if stream is not None and stream.id == request.fields["streamId"]:
            self._end_stream(channel, "cancelled")

    def _end_stream(self, channel: _Channel, status: str, code: str | None = None) -> None:
        stream = channel.stream
        if stream is None:
            return
        channel.stream = None
        stream.close()
        event: dict[str, Any] = {
            "type": "fetchEnd",
            "protocolVersion": PROTOCOL_VERSION,
            "streamId": stream.id,
            "status": status,
            "nextOffset": stream.next if status == "complete" else stream.acked,
        }
        if code is not None:
            event["code"] = code
        self._send(channel, event)

    def _ack(self, channel: _Channel, message: dict[str, Any]) -> None:
        """``fetchAck``: no id, no reply, not charged to the token bucket.
        At most 50 a second per connection; anything extra or odd is ignored."""
        now = time.monotonic()
        times = channel.ack_times
        while times and now - times[0] >= 1.0:
            times.popleft()
        if len(times) >= fetch.ACKS_PER_SECOND:
            return
        times.append(now)
        stream = channel.stream
        stream_id, received = message.get("streamId"), message.get("received")
        if (
            stream is None
            or type(stream_id) is not int
            or stream_id != stream.id
            or type(received) is not int
            or received < 0
        ):
            return
        stream.acked = max(stream.acked, min(received, stream.next))
        stream.last_ack = now
        self._pump(channel)

    def channel_writable(self, channel: _Channel) -> None:
        """The socket drained some bytes: more of the file may be queued."""
        self._pump(channel)

    def _pump(self, channel: _Channel) -> None:
        """Queue chunks while the phone's window and the socket backlog allow
        (section 13): never a byte at or beyond ``acked + windowBytes``, never
        a chunk while more than 1 MiB is waiting to be written."""
        stream = channel.stream
        if stream is None or channel.stage != "open" or channel.pumping:
            return
        channel.pumping = True
        try:
            while channel.stream is stream and stream.next < stream.end:
                room = stream.acked + fetch.WINDOW_BYTES - stream.next
                if room <= 0 or channel.backlog() > fetch.STREAM_BACKLOG_BYTES:
                    return
                want = min(fetch.CHUNK_SIZE, stream.end - stream.next, room)
                try:
                    data = stream.file.read(want)
                except OSError:
                    data = b""
                if not data:  # the file shrank under us
                    self._end_stream(channel, "error", "failed")
                    return
                if not channel.send_binary(fetch.frame(stream.id, stream.next, data)):
                    return
                stream.next += len(data)
            if channel.stream is stream:
                self._end_stream(channel, "complete")
        finally:
            channel.pumping = False

    def _sync_presence(self, device_id: str) -> None:
        """Keep ``online_devices()`` in step with the connection table."""
        was = device_id in self._online
        now = bool(self._by_device.get(device_id))
        if was == now:
            return
        self._online = self._online | {device_id} if now else self._online - {device_id}
        self.presenceChanged.emit(device_id)

    def _phone_command(self, device_id: str, command_id: int, action: str, job_id: int) -> str:
        """On the listener thread. Commands are never coalesced or dropped
        for load: the caller asked for this one and gets a clear answer."""
        target = None
        for channel in reversed(self._by_device.get(device_id, [])):
            if channel.stage == "open" and "phone" in channel.topics:
                target = channel
                break
        if target is None:
            return "offline"
        if not self._commands.allow(device_id):
            return "busy"
        if target.backlog() > EVENT_BACKLOG_BYTES:
            return "failed"
        event = {
            "event": "phone.command",
            "commandId": command_id,
            "action": action,
            "jobId": job_id,
        }
        return "sent" if self._emit_event(target, event, time.monotonic()) else "failed"

    def _count_subscribers(self) -> None:
        count = sum(1 for channel in self._channels if channel.topics)
        if count != self._subscribers:
            self._subscribers = count
            self.subscribersChanged.emit(count)

    def _unpair(self, channel: _Channel, request_id: str) -> None:
        device_id = channel.device_id
        if device_id is None:
            return
        with contextlib.suppress(Exception):
            self._store.remove(device_id)
        self._send(channel, {"id": request_id, "type": "ok", "protocolVersion": PROTOCOL_VERSION})
        channel.close(wire.CLOSE_NORMAL)
        for peer in list(self._by_device.get(device_id, [])):
            peer.close(CLOSE_REMOVED)
        self._limiters.pop(device_id, None)
        self._commands.forget(device_id)
        self.devicesChanged.emit()
        self._evaluate()

    def _revoke(self, device_id: str) -> bool:
        """Section 8, in one step on this thread: delete the row, close the
        device's connections with 4403, drop what was queued for them."""
        try:
            existed = bool(self._store.remove(device_id))
        except Exception:
            existed = False
        for channel in list(self._by_device.get(device_id, [])):
            channel.close(CLOSE_REMOVED)  # also drops its unanswered requests
        self._limiters.pop(device_id, None)
        self._commands.forget(device_id)
        self.devicesChanged.emit()
        self._evaluate()
        return existed

    # ------------------------------------------------------------- replies

    def _take_token(self, token: int) -> tuple[_Channel, str, str] | None:
        entry = self._tokens.pop(token, None) if type(token) is int else None
        if entry is None:
            return None  # the connection closed or the device was removed
        channel, request_id, _kind = entry
        channel.pending.pop(request_id, None)
        return entry if channel.stage == "open" else None

    def _reply(self, token: int, body: dict[str, Any]) -> None:
        entry = self._take_token(token)
        if entry is None:
            return
        channel, request_id, kind = entry
        if not isinstance(body, dict) or not isinstance(body.get("type"), str):
            self._send_error(channel, request_id, "internal", "internal error")
            return
        stream: _Stream | None = None
        if kind == "fetch":
            ticket = body.get("_open")  # the file handle's recipe stays off the wire
            body = {key: value for key, value in body.items() if key != "_open"}
            if body["type"] == "fetchStart":
                stream = self._open_stream(channel, request_id, ticket, time.monotonic())
                if stream is None:
                    return
                body["streamId"] = stream.id
        reply: dict[str, Any] = {
            "id": request_id,
            "type": body["type"],
            "protocolVersion": PROTOCOL_VERSION,
        }
        for key, value in body.items():
            reply.setdefault(key, value)
        if kind == "subscribe":
            reply["type"] = "subscribed"
            reply["topics"] = [topic for topic in _TOPICS if topic in channel.topics]
        sent = self._send(channel, reply)
        if stream is not None:
            if sent and channel.stage == "open":
                channel.stream = stream
                self._pump(channel)  # the first chunks follow the fetchStart reply
            else:
                stream.close()
        if not sent and channel.stage == "open":
            self._send_error(channel, request_id, "internal", "internal error")

    def _reply_error(
        self, token: int, code: str, message: str, extra: dict[str, Any] | None
    ) -> None:
        entry = self._take_token(token)
        if entry is None:
            return
        channel, request_id, kind = entry
        if kind == "subscribe":
            channel.topics.clear()
            self._count_subscribers()
        self._send_error(
            channel,
            request_id,
            code if isinstance(code, str) and code else "internal",
            message if isinstance(message, str) else "",
            extra if isinstance(extra, dict) else None,
        )

    # -------------------------------------------------------------- events

    def _broadcast(self, event: dict[str, Any]) -> None:
        name = event.get("event") if isinstance(event, dict) else None
        if not isinstance(name, str):
            return
        topic = _EVENT_TOPICS.get(name.split(".", 1)[0])
        if topic is None:
            return
        # ``_device`` aims an event (``outbox.added``) at one phone's connections.
        target = event.get("_device")
        if "_device" in event:
            event = {key: value for key, value in event.items() if key != "_device"}
        for channel in list(self._channels):
            if (
                channel.device_id is not None
                and topic in channel.topics
                and (target is None or channel.device_id == target)
            ):
                self._queue_event(channel, name, event)

    @staticmethod
    def _job_key(event: dict[str, Any]) -> object:
        job = event.get("job")
        return job.get("id") if isinstance(job, dict) else event.get("jobId")

    def _queue_event(self, channel: _Channel, name: str, event: dict[str, Any]) -> None:
        """Section 15 overload rules: above 20 events a second progress is
        dropped first, then updates are coalesced to the latest; an event
        that changes state is never dropped without leaving a ``seq`` gap."""
        now = time.monotonic()
        sent = channel.sent_at
        while sent and now - sent[0] >= 1.0:
            sent.popleft()
        busy = len(sent) >= EVENTS_PER_SECOND
        stuck = channel.backlog() > EVENT_BACKLOG_BYTES
        if name == "job.progress":
            if not busy and not stuck:
                self._emit_event(channel, event, now)
            return
        if name in _COALESCED:
            key = (name, self._job_key(event) if name == "job.updated" else None)
            if busy or stuck or key in channel.deferred:
                channel.deferred[key] = event  # the newest replaces the older one
                return
        elif name.startswith("job."):
            # A final state makes a waiting update for the same job pointless.
            channel.deferred.pop(("job.updated", self._job_key(event)), None)
        if stuck:
            channel.gap = True  # the phone sees the gap and reloads
            return
        self._emit_event(channel, event, now)

    def _emit_event(self, channel: _Channel, event: dict[str, Any], now: float) -> bool:
        seq = channel.seq + (2 if channel.gap else 1)
        frame: dict[str, Any] = {
            "type": "event",
            "protocolVersion": PROTOCOL_VERSION,
            "event": event.get("event"),
            "seq": seq,
            "ts": int(self._clock()),
        }
        for key, value in event.items():
            frame.setdefault(key, value)
        if self._send(channel, frame):
            channel.seq = seq
            channel.gap = False
            channel.sent_at.append(now)
            return True
        if channel.stage == "open":
            channel.gap = True  # it could not be encoded: make the phone reload
        return False

    def _flush_deferred(self, channel: _Channel, now: float) -> None:
        sent = channel.sent_at
        while sent and now - sent[0] >= 1.0:
            sent.popleft()
        while (
            channel.deferred
            and len(sent) < EVENTS_PER_SECOND
            and channel.stage == "open"
            and channel.backlog() <= EVENT_BACKLOG_BYTES
        ):
            key = next(iter(channel.deferred))
            self._emit_event(channel, channel.deferred.pop(key), now)

    # ---------------------------------------------------------- housekeeping

    @Slot()
    def _tick(self) -> None:
        now = time.monotonic()
        for channel in list(self._channels):
            if channel.device_id is None:
                if channel.deadline is not None and now >= channel.deadline:
                    if channel.stage == "open":
                        channel.close(CLOSE_AUTH_TIMEOUT)
                    else:
                        channel.abort()
            elif (
                channel.stream is not None and now - channel.stream.last_ack >= fetch.STALL_SECONDS
            ):
                self._end_stream(channel, "error", "failed")  # section 13 "Stall"
            elif now - channel.last_rx >= IDLE_TIMEOUT_S:
                channel.close(wire.CLOSE_GOING_AWAY)
            elif channel.deferred:
                self._flush_deferred(channel, now)
        for channel in list(self._lingering):
            if now >= channel.linger_until:
                channel.abort()
        self._check_session()
        self._ticks += 1
        if self._ticks % 4 == 0 and self._active:
            self._drop_removed_devices()
            self._evaluate()
        if (
            not self._active
            and not self._channels
            and not self._lingering
            and self._tick_timer is not None
        ):
            self._tick_timer.stop()

    def _drop_removed_devices(self) -> None:
        """Belt and braces for section 8: a device deleted behind the
        listener's back loses its connections within a second too."""
        if not self._by_device:
            return
        try:
            known = {device.device_id for device in self._store.list()}
        except Exception:
            return
        for device_id in [d for d in self._by_device if d not in known]:
            for channel in list(self._by_device.get(device_id, [])):
                channel.close(CLOSE_REMOVED)
            self._limiters.pop(device_id, None)
            self._commands.forget(device_id)

    def _shutdown_in_thread(self) -> None:
        self._enabled = False
        self._drop_session("cancelled")
        if self._active:
            self._close()
        for channel in list(self._channels) + list(self._lingering):
            channel.abort()
        for timer in (self._tick_timer, self._refresh_timer):
            if timer is not None:
                timer.stop()
                timer.deleteLater()
        self._tick_timer = None
        self._refresh_timer = None
        app = QCoreApplication.instance()
        if app is not None:
            # Leave no object behind in a thread that is about to end.
            self.moveToThread(app.thread())
