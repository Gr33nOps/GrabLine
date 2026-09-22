"""Bulk torrent import: one list, one destination, one button.

The single-torrent dialog asks where to save, which files to take and how to
fetch them. Asking that a hundred times is the problem this screen exists to
remove: pick the torrents, pick one folder, press Download All. Everything
per-torrent that the single dialog offers is still reachable afterwards from
the download's own detail panel - what is *not* offered is a hundred modal
dialogs.

Reading the torrents happens on a worker thread and the list is filled in one
go, so dropping a folder of several hundred .torrent files leaves the window
responsive and shows a progress bar while it works.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core import torrentbatch
from app.core.i18n import t
from app.core.torrentbatch import TorrentCandidate
from app.ui import chrome, design, threads
from app.ui.format import human_bytes


class TorrentScanThread(QThread):
    """Reads sources into candidates off the GUI thread.

    Parsing a .torrent is cheap; parsing four hundred of them from a slow disk
    is not, and an http(s) .torrent URL in the list is a network round trip.
    Neither belongs on the thread painting the dialog.
    """

    progress = Signal(int, int)  # done, total
    scanned = Signal(object)  # list[TorrentCandidate]

    def __init__(
        self, sources: Sequence[str], *, proxy: str | None = None, insecure: bool = False
    ) -> None:
        super().__init__()
        self._sources = list(sources)
        self._proxy = proxy
        self._insecure = insecure

    def start_tracked(self) -> None:
        threads.retain(self)  # owned until finished; see app/ui/threads
        self.start()

    def run(self) -> None:
        found: list[TorrentCandidate] = []
        total = len(self._sources)
        for index, source in enumerate(self._sources, start=1):
            if self.isInterruptionRequested():  # the dialog closed mid-scan
                break
            found.append(
                torrentbatch.load_candidate(source, proxy=self._proxy, insecure=self._insecure)
            )
            self.progress.emit(index, total)
        self.scanned.emit(found)


class MagnetPasteDialog(chrome.Dialog):
    """A box for magnet links, one per line."""

    def __init__(self, parent: QWidget | None = None) -> None:
        from PySide6.QtWidgets import QPlainTextEdit

        super().__init__(parent)
        self.setWindowTitle(t("Paste magnet links"))
        self.setMinimumSize(520, 320)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(t("One magnet link per line. Anything else is ignored.")))
        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlaceholderText("magnet:?xt=urn:btih:…\nmagnet:?xt=urn:btih:…")
        layout.addWidget(self.text_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def magnets(self) -> list[str]:
        return torrentbatch.magnet_links(self.text_edit.toPlainText())


class TorrentBatchDialog(chrome.Dialog):
    """The batch list, the shared destination, and Download All."""

    _STATUS_COLUMN = 3

    def __init__(
        self,
        *,
        default_dir: Path,
        queues: Sequence[tuple[int | None, str]] | None = None,
        subfolders: bool = False,
        remember: bool = True,
        sequential: bool = False,
        proxy: str | None = None,
        insecure: bool = False,
        existing_keys: Sequence[str] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("Add torrents"))
        self.setMinimumSize(720, 520)
        self.setAcceptDrops(True)
        self._proxy = proxy
        self._insecure = insecure
        self._existing = set(existing_keys)
        self._candidates: list[TorrentCandidate] = []
        self._scan: TorrentScanThread | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                t(
                    "Add as many torrents as you like, choose one folder for all "
                    "of them, and start the lot in one go."
                )
            )
        )

        source_row = QHBoxLayout()
        for label, handler in (
            (t("Add files…"), self._add_files),
            (t("Add folder…"), self._add_folder),
            (t("Paste magnets…"), self._paste_magnets),
        ):
            button = QPushButton(label)
            button.clicked.connect(handler)
            source_row.addWidget(button)
        source_row.addStretch(1)
        self._remove_button = QPushButton(t("Remove selected"))
        self._remove_button.clicked.connect(self._remove_selected)
        source_row.addWidget(self._remove_button)
        layout.addLayout(source_row)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([t("Torrent"), t("Size"), t("Files"), t("Status")])
        self.tree.setRootIsDecorated(False)
        # Hundreds of rows: a uniform row height lets Qt skip measuring every
        # one of them, which is the difference between an instant list and a
        # visible stall when a folder of 500 torrents lands here.
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.tree, 1)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        dest_row = QHBoxLayout()
        dest_row.addWidget(QLabel(t("Save all to:")))
        self.dir_edit = QLineEdit(str(default_dir))
        self.dir_edit.textChanged.connect(self._refresh_counts)
        browse = QPushButton(t("Browse…"))
        browse.clicked.connect(self._browse)
        dest_row.addWidget(self.dir_edit, 1)
        dest_row.addWidget(browse)
        layout.addLayout(dest_row)

        options_row = QHBoxLayout()
        self.subfolder_check = QCheckBox(t("Give each torrent its own folder"))
        self.subfolder_check.setChecked(subfolders)
        self.remember_check = QCheckBox(t("Remember this folder"))
        self.remember_check.setChecked(remember)
        self.sequential_check = QCheckBox(t("Sequential (stream-friendly)"))
        self.sequential_check.setChecked(sequential)
        options_row.addWidget(self.subfolder_check)
        options_row.addWidget(self.remember_check)
        options_row.addWidget(self.sequential_check)
        options_row.addStretch(1)
        options_row.addWidget(QLabel(t("Queue:")))
        self._queue = QComboBox()
        for queue_id, name in queues if queues is not None else [(None, t("Default"))]:
            self._queue.addItem(name, queue_id)
        options_row.addWidget(self._queue)
        layout.addLayout(options_row)

        self._warning = QLabel("")
        self._warning.setWordWrap(True)
        self._warning.setProperty("role", "muted")
        layout.addWidget(self._warning)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_button.setProperty("accent", "true")
        buttons.accepted.connect(self._confirm)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh_counts()

    # -------------------------------------------------------------- sources

    def _add_files(self) -> None:
        chosen, _filter = QFileDialog.getOpenFileNames(
            self, t("Add torrent files"), "", "Torrents (*.torrent);;All files (*)"
        )
        if chosen:
            self.add_sources(chosen)

    def _add_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, t("Add every torrent in a folder"))
        if chosen:
            self.add_sources([chosen])

    def _paste_magnets(self) -> None:
        dialog = MagnetPasteDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        magnets = dialog.magnets()
        if not magnets:
            QMessageBox.information(self, "GrabLine", t("No magnet links in that text."))
            return
        self.add_sources(magnets)

    def add_sources(self, raw: Sequence[str]) -> None:
        """Expand, read and append sources. Folders expand to the .torrent
        files inside them; anything that is not a torrent source is counted
        and reported rather than silently dropped."""
        if self._scan is not None and self._scan.isRunning():
            return  # a scan is already in flight; the buttons come back after
        sources, ignored = torrentbatch.expand_sources(raw)
        known = {c.source for c in self._candidates}
        sources = [s for s in sources if s not in known]
        if ignored:
            self._warning.setText(
                t("{count} item(s) were not torrents and were skipped.", count=len(ignored))
            )
        if not sources:
            self._refresh_counts()
            return
        self._set_busy(True, len(sources))
        scan = TorrentScanThread(sources, proxy=self._proxy, insecure=self._insecure)
        scan.progress.connect(self._on_progress)
        scan.scanned.connect(self._on_scanned)
        self._scan = scan
        scan.start_tracked()

    def _set_busy(self, busy: bool, total: int = 0) -> None:
        self._progress.setVisible(busy)
        self._progress.setRange(0, max(1, total))
        self._progress.setValue(0)
        self._ok_button.setEnabled(not busy)

    def _on_progress(self, done: int, total: int) -> None:
        self._progress.setRange(0, max(1, total))
        self._progress.setValue(done)

    def _on_scanned(self, result: object) -> None:
        self._scan = None
        self._set_busy(False)
        if isinstance(result, list):
            self._candidates.extend(result)
        self._rebuild_rows()

    # ----------------------------------------------------------------- list

    def _rebuild_rows(self) -> None:
        """Re-mark duplicates and repaint the list in one pass.

        Rebuilt wholesale rather than patched: removing a row can promote the
        duplicate that followed it into the one that gets downloaded, so the
        marks are only ever correct for the list as a whole.
        """
        self._candidates = torrentbatch.mark_duplicates(self._candidates, self._existing)
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        numbers = design.numeric_font(self.tree.font())
        self.tree.setUpdatesEnabled(False)
        try:
            self.tree.clear()
            items = []
            for candidate in self._candidates:
                item = QTreeWidgetItem(
                    [
                        candidate.name,
                        human_bytes(candidate.total_size) if candidate.total_size else "—",
                        str(candidate.file_count) if candidate.file_count else "—",
                        self._status_text(candidate),
                    ]
                )
                item.setData(0, Qt.ItemDataRole.UserRole, candidate.source)
                item.setToolTip(0, candidate.source)
                for column in (1, 2):  # numbers: right-aligned, tabular digits
                    item.setTextAlignment(column, right)
                    item.setFont(column, numbers)
                if not candidate.ok:
                    item.setToolTip(self._STATUS_COLUMN, candidate.error or candidate.duplicate)
                items.append(item)
            self.tree.addTopLevelItems(items)
        finally:
            self.tree.setUpdatesEnabled(True)
        self._refresh_counts()

    @staticmethod
    def _status_text(candidate: TorrentCandidate) -> str:
        if candidate.error:
            return t("Unreadable")
        if candidate.duplicate == torrentbatch.DUPLICATE_IN_BATCH:
            return t("Duplicate")
        if candidate.duplicate == torrentbatch.DUPLICATE_QUEUED:
            return t("Already queued")
        if not candidate.info_hash and torrentbatch.is_magnet(candidate.source):
            return t("Magnet")
        return t("Ready")

    def _remove_selected(self) -> None:
        doomed = {item.data(0, Qt.ItemDataRole.UserRole) for item in self.tree.selectedItems()}
        if not doomed:
            return
        self._candidates = [c for c in self._candidates if c.source not in doomed]
        self._rebuild_rows()

    def keyPressEvent(self, event: object) -> None:
        key = getattr(event, "key", lambda: None)()
        if key == Qt.Key.Key_Delete and self.tree.hasFocus():
            self._remove_selected()
            return
        super().keyPressEvent(event)  # type: ignore[arg-type]

    def _refresh_counts(self) -> None:
        ready = sum(1 for c in self._candidates if c.ok)
        duplicates = sum(1 for c in self._candidates if c.duplicate)
        broken = sum(1 for c in self._candidates if c.error)
        self._ok_button.setText(
            t("Download All ({count})", count=ready) if ready else t("Download All")
        )
        self._ok_button.setEnabled(ready > 0 and self._scan is None)
        self._remove_button.setEnabled(bool(self._candidates))
        notes = []
        if duplicates:
            notes.append(t("{count} duplicate(s) will be skipped.", count=duplicates))
        if broken:
            notes.append(t("{count} file(s) could not be read.", count=broken))
        if notes:
            self._warning.setText("  ".join(notes))
        elif self._candidates:
            self._warning.setText("")

    # ------------------------------------------------------------- finished

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, t("Save every torrent to"), self.dir_edit.text()
        )
        if chosen:
            self.dir_edit.setText(chosen)

    def _confirm(self) -> None:
        """Validate the destination before accepting, so a mistyped folder is
        one message here rather than a hundred failed downloads later."""
        try:
            base = torrentbatch.validate_destination(self.dir_edit.text())
            torrentbatch.ensure_destination(base)
        except torrentbatch.DestinationError as exc:
            QMessageBox.warning(self, "GrabLine", str(exc))
            return
        if not any(c.ok for c in self._candidates):
            QMessageBox.information(self, "GrabLine", t("Nothing in the list can be added."))
            return
        self.accept()

    def selected_candidates(self) -> list[TorrentCandidate]:
        """The queueable candidates, each already carrying its destination."""
        base = torrentbatch.validate_destination(self.dir_edit.text())
        ready = [c for c in self._candidates if c.ok]
        return torrentbatch.assign_destinations(
            ready, base, subfolders=self.subfolder_check.isChecked()
        )

    def skipped_counts(self) -> tuple[int, int]:
        """(duplicates, unreadable) - the two halves of the closing summary."""
        return (
            sum(1 for c in self._candidates if c.duplicate),
            sum(1 for c in self._candidates if c.error),
        )

    def destination(self) -> str:
        return self.dir_edit.text().strip()

    def chosen_queue(self) -> int | None:
        data = self._queue.currentData()
        return int(data) if data is not None else None

    def remember_destination(self) -> bool:
        return self.remember_check.isChecked()

    def use_subfolders(self) -> bool:
        return self.subfolder_check.isChecked()

    def shared_options(self) -> dict[str, object]:
        """The job options every torrent in this batch is queued with."""
        options: dict[str, object] = {}
        if self.sequential_check.isChecked():
            options["sequential"] = True
        return options

    # --------------------------------------------------------- drag & drop

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        data = event.mimeData()
        if data.hasUrls() or data.hasText():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        data = event.mimeData()
        dropped = [url.toLocalFile() or url.toString() for url in data.urls()]
        if not dropped and data.hasText():
            dropped = torrentbatch.magnet_links(data.text())
        if not dropped:
            return
        event.acceptProposedAction()
        self.add_sources(dropped)

    def closeEvent(self, event: object) -> None:
        if self._scan is not None:
            self._scan.requestInterruption()  # retained; it finishes on its own
        super().closeEvent(event)  # type: ignore[arg-type]
