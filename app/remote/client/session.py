"""Keeping one link connected (PROTOCOL.md sections 6, 11 and 21).

``LinkRunner`` owns one daemon thread per link. The thread connects to the
host PC (last good address first, then the others, 2 s each), checks the pin,
reads ``hello``, signs in with the link's key, and then reads messages until
the connection ends. While the host is unreachable it backs off up to 30 s.

What ends the thread for good (no automatic retry, the UI tells the person):

* ``identityChanged``  the key at the last good address is not the pinned one,
  or ``hello.pcId`` is not the host's id;
* ``removed``          ``unauthorized`` on a pin-matched connection, or close
  ``4401`` / ``4403``: the host no longer accepts this PC;
* ``hostTooOld`` / ``clientTooOld``  the protocol versions do not meet.

Everything the thread learns goes out through the ``post`` callable as
``(kind, payload)``: ``("state", name)``, ``("online", {"authOk", "address"})``
and ``("message", dict)`` for every frame after login. ``LinkService`` turns
those into Qt signals; this module has no Qt. Requests to send are queued with
``send`` from any thread. Nothing here logs a key, a signature, a nonce, an
address, a URL or a pin.
"""

from __future__ import annotations

import queue
import random
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from app.native_host.host import PROTOCOL_VERSION
from app.remote.client import keys
from app.remote.client.transport import (
    Address,
    ConnectionClosed,
    IdentityMismatch,
    TransportError,
    WsConnection,
)
from app.remote.devices import auth_transcript, b64url_decode, b64url_encode, is_valid_id

MIN_HOST_PROTOCOL = 6
AUTH_TIMEOUT_S = 10.0
HELLO_TIMEOUT_S = 5.0
PING_INTERVAL_S = 20.0  # the host drops a connection that is silent for 60 s
BACKOFF_CAP_S = 30.0
FLOOD_WAIT_S = 30.0  # close 4429
CLOSED_WAIT_S = 1.0
AUTH_ID = "auth-1"

#: Link states (``LinkInfo.state``); the first four are what the thread posts.
CONNECTING = "connecting"
ONLINE = "online"
OFFLINE = "offline"
IDENTITY_CHANGED = "identityChanged"
REMOVED = "removed"
HOST_TOO_OLD = "hostTooOld"
CLIENT_TOO_OLD = "clientTooOld"
TERMINAL_STATES = frozenset({IDENTITY_CHANGED, REMOVED, HOST_TOO_OLD, CLIENT_TOO_OLD})

Post = Callable[[str, Any], None]


def backoff_seconds(failures: int, rand: Callable[[], float] = random.random) -> float:
    """Section 11: 1 s, doubling, at most 30 s, plus up to a quarter of that
    as jitter, so linked PCs do not all knock at the same moment."""
    base = min(BACKOFF_CAP_S, float(2 ** max(0, min(failures - 1, 5))))
    return base + rand() * base / 4


def candidate_addresses(last_good: str | None, known: Sequence[str]) -> list[str]:
    """Section 11: the last address that worked first, then the others."""
    ordered: list[str] = []
    for address in ([last_good] if last_good else []) + list(known):
        if address and address not in ordered:
            ordered.append(address)
    return ordered


@dataclass(frozen=True)
class LinkRecord:
    """What is stored for one link (the ``remote_links`` row)."""

    pc_id: str
    name: str
    pin: str
    device_id: str
    addresses: tuple[str, ...]
    port: int
    last_address: str | None
    key_file: str
    paired_at: int
    last_seen_at: int

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> LinkRecord:
        return cls(
            pc_id=row["pc_id"],
            name=row["name"],
            pin=row["pin"],
            device_id=row["device_id"],
            addresses=tuple(row["addresses"]),
            port=int(row["port"]),
            last_address=row["last_address"],
            key_file=row["key_file"],
            paired_at=int(row["paired_at"]),
            last_seen_at=int(row["last_seen_at"]),
        )


class _End(Exception):
    """Internal: the attempt or session ended; ``state`` says how."""

    def __init__(self, state: str, retry_after: float = 0.0) -> None:
        super().__init__(state)
        self.state = state
        self.retry_after = retry_after


