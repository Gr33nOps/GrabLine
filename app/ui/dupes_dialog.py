"""Duplicate merging: byte-identical downloads grouped, with the extra copies
pre-checked for deletion so one copy of each file survives.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import t
from app.ui import chrome
from app.ui.format import human_bytes


class DupesDialog(chrome.Dialog):
    def __init__(self, groups: list[list[Path]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("Duplicate files"))
        self.setMinimumSize(560, 380)
        layout = QVBoxLayout(self)

        extra = sum(len(group) - 1 for group in groups)
        layout.addWidget(
            QLabel(
                t(
                    "{sets} set(s) of identical files, {extra} redundant "
                    "cop(ies). Checked files will be deleted; the first of each "
                    "set is kept.",
                    sets=len(groups),
                    extra=extra,
                )
            )
        )

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([t("File"), t("Size")])
        self.tree.setColumnWidth(0, 400)
        for index, group in enumerate(groups, start=1):
            try:
                size = human_bytes(group[0].stat().st_size)
            except OSError:
                size = ""
            top = QTreeWidgetItem(
                [t("Set {index} ({count} copies)", index=index, count=len(group)), size]
            )
            top.setFlags(top.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            self.tree.addTopLevelItem(top)
            for position, path in enumerate(group):
                child = QTreeWidgetItem([str(path), ""])
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                # Keep the first (the original download), delete the rest.
                child.setCheckState(
                    0, Qt.CheckState.Unchecked if position == 0 else Qt.CheckState.Checked
                )
                child.setData(0, Qt.ItemDataRole.UserRole, str(path))
                top.addChild(child)
            top.setExpanded(True)
        layout.addWidget(self.tree)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(t("Delete checked"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_paths(self) -> list[Path]:
        doomed: list[Path] = []
        for top_index in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(top_index)
            if top is None:
                continue
            checked = [
                top.child(i)
                for i in range(top.childCount())
                if (child := top.child(i)) is not None
                and child.checkState(0) == Qt.CheckState.Checked
            ]
            # Refuse to delete a whole set - at least one copy always stays.
            if len(checked) == top.childCount():
                checked = checked[1:]
            doomed.extend(
                Path(str(child.data(0, Qt.ItemDataRole.UserRole)))
                for child in checked
                if child is not None
            )
        return doomed
