"""The Download Info dialog: a fast, IDM-style confirmation shown when a
download starts from the browser. It carries the name, category, save location
and - for a video URL - a quality choice, with Start / Download Later / Cancel.
It opens instantly (no analysis, generic quality tiers that resolve at download
time) so it feels as quick as clicking Download in IDM.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import N_, t
from app.ui import chrome, components, design

#: Auto-sort categories (mirrors app/core/categories.py) offered in the picker.
#: These stay English - they are the value (the save-folder name and the sort
#: key), shown translated but never returned translated.
CATEGORIES = [
    N_("Video"),
    N_("Music"),
    N_("Images"),
    N_("Documents"),
    N_("Archives"),
    N_("Programs"),
    N_("Games"),
    N_("Torrents"),
]
#: Generic quality choices for a video URL - resolved at download time, so the
#: dialog needs no analysis to show them (mirrors the app's quality tiers). Only
#: "Best" is a word to translate; the format names are shown as-is.
VIDEO_QUALITIES = [N_("Best"), "1080p", "720p", "480p", "MP3", "M4A", "FLAC"]


class AddDownloadDialog(chrome.Dialog):
    def __init__(
        self,
        url: str,
        *,
        suggested_name: str,
        category: str,
        download_dir: str,
        with_quality: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("Download"))
        self.setMinimumWidth(540)
        self._base_dir = download_dir
        self._outcome: str | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(
            components.role_label(
                t("Download File Info"), "strong", size=design.FONT["h1"], bold=True
            )
        )

        form = QFormLayout()
        self._name = QLineEdit(suggested_name)
        form.addRow(t("Name"), self._name)

        url_label = components.role_label(url, "muted")
        url_label.setWordWrap(True)
        form.addRow(t("URL"), url_label)

        # Show the translated category name but carry the English value, so the
        # save folder and auto-sort key stay stable across languages.
        self._category = QComboBox()
        for cat in CATEGORIES:
            self._category.addItem(t(cat), cat)
        index = self._category.findData(category)
        if index >= 0:
            self._category.setCurrentIndex(index)
        self._category.currentIndexChanged.connect(self._category_changed)
        form.addRow(t("Category"), self._category)

        self._directory = QLineEdit(str(Path(download_dir) / str(self._category.currentData())))
        self._dir_edited = False
        self._directory.textEdited.connect(lambda _t: setattr(self, "_dir_edited", True))
        browse = QPushButton(t("Browse"))
        browse.clicked.connect(self._browse)
        save_row = QHBoxLayout()
        save_row.setContentsMargins(0, 0, 0, 0)
        save_row.addWidget(self._directory, 1)
        save_row.addWidget(browse)
        save_widget = QWidget()
        save_widget.setLayout(save_row)
        form.addRow(t("Save to"), save_widget)

        self._quality: QComboBox | None = None
        if with_quality:
            self._quality = QComboBox()
            for quality in VIDEO_QUALITIES:
                self._quality.addItem(t(quality), quality)
            form.addRow(t("Quality"), self._quality)
        layout.addLayout(form)

        self._dont_ask = QCheckBox(
            t("Start downloads immediately from now on (change in Settings)")
        )
        layout.addWidget(self._dont_ask)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton(t("Cancel"))
        cancel.clicked.connect(self.reject)
        later = QPushButton(t("Download Later"))
        later.clicked.connect(self._later)
        start = components.accent_button(t("Start Download"))
        start.setDefault(True)
        start.clicked.connect(self._start)
        for button in (cancel, later, start):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        components.cap_field_widths(self, width=380)

    # ------------------------------------------------------------ internals

    def _category_changed(self, _index: int) -> None:
        # Follow the category (its English value) with the save folder until the
        # user edits it.
        if not self._dir_edited:
            self._directory.setText(str(Path(self._base_dir) / str(self._category.currentData())))

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, t("Save to"), self._directory.text())
        if chosen:
            self._directory.setText(chosen)
            self._dir_edited = True

    def _start(self) -> None:
        self._outcome = "start"
        self.accept()

    def _later(self) -> None:
        self._outcome = "later"
        self.accept()

    # -------------------------------------------------------------- result

    def outcome(self) -> str | None:
        """ "start", "later", or None when cancelled."""
        return self._outcome

    def chosen_name(self) -> str:
        return self._name.text().strip()

    def chosen_category(self) -> str:
        return str(self._category.currentData())

    def chosen_directory(self) -> str:
        return self._directory.text().strip()

    def chosen_quality(self) -> str | None:
        return str(self._quality.currentData()) if self._quality is not None else None

    def dont_ask_again(self) -> bool:
        return self._dont_ask.isChecked()