class LinkRunner:
    """One link's connection thread. Create, ``start()``, and ``stop()``."""

    def __init__(
        self,
        record: LinkRecord,
        key: keys.DeviceKey,
        post: Post,
        *,
        app_version: str = "",
        client_pc_id: str = "",
        backoff: Callable[[int], float] = backoff_seconds,
        connect_timeout: float = 2.0,
    ) -> None:
        self._record = record
        self._key = key
        self._post = post
        self._app_version = app_version
        self._client_pc_id = client_pc_id
        self._backoff = backoff
        self._connect_timeout = connect_timeout
        self._addresses = list(record.addresses)
        self._last_address = record.last_address
        self._port = record.port
        self._stop = threading.Event()
        self._flush = False
        self._wake = threading.Event()
        self._out: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
        self._online = False
        self._thread = threading.Thread(target=self._run, name="grabline-link", daemon=True)

    # ------------------------------------------------------------ public API

    def start(self) -> None:
        self._thread.start()

    def stop(self, *, flush: bool = False) -> None:
        """End the thread. With ``flush`` what is queued (the ``unpair``) is
        sent first. Returns at once."""
        self._flush = flush
        self._stop.set()
        self._wake.set()

    def join(self, timeout: float = 3.0) -> None:
        if self._thread.is_alive():
            self._thread.join(timeout)

    def wake(self) -> None:
        """Try again now instead of waiting out the back-off."""
        self._wake.set()

    @property
    def online(self) -> bool:
        return self._online

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def send(self, message: dict[str, Any]) -> bool:
        """Queue one request. False when there is no login to send it on."""
        if not self._online or self._stop.is_set():
            return False
        self._out.put(message)
        return True

    # -------------------------------------------------------------- the loop

    def _run(self) -> None:
        failures = 0
        try:
            while not self._stop.is_set():
                self._post("state", CONNECTING)
                try:
                    connection = self._connect()
                except _End as end:
                    if end.state in TERMINAL_STATES:
                        self._post("state", end.state)
                        return
                    failures += 1
                    self._post("state", OFFLINE)
                    self._sleep(self._backoff(failures))
                    continue
                failures = 0
                try:
                    self._session(connection)
                except _End as end:
                    self._online = False
                    connection.close()
                    if end.state in TERMINAL_STATES:
                        self._post("state", end.state)
                        return
                    self._post("state", OFFLINE)
                    self._sleep(max(end.retry_after, CLOSED_WAIT_S))
                    continue
                self._online = False
                connection.close()
                return  # stopped
        except Exception:  # a bug here must not leave a link silently dead
            self._online = False
            self._post("state", OFFLINE)

    def _sleep(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    # ------------------------------------------------------------ connecting

    def _connect(self) -> WsConnection:
        """Open and log in, or raise ``_End``."""
        known = candidate_addresses(self._last_address, self._addresses)
        last_good_mismatch = False
        for address in known:
            if self._stop.is_set():
                raise _End(OFFLINE)
            host = address.strip("[]")
            try:
                connection = WsConnection.open(
                    Address(host, self._port),
                    pin=self._record.pin,
                    timeout=self._connect_timeout,
                )
            except IdentityMismatch:
                # Only the last good address counts: another machine at an
                # address the host used to have is just a miss (section 11).
                if address == self._last_address:
                    last_good_mismatch = True
                continue
            except TransportError:
                continue
            try:
                auth_ok = self._login(connection)
            except _End:
                connection.close()
                raise
            except Exception:
                connection.close()
                raise _End(OFFLINE) from None
            self._last_address = address
            self._learn(auth_ok)
            while not self._out.empty():  # nothing queued for an old connection is sent on this one
                self._out.get_nowait()
            self._online = True
            self._post("online", {"authOk": auth_ok, "address": address})
            return connection
        raise _End(IDENTITY_CHANGED if last_good_mismatch else OFFLINE)

    def _login(self, connection: WsConnection) -> dict[str, Any]:
        """Read ``hello`` and sign in. Returns the ``authOk`` reply."""
        hello = self._wait(connection, HELLO_TIMEOUT_S, lambda m: m.get("type") == "hello")
        version = hello.get("protocolVersion")
        if type(version) is not int or version < MIN_HOST_PROTOCOL:
            raise _End(HOST_TOO_OLD)  # nothing is sent to a host this old
        minimum = hello.get("minClientVersion")
        if type(minimum) is int and minimum > PROTOCOL_VERSION:
            raise _End(CLIENT_TOO_OLD)
        pc_id = hello.get("pcId")
        if not isinstance(pc_id, str) or not is_valid_id(pc_id):
            raise _End(OFFLINE)
        if pc_id != self._record.pc_id:
            # The same key under another PC id: do not guess, call it changed.
            raise _End(IDENTITY_CHANGED)
        try:
            nonce = b64url_decode(str(hello.get("nonce")))
            transcript = auth_transcript(nonce, self._record.pin, self._record.device_id)
        except ValueError:
            raise _End(OFFLINE) from None
        request: dict[str, Any] = {
            "id": AUTH_ID,
            "type": "auth",
            "protocolVersion": PROTOCOL_VERSION,
            "deviceId": self._record.device_id,
            "signature": b64url_encode(self._key.sign(transcript)),
            "deviceKind": "pc",
        }
        if self._client_pc_id and is_valid_id(self._client_pc_id):
            request["clientPcId"] = self._client_pc_id
        if self._app_version:
            request["appVersion"] = self._app_version[:32]
        try:
            connection.send_json(request)
        except TransportError:
            raise _End(OFFLINE) from None
        reply = self._wait(connection, AUTH_TIMEOUT_S, lambda m: m.get("id") == AUTH_ID)
        kind = reply.get("type")
        if kind == "authOk":
            return reply
        code = reply.get("code")
        if code == "unauthorized":
            raise _End(REMOVED)  # the pin matched: the real host refuses this key
        if code == "versionTooOld":
            raise _End(CLIENT_TOO_OLD)
        raise _End(OFFLINE)

    def _learn(self, auth_ok: dict[str, Any]) -> None:
        listed = auth_ok.get("addresses")
        if isinstance(listed, list):
            fresh = [a for a in listed if isinstance(a, str) and a][:8]
            if fresh:
                self._addresses = fresh
        port = auth_ok.get("port")
        if type(port) is int and 1024 <= port <= 65535:
            self._port = port

    def _wait(
        self,
        connection: WsConnection,
        timeout: float,
        wanted: Callable[[dict[str, Any]], bool],
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._stop.is_set():
                raise _End(OFFLINE)
            try:
                for message in connection.read(0.1):
                    if wanted(message):
                        return message
            except ConnectionClosed as closed:
                raise self._end_for_close(closed.code) from None
            except TransportError:
                raise _End(OFFLINE) from None
        raise _End(OFFLINE)

    @staticmethod
    def _end_for_close(code: int | None) -> _End:
        if code in (4401, 4403):
            return _End(REMOVED)
        if code == 4426:
            return _End(CLIENT_TOO_OLD)
        if code == 4429:
            return _End(OFFLINE, FLOOD_WAIT_S)
        return _End(OFFLINE)

    # --------------------------------------------------------------- session

    def _session(self, connection: WsConnection) -> None:
        """Read and write until the connection ends or the thread is stopped.
        Raises ``_End`` when the host or the network ended it; returns on stop."""
        last_ping = time.monotonic()
        while True:
            try:
                while True:
                    try:
                        message = self._out.get_nowait()
                    except queue.Empty:
                        break
                    connection.send_json(message)
                if self._stop.is_set():
                    if not self._flush or self._out.empty():
                        return
                    continue
                for message in connection.read(0.05):
                    self._post("message", message)
                now = time.monotonic()
                if now - last_ping >= PING_INTERVAL_S:
                    last_ping = now
                    connection.send_ping()
            except ConnectionClosed as closed:
                raise self._end_for_close(closed.code) from None
            except TransportError:
                raise _End(OFFLINE) from None


__all__ = [
    "AUTH_ID",
    "CLIENT_TOO_OLD",
    "CONNECTING",
    "HOST_TOO_OLD",
    "IDENTITY_CHANGED",
    "OFFLINE",
    "ONLINE",
    "REMOVED",
    "TERMINAL_STATES",
    "LinkRecord",
    "LinkRunner",
    "backoff_seconds",
    "candidate_addresses",
]
