"""Desktop side of the phone end-to-end test (P1-A16). Test harness only.

Runs the real ``RemoteService`` (listener in loopback test mode, so the
Android emulator reaches it at 10.0.2.2), a real ``DownloadManager`` and the
unattended add path, against a throwaway data directory. A small local HTTP
server serves test files, one of them slowly so a job stays in progress long
enough to pause, resume and cancel it from the phone.

    python scripts/e2e/phone_peer.py STATE_JSON

It writes STATE_JSON once pairing is open: {"code", "port", "mediaBase",
"dataDir"} and keeps it updated with "events" (what the PC did). Pairing
requests are approved automatically. A download of ``<mediaBase>/revoke-me``
makes the PC remove the phone at once (QA-06), and ``<mediaBase>/quit`` ends
the peer. Never run this against a real profile: it always uses a temp dir.
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

FAST_NAME = "fast.bin"
SLOW_NAME = "slow.bin"
FAST_SIZE = 2 * 1024 * 1024
SLOW_SIZE = 8 * 1024 * 1024
SLOW_RATE = 256 * 1024  # bytes per second


def _payload(size: int, seed: int) -> bytes:
    block = bytes((i * 31 + seed) % 251 for i in range(65536))
    return (block * (size // len(block) + 1))[:size]


class _Media(http.server.BaseHTTPRequestHandler):
    files: ClassVar[dict[str, bytes]] = {}
    hits: ClassVar[list[str]] = []

    def log_message(self, *_args: object) -> None:
        return

    def do_HEAD(self) -> None:
        self._serve(head=True)

    def do_GET(self) -> None:
        self._serve(head=False)

    def _serve(self, *, head: bool) -> None:
        name = self.path.lstrip("/")
        self.hits.append(name)
        body = self.files.get(name)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        start, end = 0, len(body) - 1
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            first, _, last = rng[6:].partition("-")
            start = int(first or 0)
            end = int(last) if last else end
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
        else:
            self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("ETag", f'"{name}-v1"')
        self.end_headers()
        if head:
            return
        chunk = body[start : end + 1]
        slow = name == SLOW_NAME
        step = 16384
        for offset in range(0, len(chunk), step):
            try:
                self.wfile.write(chunk[offset : offset + step])
            except OSError:
                return
            if slow:
                time.sleep(step / SLOW_RATE)


def main() -> int:
    state_path = Path(sys.argv[1]).resolve()
    data_dir = Path(tempfile.mkdtemp(prefix="grabline-e2e-"))
    os.environ["LOCALAPPDATA"] = str(data_dir)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from app.core.manager import DownloadManager
    from app.core.resolver import Resolver
    from app.core.settings import Settings
    from app.db.database import Database
    from app.remote.service import RemoteService
    from app.ui import remote_add

    app = QApplication([])
    downloads = data_dir / "downloads"
    downloads.mkdir()
    db = Database(data_dir / "grabline.db")
    settings = Settings(db)
    settings.download_dir = downloads
    db.set_setting("remote_loopback_test", "1")
    db.set_setting("remote_port", os.environ.get("GRABLINE_E2E_PORT", "47831"))
    manager = DownloadManager(db, settings=settings)
    resolver = Resolver()

    _Media.files = {FAST_NAME: _payload(FAST_SIZE, 1), SLOW_NAME: _payload(SLOW_SIZE, 2)}
    media = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Media)
    threading.Thread(target=media.serve_forever, daemon=True).start()
    # The PC fetches these, not the phone: they must be reachable from the PC.
    media_base = f"http://127.0.0.1:{media.server_address[1]}"

    service = RemoteService(manager, settings, resolver)
    state: dict[str, object] = {"events": [], "mediaBase": media_base, "dataDir": str(data_dir)}

    def save() -> None:
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(state_path)

    def note(event: str, **extra: object) -> None:
        events = state["events"]
        assert isinstance(events, list)
        events.append({"t": round(time.time(), 3), "event": event, **extra})
        save()

    def on_outcome(outcome: remote_add.AddOutcome) -> None:
        service.add_outcome(outcome)
        note("add", result=outcome.result, url=outcome.url, jobs=list(outcome.job_ids))

    adder = remote_add.UnattendedAdder(
        manager,
        resolver,
        settings,
        resolve_folder=service.resolve_folder,
        queue_exists=lambda qid: any(q.id == qid for q in manager.list_queues()),
        on_outcome=on_outcome,
    )

    def drain() -> None:
        for handoff in db.claim_handoffs():
            name = handoff.url.rsplit("/", 1)[-1]
            note("handoff", source=handoff.source, url=handoff.url)
            if name == "revoke-me":
                for device in service.store.list():
                    service.remove_device(device.device_id)
                note("revoked")
                continue
            if name == "quit":
                note("quit")
                QTimer.singleShot(500, app.quit)
                continue
            if handoff.source == remote_add.PHONE_LINKS_SOURCE:
                for link in handoff.payload:
                    one = replace(handoff, url=link, source=remote_add.PHONE_SOURCE, payload=())
                    adder.add(one)
            elif handoff.source == remote_add.PHONE_SOURCE:
                adder.add(handoff)

    def on_started(_uri: str, manual: str, expires: int) -> None:
        _address, _, code = manual.partition("\n")
        state.update(code=code.strip(), port=service.listener.port, expires=expires)
        note("pairing-open")

    def on_pending(name: str) -> None:
        note("pairing-pending", device=name)
        service.approve_pairing()

    service.pairingStarted.connect(on_started)
    service.pairingPending.connect(on_pending)
    service.pairingFinished.connect(lambda outcome: note("pairing-finished", outcome=outcome))
    timer = QTimer()
    timer.setInterval(300)
    timer.timeout.connect(drain)
    timer.start()
    # A fresh code every 100 s so a slow emulator never meets an expired one.
    renew = QTimer()
    renew.setInterval(100_000)
    renew.timeout.connect(lambda: service.store.count() == 0 and service.start_pairing())
    renew.start()
    service.start_pairing()
    # Hard stop so a broken run never leaves a listener behind.
    QTimer.singleShot(15 * 60 * 1000, app.quit)
    code = app.exec()
    service.shutdown()
    manager.shutdown()
    media.shutdown()
    note("stopped")
    return code


if __name__ == "__main__":
    sys.exit(main())
