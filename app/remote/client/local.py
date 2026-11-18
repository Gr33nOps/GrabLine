"""This PC's own downloads, as it reports them to a host PC and as the host
may command them (PROTOCOL.md sections 20 and 21).

Qt-free and socket-free: it reads the ``DownloadManager`` and acts on it, on
the thread that calls it (the GUI thread). ``LinkService`` owns the schedule
and the sending.

Reports: the newest 100 downloads as section 20 phone-job objects. Names are
never paths, links have ``user:password@`` removed, and a link that is not an
http(s) or magnet link (a local torrent file, say) is reported as "" so no
path leaves this PC.

Commands: ``pause``, ``resume``, ``cancel``, ``retry``, ``remove`` and
``moveToPc``, each checked against the download's state with the same rule
that decides which buttons the Devices page offers (``PhoneJob.actions``). The
words in a refusal go to the other PC, so they are fixed English sentences.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.core.models import JobKind, JobStatus
from app.remote import schema
from app.remote.events import EventTracker
from app.remote.handlers import strip_paths, strip_userinfo
from app.remote.phonejobs import ACTIONS, PhoneJob

if TYPE_CHECKING:
    from app.core.manager import DownloadManager, JobView

MAX_REPORTED = 100
URL_LIMIT = 2048

OK = "ok"
NOT_FOUND = "notFound"
REFUSED = "refused"
FAILED = "failed"

NOT_SHARED_TEXT = "This PC does not let other PCs control its downloads"
NOT_FOUND_TEXT = "That download is no longer on this PC"
WRONG_STATE_TEXT = "That can't be done to this download right now"
UNKNOWN_ACTION_TEXT = "This PC does not know that action"
CANT_MOVE_TEXT = "This download can't be moved to another PC"
FAILED_TEXT = "This PC couldn't do that. Try again"

_KINDS = {
    JobKind.DIRECT: "direct",
    JobKind.SMART: "video",
    JobKind.HLS: "video",
    JobKind.TORRENT: "torrent",
    JobKind.CLOUD: "cloud",
}


def cut(text: str, limit: int) -> str:
    """``text`` shortened to at most ``limit`` UTF-16 code units (the protocol's
    unit), never in the middle of a character."""
    if len(text.encode("utf-16-le")) // 2 <= limit:
        return text
    out: list[str] = []
    used = 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if used + size > limit:
            break
        out.append(char)
        used += size
    return "".join(out)


def _safe_url(url: str) -> str:
    """A link that may be shown to another PC, or "" (never a local path)."""
    clean = schema.valid_url(url)
    if clean is None:
        return ""
    clean = strip_userinfo(clean)
    return clean if len(clean) <= URL_LIMIT else ""


@dataclass(frozen=True)
class Outcome:
    """What a command came to. ``changed`` means this PC's list moved, so a
    fresh report is due."""

    result: str
    message: str | None = None
    changed: bool = False


@dataclass(frozen=True)
class MovePlan:
    """``moveToPc`` is two steps: the link goes to the host with ``download``
    and only after it replied ``queued`` is the local download cancelled."""

    job_id: int
    url: str
    title: str | None


class LocalDownloads:
    def __init__(self, manager: DownloadManager) -> None:
        self._manager = manager
        self._tracker = EventTracker(lambda _job_id: None, dict)
        self._created: dict[int, int] = {}

    # --------------------------------------------------------------- reports

    def _created_at(self, job_id: int) -> int:
        cached = self._created.get(job_id)
        if cached is None:
            from app.remote.handlers import _unix

            created, _updated = self._manager.db.job_timestamps(job_id)
            cached = _unix(created) or 0
            self._created[job_id] = cached
        return cached

    def _views(self) -> list[JobView]:
        views = self._manager.snapshot()
        self._tracker.update(views, time.monotonic())
        return views

    def _phone_job(self, view: JobView) -> dict[str, Any]:
        live = self._tracker.live(view.id)
        downloading = view.status is JobStatus.DOWNLOADING
        name = cut(strip_paths(view.display_name, 256) or "download", 256)
        return {
            "id": view.id,
            "name": name,
            "url": _safe_url(view.url),
            "status": view.status.value,
            "kind": _KINDS.get(view.kind, "direct"),
            "downloaded": max(0, int(view.downloaded)),
            "total": view.total_size if view.total_size and view.total_size > 0 else None,
            "speed": live.speed if downloading else 0,
            "eta": live.eta if downloading else None,
            "createdAt": self._created_at(view.id),
            "error": (
                cut(strip_paths(view.error) or "", 512) or None
                if view.status is JobStatus.FAILED
                else None
            ),
        }

    def report(self) -> list[dict[str, Any]]:
        """The ``jobs`` of a ``phoneJobs`` request: newest first, at most 100."""
        views = self._views()
        known = {view.id for view in views}
        for gone in [job_id for job_id in self._created if job_id not in known]:
            del self._created[gone]
        newest = sorted(views, key=lambda v: v.id, reverse=True)[:MAX_REPORTED]
        return [self._phone_job(view) for view in newest]

    # -------------------------------------------------------------- commands

    def _view(self, job_id: int) -> JobView | None:
        for view in self._manager.snapshot():
            if view.id == job_id:
                return view
        return None

    def run(self, action: str, job_id: int) -> Outcome | MovePlan:
        """Check a command from the host against the download's state and do
        it. ``moveToPc`` returns a plan instead; the caller finishes it."""
        if action not in ACTIONS:
            return Outcome(REFUSED, UNKNOWN_ACTION_TEXT)
        view = self._view(job_id)
        if view is None:
            return Outcome(NOT_FOUND, NOT_FOUND_TEXT)
        mine = PhoneJob.from_fields(self._phone_job(view))
        # Remove fits any state, as the window's own Remove and ``control`` do.
        if action != "remove" and action not in mine.actions():
            return Outcome(REFUSED, WRONG_STATE_TEXT)
        try:
            if action == "moveToPc":
                return self._plan_move(view, mine)
            manager = self._manager
            if action == "pause":
                manager.pause(job_id)
            elif action in ("resume", "retry"):
                manager.resume(job_id)
            elif action == "cancel":
                manager.cancel(job_id)
            else:  # remove: finished files stay, a running job is stopped first
                manager.remove(job_id, force=True)
        except Exception:
            return Outcome(FAILED, FAILED_TEXT)
        return Outcome(OK, None, changed=True)

    def _plan_move(self, view: JobView, mine: PhoneJob) -> Outcome | MovePlan:
        url = schema.valid_url(view.url)
        if url is None or view.status is JobStatus.CANCELLED or not mine.url:
            return Outcome(REFUSED, CANT_MOVE_TEXT)
        return MovePlan(view.id, strip_userinfo(url), cut(mine.name, 512) or None)

    def finish_move(self, job_id: int) -> None:
        """The host replied ``queued``: stop the local download and delete its
        partial file. A finished file stays (nothing to cancel)."""
        job = self._manager.db.get_job(job_id)
        if job is not None and job.status in (
            JobStatus.QUEUED,
            JobStatus.DOWNLOADING,
            JobStatus.PAUSED,
            JobStatus.FAILED,
        ):
            self._manager.cancel(job_id)
