"""Worker-thread infrastructure for the main window: the off-the-GUI-thread
resolve and file-operation threads, and the progress relay that marshals a
worker's progress back onto the GUI thread. Kept out of main_window.py so the
window file is about the window, not thread plumbing.

Lifetime note: these threads are parented (or handed to ``threads.retain``) by
their caller so a dropped Python reference can never destroy a still-running
QThread - the crash class fixed app-wide.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QObject, QThread, Signal

from app.core.errors import DownloadError
from app.core.resolver import Resolution, Resolver
from app.core.settings import Settings
from app.engines.smart import (
    cancel_download_prefetch,
    prefetch_download_ready,
    should_prefetch_download,
)

log = logging.getLogger(__name__)


class ResolveThread(QThread):
    #: Resolution, page_title (str|None), quality label (str|None, F1.3),
    #: fallback URLs (tuple[str,...]), extra HTTP headers (dict[str,str]).
    resolved = Signal(object, object, object, object, object)

    def __init__(
        self,
        resolver: Resolver,
        url: str,
        settings: Settings,
        page_title: str | None,
        quality: str | None = None,
        fallbacks: tuple[str, ...] = (),
        headers: dict[str, str] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._resolver = resolver
        self._url = url
        self._page_title = page_title
        self._quality = quality
        self._fallbacks = fallbacks
        self._headers = headers or {}
        # Analysis must stay on the fast JS-less path. Forcing cookies+runtime
        # for every YouTube URL made "Analyzing…" take tens of seconds (or fail
        # on a missing Chrome cookie DB) before the quality panel appeared.
        # SmartEngine.inspect already escalates to a browser login only when
        # the video hits an auth/bot wall. Download still attaches cookies
        # separately via manager.add_smart_entry / prefetch_download_ready.
        self._use_session = settings.use_browser_session
        self._browser = settings.session_browser
        self._proxy = settings.proxy

    def run(self) -> None:
        # Overlap YouTube's slow cookies+runtime extract with JS-less analysis
        # and the quality dialog. Confirm then reuses the prefetch instead of
        # waiting a minute after Start.
        prefetching = should_prefetch_download(self._url)
        if prefetching:
            prefetch_download_ready(
                self._url,
                proxy=self._proxy,
                session_browser=self._browser or None,
                headers=self._headers or None,
            )
        try:
            resolution = self._resolver.resolve(
                self._url,
                use_session=self._use_session,
                session_browser=self._browser,
                proxy=self._proxy,
                headers=self._headers or None,
            )
        except Exception as exc:
            # Any escape here would leave the window stuck on "Analyzing…"
            # forever with no result and no error, so report it as one.
            log.exception("resolving %s failed", self._url)
            resolution = Resolution(url=self._url, kind=None, message=str(exc))
        if prefetching and resolution.kind is None:
            cancel_download_prefetch(self._url, self._proxy)
        self.resolved.emit(
            resolution, self._page_title, self._quality, self._fallbacks, self._headers
        )


class FileOpThread(QThread):
    """Runs a slow file operation (hashing, extraction) off the UI thread."""

    done = Signal(object, object)  # result, error Exception | None

    def __init__(self, work: Callable[[], object], parent: QObject | None = None) -> None:
        # Parented: the C++ thread object's lifetime is owned by Qt, so a
        # dropped Python reference can never destroy a still-running QThread
        # (the classic hard crash).
        super().__init__(parent)
        self._work = work

    def run(self) -> None:
        try:
            result = self._work()
        except Exception as exc:
            # Every exception, not just the expected file ones: an unforeseen
            # type escaping run() means `done` never fires, so the caller's
            # completion handler never runs - the status-bar busy shimmer spins
            # for the rest of the session and one-at-a-time guards (the update
            # check) stay latched shut. The exception object itself is passed
            # so handlers can still tell PasswordRequired from a plain failure.
            if not isinstance(exc, OSError | DownloadError | ValueError):
                log.exception("background file operation failed")
            self.done.emit(None, exc)
        else:
            self.done.emit(result, None)


class ProgressRelay(QObject):
    """Marshals worker-thread progress onto the GUI thread via a signal."""

    tick = Signal(int)
    status = Signal(str)  # optional label text (e.g. "12 MB / 146 MB")
