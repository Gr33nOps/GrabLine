"""Archive preview: the contents of a zip/tar/rar/7z with checkboxes, so a
user can look inside before extracting and pull out only the files they want.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.archive import ArchiveEntry
from app.core.i18n import t
from app.ui import chrome
from app.ui.format import human_bytes


class ArchiveDialog(chrome.Dialog):
    def __init__(
        self, archive_name: str, entries: tuple[ArchiveEntry, ...], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("GrabLine: {name}", name=archive_name))
        self.setMinimumSize(520, 380)
        layout = QVBoxLayout(self)

        files = [e for e in entries if not e.is_dir]
        known = [e.size for e in files if e.size is not None]
        summary = t("1 file") if len(files) == 1 else t("{count} files", count=len(files))
        if known:
            summary += f", {human_bytes(sum(known))}" + (" +" if len(known) < len(files) else "")
        layout.addWidget(QLabel(summary))

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([t("Name"), t("Size")])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 360)
        for entry in entries:
            if entry.is_dir:
                continue  # selecting a file is what matters; dirs come along
            item = QTreeWidgetItem(
                [entry.name, human_bytes(entry.size) if entry.size is not None else ""]
            )
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked)
            item.setData(0, Qt.ItemDataRole.UserRole, entry.name)
            self.tree.addTopLevelItem(item)
        layout.addWidget(self.tree)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(t("Extract"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_members(self) -> list[str] | None:
        """The checked entry names, or None when everything is checked (so the
        extractor takes its simpler extract-all path)."""
        chosen: list[str] = []
        total = self.tree.topLevelItemCount()
        for index in range(total):
            item = self.tree.topLevelItem(index)
            if item is not None and item.checkState(0) == Qt.CheckState.Checked:
                chosen.append(str(item.data(0, Qt.ItemDataRole.UserRole)))
        if len(chosen) == total:
            return None
        return chosen
