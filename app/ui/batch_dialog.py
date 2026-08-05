"""Batch import (F2.4): paste links or load a .txt, everything queues at
sensible defaults - Smart URLs at Best quality, no per-URL panels.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core import naming
from app.core.batch import expand_all, extract_urls
from app.core.i18n import t
from app.core.manager import DownloadManager
from app.core.models import JobKind
from app.core.resolver import Resolver
from app.core.settings import Settings
from app.ui import chrome, threads


class BatchImportThread(QThread):
    """Resolves and queues one URL at a time; playlists are skipped (they
    deserve the selection panel, not a silent 500-video dump)."""

    progress = Signal(int, int)  # done, total
    summary = Signal(int, object)  # queued count, list[(url, reason)]

    def __init__(self, manager: DownloadManager, settings: Settings, urls: list[str]) -> None:
        super().__init__()
        self._manager = manager
        self._settings = settings
        self._urls = urls

    def start_tracked(self) -> None:
        threads.retain(self)  # owned until finished; see app/ui/threads
        self.start()

    def run(self) -> None:
        resolver = Resolver()
        queued = 0
        skipped: list[tuple[str, str]] = []
        for index, url in enumerate(self._urls, start=1):
            if self.isInterruptionRequested():  # app quit mid-import: stop now
                break
            reason = self._queue_one(resolver, url)
            if reason is None:
                queued += 1
            else:
                skipped.append((url, reason))
            self.progress.emit(index, len(self._urls))
        self.summary.emit(queued, skipped)

    def _queue_one(self, resolver: Resolver, url: str) -> str | None:
        """Queue one URL; a string return is the reason it was skipped."""
        resolution = resolver.resolve(
            url,
            use_session=self._settings.use_browser_session,
            session_browser=self._settings.session_browser,
            proxy=self._settings.proxy,
        )
        if resolution.kind is None:
            return resolution.message or "nothing downloadable"
        if resolution.kind is JobKind.SMART:
            if resolution.playlist is not None:
                return "playlist, add it on its own to pick entries"
            assert resolution.media is not None
            if not resolution.media.options:
                return "no downloadable formats"
            self._manager.add_smart(
                url,
                resolution.media,
                resolution.media.options[0],  # Best
                use_session=self._settings.use_browser_session,
                session_browser=self._settings.session_browser,
            )
            return None
        if resolution.kind is JobKind.HLS:
            variant = resolution.variants[0] if resolution.variants else None
            self._manager.add_hls(url, variant=variant)
            return None
        probe = resolution.probe
        filename = (
            probe.filename
            if probe is not None and probe.filename
            else naming.improved_filename(
                url, None, probe.content_type if probe is not None else None
            )
        )
        self._manager.add_url(url, filename=filename, probe=probe)
        return None


class BatchImportDialog(chrome.Dialog):
    """Collects URLs; the import itself runs after accept, via the thread."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("Import links"))
        self.setMinimumSize(520, 380)

        layout = QVBoxLayout(self)
        intro = QLabel(
            t(
                "Paste anything with links in it, one per line, a page's\n"
                "text, an export file. GrabLine picks out the URLs."
            )
        )
        layout.addWidget(intro)
        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlaceholderText("https://…\nhttps://…")
        # Debounced: counting means running the URL regex over the whole box
        # and expanding every range pattern in it. On a pasted list of a few
        # thousand links, doing that per keystroke made typing unusable.
        self._count_timer = QTimer(self)
        self._count_timer.setSingleShot(True)
        self._count_timer.setInterval(150)
        self._count_timer.timeout.connect(self._update_count)
        self.text_edit.textChanged.connect(self._count_timer.start)
        layout.addWidget(self.text_edit)

        row = QHBoxLayout()
        load = QPushButton(t("Load file…"))
        load.clicked.connect(self._load_file)
        row.addWidget(load)
        row.addStretch(1)
        self._count_label = QLabel(t("{count} links", count=0))
        row.addWidget(self._count_label)
        layout.addLayout(row)

        note = QLabel(t("Videos queue at Best quality; playlists are skipped."))
        note.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update_count()

    def urls(self) -> list[str]:
        # Expand range patterns (file[1-20].jpg) after pulling URLs out.
        return expand_all(extract_urls(self.text_edit.toPlainText()))

    def _load_file(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, t("Import links from file"), "", "Text files (*.txt *.md *.csv);;All files (*)"
        )
        if path:
            try:
                content = Path(path).read_text(errors="replace")
            except OSError:
                return
            existing = self.text_edit.toPlainText()
            self.text_edit.setPlainText((existing + "\n" + content) if existing else content)

    def _update_count(self) -> None:
        count = len(self.urls())
        self._count_label.setText(t("{count} links", count=count))
        self._ok_button.setText(t("Import {count}", count=count) if count else t("Import"))
        self._ok_button.setEnabled(count > 0)
