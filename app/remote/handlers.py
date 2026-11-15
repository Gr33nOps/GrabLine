"""Handlers for authenticated phone requests (PROTOCOL.md sections 12-14).

Runs on the GUI thread: the listener thread validates, authenticates and
rate-limits first (``app.remote.schema``), then hands each ``Request`` here
through a queued signal. Every handler returns the reply body (without ``id``
and ``protocolVersion``, which the listener adds) or raises ``RequestError``.
``analyze`` answers later through the ``respond`` callback.

Handlers never take a path from the phone: folders and queues are ids the PC
issued (``app.remote.folders``), jobs are ids from the PC's own database.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from app import __version__
from app.core import instance
from app.core.mediaurl import captured_video_page
from app.core.models import Job, JobKind, JobStatus
from app.native_host import host
from app.remote import folders
from app.remote.schema import QUALITIES, Request, RequestError, encode_cursor

if TYPE_CHECKING:
    from app.core.manager import DownloadManager
    from app.core.settings import Settings
    from app.remote.requests_log import RequestLog

log = logging.getLogger(__name__)

PHONE_SOURCE = "phone"
PHONE_LINKS_SOURCE = "phone-links"
#: Same device, same link and target, different requestId, within this
#: window: the first handoff is returned (QA-15).
DUPLICATE_WINDOW_S = 5.0

_ACTIVE = (JobStatus.QUEUED, JobStatus.DOWNLOADING, JobStatus.PAUSED)
_FILTERS: dict[str, tuple[JobStatus, ...] | None] = {
    "all": None,
    "active": _ACTIVE,
    "finished": (JobStatus.COMPLETED,),
    "failed": (JobStatus.FAILED, JobStatus.CANCELLED),
}
_FINAL = (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)
_PATHISH = re.compile(r"([A-Za-z]:[\\/]|\\\\|(?<![\w.])/(?:[^\s/]+/)+)[^\s'\"]*")


@dataclass(frozen=True)
class LiveFigures:
    speed: int = 0
    eta: int | None = None


Identity = Callable[[], tuple[str, str]]  # (pcId, pcName)
LiveLookup = Callable[[int], LiveFigures]
Responder = Callable[[dict[str, Any]], None]
Failer = Callable[[RequestError], None]


# ---------------------------------------------------------------- job object


def strip_userinfo(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if parts.username is None and parts.password is None:
        return url
    host_part = parts.hostname or ""
    if ":" in host_part:
        host_part = f"[{host_part}]"
    if parts.port:
        host_part = f"{host_part}:{parts.port}"
    return urlunsplit((parts.scheme, host_part, parts.path, parts.query, parts.fragment))


def strip_paths(text: str | None, limit: int = 512) -> str | None:
    """Plain-words error text with any filesystem path removed."""
    if not text:
        return None
    cleaned = _PATHISH.sub("…", text).strip()
    return cleaned[:limit] or None


def _unix(stamp: str | None) -> int | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace(" ", "T"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)  # SQLite datetime('now') is UTC
    return int(parsed.timestamp())


def job_object(
    job: Job,
    downloaded: int,
    created: str | None,
    updated: str | None,
    live: LiveFigures,
    *,
    request_id: str | None,
) -> dict[str, Any]:
    downloading = job.status is JobStatus.DOWNLOADING
    name = job.title or job.filename or ""
    return {
        "id": job.id,
        "url": strip_userinfo(job.url),
        "name": strip_paths(name, 512) or "",
        "status": job.status.value,
        "kind": job.kind.value if isinstance(job.kind, JobKind) else str(job.kind),
        "downloaded": max(0, int(downloaded)),
        "total": job.total_size if job.total_size and job.total_size > 0 else None,
        "speed": live.speed if downloading else 0,
        "eta": live.eta if downloading else None,
        "queueId": job.queue_id,
        "createdAt": _unix(created) or 0,
        "finishedAt": _unix(updated) if job.status in _FINAL else None,
        "error": strip_paths(job.error) if job.status is JobStatus.FAILED else None,
        "requestId": request_id,
        "fetchable": False,
    }


# ------------------------------------------------------------------ handlers


class RemoteHandlers:
    """One instance per app; stateless apart from the request log."""

    def __init__(
        self,
        manager: DownloadManager,
        settings: Settings,
        request_log: RequestLog,
        *,
        identity: Identity,
        live: LiveLookup,
        analyze: Callable[[str, Responder, Failer], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._manager = manager
        self._db = manager.db
        self._settings = settings
        self._log = request_log
        self._identity = identity
        self._live = live
        self._analyze = analyze
        self._clock = clock
        self._recent: dict[tuple[str, str, str | None, int | None, str | None], tuple[float, int]]
        self._recent = {}

    # ------------------------------------------------------------ dispatch

    def handle(self, device_id: str, request: Request, respond: Responder, fail: Failer) -> None:
        """Answer one validated request; exactly one of respond/fail is called."""
        try:
            if request.type == "analyze":
                self._do_analyze(request.fields, respond, fail)
                return
            method = getattr(self, "_h_" + request.type, None)
            if method is None:
                raise RequestError("unsupported", "unknown type")
            respond(method(device_id, request.fields))
        except RequestError as exc:
            fail(exc)
        except Exception:
            log.exception("remote handler failed for %s", request.type)
            fail(RequestError("internal", "internal error"))

    # --------------------------------------------------------------- basics

    def _h_ping(self, _device: str, _f: dict[str, Any]) -> dict[str, Any]:
        pc_id, pc_name = self._identity()
        return {
            "type": "pong",
            "appRunning": True,
            "pcId": pc_id,
            "pcName": pc_name,
            "appVersion": __version__,
            "serverTime": int(self._clock()),
        }

    def _h_status(self, _device: str, f: dict[str, Any]) -> dict[str, Any]:
        reply = host.handle_message(self._db, {"type": "status", "urls": f["urls"]})
        reply["appRunning"] = True
        for item in reply.get("jobs", []):
            if isinstance(item.get("name"), str):
                item["name"] = strip_paths(item["name"]) or ""
        return reply

    def _h_focus(self, _device: str, f: dict[str, Any]) -> dict[str, Any]:
        message: dict[str, Any] = {"type": "focus"}
        if f.get("target"):
            message["target"] = f["target"]
        host.handle_message(self._db, message)
        return {"type": "ok", "appRunning": True}

    def _h_recent(self, device: str, f: dict[str, Any]) -> dict[str, Any]:
        jobs = [self.job(job, device) for job in self._db.recent_jobs(f["limit"])]
        return {"type": "recent", "jobs": jobs, "appRunning": True}

    # ------------------------------------------------------------ adding

    def _target(self, f: dict[str, Any]) -> tuple[int | None, str | None]:
        queue_id = f.get("queueId")
        if queue_id is not None and not any(q.id == queue_id for q in self._manager.list_queues()):
            raise RequestError("notFound", "no such queue")
        folder_id = f.get("folderId")
        if (
            folder_id is not None
            and folders.resolve_folder(self._settings, self._db, folder_id) is None
        ):
            raise RequestError("notFound", "no such folder")
        return queue_id, folder_id

    def _h_download(self, device: str, f: dict[str, Any]) -> dict[str, Any]:
        queue_id, folder_id = self._target(f)
        request_id: str = f["requestId"]
        url: str = f["url"]
        stored = captured_video_page(url, f.get("pageUrl"))
        previous = self._db.latest_job_for_url(stored)
        base = {
            "type": "queued",
            "handoffUrl": stored,
            "previousJobId": previous.id if previous else 0,
            "appRunning": True,
            "requestId": request_id,
            "jobId": None,
        }
        earlier = self._log.lookup(device, request_id)
        if earlier is not None:
            return {**base, "handoffId": earlier, "duplicate": True}
        key = (device, stored, f["quality"], queue_id, folder_id)
        now = self._clock()
        self._recent = {k: v for k, v in self._recent.items() if now - v[0] < DUPLICATE_WINDOW_S}
        if key in self._recent:
            handoff_id = self._recent[key][1]
            self._log.remember(device, request_id, handoff_id)
            return {**base, "handoffId": handoff_id, "duplicate": True}
        handoff_id = self._db.add_handoff(
            stored,
            page_url=f.get("pageUrl"),
            page_title=f.get("title"),
            source=PHONE_SOURCE,
            quality=f["quality"],
            options={
                "requestId": request_id,
                "deviceId": device,
                "queueId": queue_id,
                "folderId": folder_id,
                "title": f.get("title"),
                "quality": f["quality"],
                "unattended": True,
            },
        )
        self._log.remember(device, request_id, handoff_id)
        self._recent[key] = (now, handoff_id)
        return {**base, "handoffId": handoff_id, "duplicate": False}

    def _h_links(self, device: str, f: dict[str, Any]) -> dict[str, Any]:
        queue_id, folder_id = self._target(f)
        request_id: str = f["requestId"]
        urls: list[str] = f["urls"]
        base = {
            "type": "queued",
            "count": len(urls),
            "appRunning": True,
            "requestId": request_id,
        }
        earlier = self._log.lookup(device, request_id)
        if earlier is not None:
            return {**base, "handoffId": earlier, "duplicate": True}
        handoff_id = self._db.add_handoff(
            f.get("pageUrl") or urls[0],
            page_url=f.get("pageUrl"),
            page_title=f.get("pageTitle"),
            source=PHONE_LINKS_SOURCE,
            quality=f["quality"],
            payload=urls,
            options={
                "requestId": request_id,
                "deviceId": device,
                "queueId": queue_id,
                "folderId": folder_id,
                "quality": f["quality"],
                "unattended": True,
            },
        )
        self._log.remember(device, request_id, handoff_id)
        return {**base, "handoffId": handoff_id, "duplicate": False}

    # ------------------------------------------------------------ job list

    def job(self, job: Job, device: str | None) -> dict[str, Any]:
        created, updated = self._db.job_timestamps(job.id)
        return job_object(
            job,
            self._db.stored_progress(job),
            created,
            updated,
            self._live(job.id),
            request_id=self._log.request_for_job(job.id, device),
        )

    def _h_jobs(self, device: str, f: dict[str, Any]) -> dict[str, Any]:
        statuses = _FILTERS[f["filter"]]
        matching = [
            job for job in self._db.list_jobs() if statuses is None or job.status in statuses
        ]
        matching.sort(key=lambda j: j.id, reverse=True)
        after = f.get("after")
        page_source = [j for j in matching if after is None or j.id < after]
        page = page_source[: f["limit"]]
        more = len(page_source) > len(page)
        return {
            "type": "jobs",
            "jobs": [self.job(job, device) for job in page],
            "nextCursor": encode_cursor(page[-1].id) if (page and more) else None,
            "total": len(matching),
        }

    # -------------------------------------------------------------- control

    def _h_control(self, device: str, f: dict[str, Any]) -> dict[str, Any]:
        job_id: int = f["jobId"]
        action: str = f["action"]
        job = self._db.get_job(job_id)
        if job is None:
            raise RequestError("notFound", "no such job")
        status = job.status
        conflict = RequestError("conflict", "not possible in this state")
        if action == "pause":
            if status is JobStatus.PAUSED:
                pass
            elif status in (JobStatus.QUEUED, JobStatus.DOWNLOADING):
                self._manager.pause(job_id)
            else:
                raise conflict
        elif action == "resume":
            if status in (JobStatus.QUEUED, JobStatus.DOWNLOADING):
                pass
            elif status is JobStatus.PAUSED:
                self._manager.resume(job_id)
            else:
                raise conflict
        elif action == "cancel":
            if status is JobStatus.CANCELLED:
                pass
            elif status in _ACTIVE:
                self._manager.cancel(job_id)
            else:
                raise conflict
        elif action == "retry":
            if status in (JobStatus.QUEUED, JobStatus.DOWNLOADING):
                pass
            elif status in (JobStatus.FAILED, JobStatus.CANCELLED):
                self._manager.resume(job_id)
            else:
                raise conflict
        elif action == "remove":
            # As the desktop's Remove: finished files stay; running jobs stop.
            self._manager.remove(job_id, force=True)
            return {"type": "ok", "job": None}
        after = self._db.get_job(job_id)
        return {"type": "ok", "job": self.job(after, device) if after else None}

    # --------------------------------------------------------------- global

    def global_state(self) -> dict[str, Any]:
        jobs = self._db.list_jobs()
        active = sum(1 for j in jobs if j.status is JobStatus.DOWNLOADING)
        queued = sum(1 for j in jobs if j.status is JobStatus.QUEUED)
        paused = sum(1 for j in jobs if j.status is JobStatus.PAUSED)
        speed = sum(self._live(j.id).speed for j in jobs if j.status is JobStatus.DOWNLOADING)
        kbps = self._settings.speed_limit_kbps
        return {
            "paused": active == 0 and queued == 0 and paused > 0,
            "speedLimit": kbps * 1024 if kbps > 0 else None,
            "speed": int(speed),
            "active": active,
            "queued": queued,
        }

    def _h_global(self, _device: str, f: dict[str, Any]) -> dict[str, Any]:
        action = f["action"]
        if action == "pauseAll":
            for job in self._db.list_jobs():
                if job.status in (JobStatus.DOWNLOADING, JobStatus.QUEUED):
                    self._manager.pause(job.id)
        elif action == "resumeAll":
            for job in self._db.list_jobs():
                if job.status is JobStatus.PAUSED:
                    self._manager.resume(job.id)
        elif action == "setSpeedLimit":
            self._settings.speed_limit_kbps = max(1, int(f["bytesPerSecond"]) // 1024)
            self._manager.reload_settings()
        elif action == "clearSpeedLimit":
            self._settings.speed_limit_kbps = 0
            self._manager.reload_settings()
        return {"type": "global", "state": self.global_state()}

    # --------------------------------------------------------------- queues

    def _h_subscribe(self, _device: str, _f: dict[str, Any]) -> dict[str, Any]:
        # The listener keeps the topic set and fills in ``topics``.
        return {"type": "subscribed", "state": self.global_state()}

    def _h_queues(self, _device: str, _f: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "queues",
            "queues": [
                {"id": q.id, "name": strip_paths(q.name, 64) or "", "paused": bool(q.paused)}
                for q in self._manager.list_queues()
            ],
            "folders": [
                {"id": folder.id, "name": folder.name, "isDefault": folder.is_default}
                for folder in folders.list_folders(self._settings, self._db)
            ],
        }

    # -------------------------------------------------------------- analyze

    def _do_analyze(self, f: dict[str, Any], respond: Responder, fail: Failer) -> None:
        if self._analyze is None:
            raise RequestError("unsupported", "analyze not available")
        self._analyze(f["url"], respond, fail)


def analysis_reply(url: str, resolution: Any) -> dict[str, Any]:
    """Map a resolver ``Resolution`` to the ``analysis`` reply (or raise)."""
    if resolution.kind is None:
        message = (resolution.message or "").lower()
        reason = "drm" if "drm" in message else "noMedia"
        raise RequestError("failed", "analysis failed", reason=reason)
    media = resolution.media
    playlist = resolution.playlist
    qualities: list[dict[str, Any]] = []
    title: str | None = None
    duration: int | None = None
    if media is not None:
        title = media.title
        duration = int(media.duration) if media.duration else None
        for option in media.options:
            label = option.label.strip().lower()
            if label not in QUALITIES or any(q["label"] == label for q in qualities):
                continue
            qualities.append(
                {
                    "label": label,
                    "kind": option.kind if option.kind in ("video", "audio") else "video",
                    "height": option.height,
                    "ext": option.audio_format or ("mp4" if option.kind == "video" else "m4a"),
                    "sizeBytes": option.estimated_size,
                }
            )
            if len(qualities) >= 20:
                break
    elif playlist is not None:
        title = playlist.title
    return {
        "type": "analysis",
        "url": url,
        "title": strip_paths(title, 512) if title else None,
        "duration": duration,
        "isPlaylist": playlist is not None,
        "count": len(playlist.entries) if playlist is not None else 1,
        "qualities": qualities,
    }


def app_running() -> bool:
    return instance.app_is_running()
