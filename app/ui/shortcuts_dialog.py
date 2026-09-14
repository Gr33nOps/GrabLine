"""The Keyboard Shortcuts cheat-sheet (F1): a searchable, read-only list of every
binding grouped by category. Rebinding lives in Settings -> Shortcuts; this is
the quick reference."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import t
from app.ui import chrome, components, theme
from app.ui.shortcuts import by_category


def _key_caps(sequence: str) -> QLabel:
    """A monospace 'keycap' label for one binding, in the native form (⌘ etc.
    on macOS). Empty binding reads as a muted 'Unbound'."""
    if sequence:
        text = QKeySequence(sequence).toString(QKeySequence.SequenceFormat.NativeText)
        role = "value"
    else:
        text = t("Unbound")
        role = "muted"
    cap = components.role_label(text, role)
    cap.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    palette = theme.current()
    font = cap.font()
    font.setFamily("monospace")
    cap.setFont(font)
    if sequence:
        cap.setStyleSheet(
            f"border: 1px solid {palette.border}; border-radius: 5px;"
            f" padding: 1px 7px; background: {palette.surface2};"
        )
    return cap


class ShortcutsDialog(chrome.Dialog):
    def __init__(self, effective: dict[str, str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("Keyboard shortcuts"))
        self.setMinimumSize(520, 560)
        layout = QVBoxLayout(self)

        self._search = QLineEdit()
        self._search.setPlaceholderText(t("Search shortcuts…"))
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._filter)
        layout.addWidget(self._search)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        column = QVBoxLayout(body)
        column.setContentsMargins(2, 4, 12, 8)
        column.setSpacing(6)

        # Each row remembers the text to match a search against, and each header
        # remembers its rows so it hides when they all filter out.
        self._rows: list[tuple[QWidget, str]] = []
        self._sections: list[tuple[QLabel, list[QWidget]]] = []
        for category, shortcuts in by_category():
            header = components.role_label(t(category), "strong", bold=True)
            column.addWidget(header)
            section_rows: list[QWidget] = []
            for shortcut in shortcuts:
                sequence = effective.get(shortcut.id, "")
                row = QWidget()
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(6, 0, 0, 0)
                row_layout.addWidget(components.role_label(t(shortcut.label), "value"), 1)
                row_layout.addWidget(_key_caps(sequence))
                column.addWidget(row)
                haystack = f"{shortcut.label} {sequence} {category}".lower()
                self._rows.append((row, haystack))
                section_rows.append(row)
            self._sections.append((header, section_rows))
            column.addSpacing(4)

        column.addStretch(1)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)

        note = components.role_label(t("Rebind any of these in Settings -> Shortcuts."), "muted")
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def _filter(self, text: str) -> None:
        needle = text.strip().lower()
        for row, haystack in self._rows:
            row.setVisible(needle in haystack)
        # Hide a category header when everything under it filtered out. isHidden()
        # reads the explicit flag we just set, not "is on screen", so this is
        # correct even before the dialog is shown.
        for header, rows in self._sections:
            header.setVisible(any(not row.isHidden() for row in rows))
