"""Torrent dialogs: add (location, file selection, sequential/streaming) and
create (share a file or folder as a new .torrent).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import t
from app.engines.torrent import TorrentMeta
from app.ui import chrome
from app.ui.format import human_bytes


class AddTorrentDialog(chrome.Dialog):
    """Where to save, which files to take, and how to download them - shown
    for .torrent files immediately, and for magnets before metadata (no file
    list yet; libtorrent learns it from the swarm)."""

    def __init__(
        self,
        display_name: str,
        meta: TorrentMeta | None,
        default_dir: Path,
        *,
        sequential_default: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.meta = meta
        self.setWindowTitle(t("Add torrent"))
        self.setMinimumWidth(540)
        layout = QVBoxLayout(self)

        title = QLabel(display_name)
        title.setStyleSheet("font-weight: 600; font-size: 14px;")
        title.setWordWrap(True)
        layout.addWidget(title)
        if meta is not None:
            layout.addWidget(
                QLabel(
                    t(
                        "{count} file(s), {size}",
                        count=len(meta.files),
                        size=human_bytes(meta.total_size),
                    )
                )
            )

        form = QFormLayout()
        dir_row = QHBoxLayout()
        self.dir_edit = QLineEdit(str(default_dir))
        browse = QPushButton(t("Browse…"))
        browse.clicked.connect(self._browse)
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(browse)
        form.addRow(t("Save to:"), dir_row)
        layout.addLayout(form)

        self.tree: QTreeWidget | None = None
        if meta is not None and len(meta.files) > 1:
            self.tree = QTreeWidget()
            self.tree.setHeaderLabels([t("File"), t("Size")])
            self.tree.setRootIsDecorated(False)
            self.tree.setColumnWidth(0, 380)
            for entry in meta.files:
                item = QTreeWidgetItem([entry.path, human_bytes(entry.size)])
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Checked)
                item.setData(0, Qt.ItemDataRole.UserRole, entry.index)
                self.tree.addTopLevelItem(item)
            layout.addWidget(self.tree)

        self.sequential = QCheckBox(t("Sequential download (stream-friendly, in-order pieces)"))
        self.sequential.setChecked(sequential_default)
        layout.addWidget(self.sequential)
        self.first_last = QCheckBox(t("Fetch first && last pieces early (faster preview)"))
        self.first_last.setChecked(sequential_default)
        layout.addWidget(self.first_last)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(t("Download"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, t("Save torrent to"), self.dir_edit.text())
        if chosen:
            self.dir_edit.setText(chosen)

    def dest_dir(self) -> str:
        return self.dir_edit.text().strip()

    def options(self) -> dict[str, Any]:
        options: dict[str, Any] = {}
        if self.sequential.isChecked():
            options["sequential"] = True
        if self.first_last.isChecked():
            options["first_last"] = True
        if self.tree is not None and self.meta is not None:
            skipped: set[int] = set()
            for row in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(row)
                if item is not None and item.checkState(0) != Qt.CheckState.Checked:
                    skipped.add(int(item.data(0, Qt.ItemDataRole.UserRole)))
            if skipped:
                options["file_priorities"] = self.meta.priorities_for(skipped)
        return options


class CreateTorrentDialog(chrome.Dialog):
    """Torrent creation: share a file or folder; trackers, web seeds, comment
    and the private flag are optional."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("Create torrent"))
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        form = QFormLayout()

        source_row = QHBoxLayout()
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText(t("the file or folder to share"))
        pick_file = QPushButton(t("File…"))
        pick_file.clicked.connect(self._pick_file)
        pick_dir = QPushButton(t("Folder…"))
        pick_dir.clicked.connect(self._pick_dir)
        source_row.addWidget(self.source_edit, 1)
        source_row.addWidget(pick_file)
        source_row.addWidget(pick_dir)
        form.addRow(t("Share:"), source_row)

        self.trackers_edit = QPlainTextEdit()
        self.trackers_edit.setPlaceholderText(t("tracker announce URLs, one per line (optional)"))
        self.trackers_edit.setFixedHeight(64)
        form.addRow(t("Trackers:"), self.trackers_edit)
        self.webseeds_edit = QPlainTextEdit()
        self.webseeds_edit.setPlaceholderText(t("web seed URLs, one per line (optional)"))
        self.webseeds_edit.setFixedHeight(48)
        form.addRow(t("Web seeds:"), self.webseeds_edit)
        self.comment_edit = QLineEdit()
        form.addRow(t("Comment:"), self.comment_edit)
        self.private_check = QCheckBox(t("Private (no DHT/PEX, tracker-only swarms)"))
        form.addRow("", self.private_check)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(t("Create…"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _pick_file(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, t("Share a file"))
        if chosen:
            self.source_edit.setText(chosen)

    def _pick_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, t("Share a folder"))
        if chosen:
            self.source_edit.setText(chosen)

    def source(self) -> Path:
        return Path(self.source_edit.text().strip())

    def trackers(self) -> tuple[str, ...]:
        return tuple(t.strip() for t in self.trackers_edit.toPlainText().splitlines() if t.strip())

    def web_seeds(self) -> tuple[str, ...]:
        return tuple(s.strip() for s in self.webseeds_edit.toPlainText().splitlines() if s.strip())

    def comment(self) -> str:
        return self.comment_edit.text().strip()

    def private(self) -> bool:
        return self.private_check.isChecked()
