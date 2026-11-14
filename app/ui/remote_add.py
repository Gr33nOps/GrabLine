"""Unattended add path for links sent from a paired phone.

A phone link must never open a modal dialog on the PC: nobody is sitting at
the PC to answer it. The interactive path (``MainWindow.begin_add_url``) can
open the quality panel, "Download it again?", the low-disk warning, the plain
HTTP warning, the playlist picker, the HLS variant picker, the folder prompt
and the add-torrent dialog. This module replaces each of those decisions with
a fixed rule:

- quality: the label the phone chose, else "best";
- duplicates: an existing queued, running, paused or finished job for the same
  URL wins and nothing new is added (the phone is told "duplicate");
- low disk / plain HTTP / Safe Browsing: advisory only, so the add goes ahead
  and the engines' own free-space checks fail the job with a clear error;
- playlists: every entry up to the user's playlist batch cap, at the chosen
  quality;
- HLS: the variant matching the quality label, else the first (best);
- folder: only a folder id the PC itself offered (re-validated here), else the
  default download folder;
- torrents and magnets: queued straight away with default options.

Every outcome is reported through ``on_outcome`` so the remote service can tell
the phone what happened (queued, duplicate or failed with a plain reason).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject

from app.core.manager import AUTO_QUEUE
from app.core.models import Handoff, JobKind, JobStatus
from app.core.resolver import Resolution
from app.engines.smart import option_for_label
from app.ui import work_threads

if TYPE_CHECKING:
    from app.core.manager import DownloadManager
    from app.core.resolver import Resolver
    from app.core.settings import Settings

log = logging.getLogger(__name__)

PHONE_SOURCE = "phone"
PHONE_LINKS_SOURCE = "phone-links"
DEFAULT_QUALITY = "best"

_BLOCKING_STATUSES = (
    JobStatus.QUEUED,
    JobStatus.DOWNLOADING,
    JobStatus.PAUSED,
    JobStatus.COMPLETED,
)


@dataclass(frozen=True)
class AddOutcome:
    """What happened to one phone link."""

    request_id: str | None
    url: str
    result: str  # "queued" | "duplicate" | "failed"
    job_ids: tuple[int, ...] = ()
    message: str | None = None
    device_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


FolderResolver = Callable[[str], "Path | None"]
QueueValidator = Callable[[int], bool]


class UnattendedAdder(QObject):
    """Adds phone handoffs without any UI. Lives on the GUI thread; analysis
    runs on a ``ResolveThread`` exactly like an interactive add."""

    def __init__(
        self,
        manager: DownloadManager,
        resolver: Resolver,
        settings: Settings,
        *,
        resolve_folder: FolderResolver,
        queue_exists: QueueValidator,
        on_outcome: Callable[[AddOutcome], None],
        auto_queue: int = AUTO_QUEUE,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._manager = manager
        self._resolver = resolver
        self._settings = settings
        self._resolve_folder = resolve_folder
        self._queue_exists = queue_exists
        self._on_outcome = on_outcome
        self._auto_queue = auto_queue
        self._threads: list[work_threads.ResolveThread] = []
        #: requestId -> the phone that sent it, for the outcome report.
        self._devices: dict[str, str] = {}

    # ------------------------------------------------------------ public

    def add(self, handoff: Handoff) -> None:
        """Start adding one phone handoff. Never raises, never blocks on UI."""
        options = handoff.options if isinstance(handoff.options, dict) else {}
        request_id = _str_or_none(options.get("requestId"))
        device_id = _str_or_none(options.get("deviceId"))
        if request_id is not None and device_id is not None:
            self._devices[request_id] = device_id
        url = handoff.url.strip()
        try:
            self._add(handoff, url, options, request_id)
        except Exception as exc:  # never let a phone link break the drain loop
            log.exception("unattended add failed for a phone link")
            self._report(request_id, url, "failed", message=str(exc) or "could not add")

    def busy(self) -> bool:
        return bool(self._threads)

    def stop(self) -> None:
        for thread in list(self._threads):
            thread.wait(2000)

    # ----------------------------------------------------------- helpers

    def _add(
        self, handoff: Handoff, url: str, options: dict[str, Any], request_id: str | None
    ) -> None:
        existing = self._manager.find_existing(url)
        if existing is not None and existing.status in _BLOCKING_STATUSES:
            self._report(
                request_id,
                url,
                "duplicate",
                job_ids=(existing.id,),
                message="already in the list",
            )
            return
        quality = _quality_label(options.get("quality") or handoff.quality)
        dest = self._dest(options)
        queue_id = self._queue(options)
        if url.lower().startswith("magnet:"):
            self._add_torrent(url, dest, queue_id, request_id)
            return
        thread = work_threads.ResolveThread(
            self._resolver,
            url,
            self._settings,
            handoff.page_title,
            quality,
            (),
            None,  # never cookies or browser headers from a phone
            self,
        )

        def finished() -> None:
            if thread in self._threads:
                self._threads.remove(thread)
            thread.deleteLater()

        def resolved(
            resolution: object,
            page_title: object,
            _quality: object,
            _fallbacks: object,
            _headers: object,
        ) -> None:
            try:
                self._on_resolved(
                    resolution,  # type: ignore[arg-type]
                    page_title if isinstance(page_title, str) else None,
                    quality,
                    dest,
                    queue_id,
                    request_id,
                    url,
                )
            except Exception as exc:
                log.exception("unattended add failed after analysis")
                self._report(request_id, url, "failed", message=str(exc) or "could not add")

        thread.finished.connect(finished)
        thread.resolved.connect(resolved)
        self._threads.append(thread)
        thread.start()

    def _on_resolved(
        self,
        resolution: Resolution,
        page_title: str | None,
        quality: str,
        dest: Path | None,
        queue_id: int,
        request_id: str | None,
        url: str,
    ) -> None:
        if resolution.kind is None:
            self._report(
                request_id,
                url,
                "failed",
                message=resolution.message or "This is a web page, not a file",
            )
            return
        if resolution.kind is JobKind.TORRENT:
            self._add_torrent(resolution.url, dest, queue_id, request_id, original=url)
            return
        if resolution.kind is JobKind.CLOUD:
            self._report(
                request_id, url, "failed", message="cloud links can't be sent from a phone"
            )
            return
        dest_dir = str(dest) if dest is not None else None
        if resolution.kind is JobKind.SMART and resolution.playlist is not None:
            option = option_for_label(quality) or option_for_label(DEFAULT_QUALITY)
            if option is None:
                self._report(request_id, url, "failed", message="no quality to choose from")
                return
            cap = self._settings.playlist_batch_cap
            ids = []
            for entry in resolution.playlist.entries[:cap]:
                job = self._manager.add_smart_entry(
                    entry.url,
                    entry.title,
                    option,
                    dest_dir=dest_dir,
                    queue_id=queue_id,
                )
                ids.append(job.id)
            self._report(request_id, url, "queued", job_ids=tuple(ids))
            return
        if resolution.kind is JobKind.SMART and resolution.media is not None:
            option = option_for_label(quality, resolution.media.options) or option_for_label(
                DEFAULT_QUALITY, resolution.media.options
            )
            if option is None:
                self._report(request_id, url, "failed", message="no quality to choose from")
                return
            job = self._manager.add_smart(
                resolution.url,
                resolution.media,
                option,
                dest_dir=dest_dir,
                queue_id=queue_id,
            )
            self._report(request_id, url, "queued", job_ids=(job.id,))
            return
        if resolution.kind is JobKind.HLS:
            variant = None
            if resolution.variants:
                wanted = quality.lower()
                variant = next(
                    (v for v in resolution.variants if v.label.lower() == wanted),
                    resolution.variants[0],
                )
            job = self._manager.add_hls(
                resolution.url,
                dest_dir=dest_dir,
                title=page_title,
                variant=variant,
                queue_id=queue_id,
            )
            self._report(request_id, url, "queued", job_ids=(job.id,))
            return
        probe = resolution.probe
        from app.core import naming

        filename = (
            probe.filename
            if probe is not None and probe.filename
            else naming.improved_filename(
                resolution.url,
                page_title,
                probe.content_type if probe is not None else None,
            )
        )
        job = self._manager.add_url(
            resolution.url,
            dest_dir=dest_dir,
            filename=filename,
            probe=probe,
            queue_id=queue_id,
        )
        self._report(request_id, url, "queued", job_ids=(job.id,))

    def _add_torrent(
        self,
        source: str,
        dest: Path | None,
        queue_id: int,
        request_id: str | None,
        *,
        original: str | None = None,
    ) -> None:
        job = self._manager.add_torrent(source, dest_dir=dest, queue_id=queue_id)
        self._report(request_id, original or source, "queued", job_ids=(job.id,))

    def _dest(self, options: dict[str, Any]) -> Path | None:
        folder_id = _str_or_none(options.get("folderId"))
        if folder_id is None:
            return None
        # Re-validated at drain time: only a folder the PC itself offers.
        return self._resolve_folder(folder_id)

    def _queue(self, options: dict[str, Any]) -> int:
        queue_id = options.get("queueId")
        if (
            isinstance(queue_id, int)
            and not isinstance(queue_id, bool)
            and self._queue_exists(queue_id)
        ):
            return queue_id
        return self._auto_queue

    def _report(
        self,
        request_id: str | None,
        url: str,
        result: str,
        *,
        job_ids: tuple[int, ...] = (),
        message: str | None = None,
    ) -> None:
        device_id = self._devices.get(request_id) if request_id is not None else None
        outcome = AddOutcome(request_id, url, result, job_ids, message, device_id)
        try:
            self._on_outcome(outcome)
        except Exception:
            log.exception("reporting a phone add outcome failed")


def _str_or_none(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


_QUALITY_LABELS = frozenset(
    {"best", "2160p", "1440p", "1080p", "720p", "480p", "360p", "mp3", "m4a", "flac"}
)


def _quality_label(value: object) -> str:
    if isinstance(value, str) and value.strip().lower() in _QUALITY_LABELS:
        return value.strip().lower()
    return DEFAULT_QUALITY
