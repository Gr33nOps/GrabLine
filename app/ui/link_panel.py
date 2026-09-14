"""Link picker: the downloadable links the extension found on a page, as a
checkable list with a text filter and quick type filters. Chosen links go
through the same batch-import path as pasted links, so smart URLs still get
their engine."""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import N_, t
from app.ui import chrome

#: Quick filters: label -> file extensions it selects.
_TYPE_FILTERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (N_("Video"), (".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v")),
    (N_("Audio"), (".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".aac")),
    (N_("Images"), (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg")),
    (N_("Archives"), (".zip", ".rar", ".7z", ".tar", ".gz", ".xz", ".iso")),
    (N_("Documents"), (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".epub")),
)


def _short(url: str) -> str:
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or url
    return name if len(name) <= 60 else name[:57] + "…"


class LinkPanel(chrome.Dialog):
    def __init__(
        self, urls: list[str], *, page_title: str | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.urls = urls
        self.setWindowTitle(t("Links on this page"))
        self.setMinimumSize(560, 460)

        layout = QVBoxLayout(self)
        title = QLabel(
            t("{page}: {count} links", page=page_title or t("This page"), count=len(urls))
        )
        title.setWordWrap(True)
        title.setStyleSheet("font-weight: 600; font-size: 14px;")
        layout.addWidget(title)

        self.filter_box = QLineEdit()
        self.filter_box.setPlaceholderText(t("Filter by text…"))
        self.filter_box.setClearButtonEnabled(True)
        self.filter_box.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter_box)

        types_row = QHBoxLayout()
        types_row.addWidget(QLabel(t("Select:")))
        for label, exts in _TYPE_FILTERS:
            button = QPushButton(t(label))
            button.clicked.connect(lambda _c=False, e=exts: self._select_by_ext(e))
            types_row.addWidget(button)
        types_row.addStretch(1)
        layout.addLayout(types_row)

        select_row = QHBoxLayout()
        all_button = QPushButton(t("All visible"))
        none_button = QPushButton(t("None"))
        all_button.clicked.connect(lambda: self._set_visible(Qt.CheckState.Checked))
        none_button.clicked.connect(lambda: self._set_all(Qt.CheckState.Unchecked))
        select_row.addWidget(all_button)
        select_row.addWidget(none_button)
        select_row.addStretch(1)
        self._count_label = QLabel("")
        select_row.addWidget(self._count_label)
        layout.addLayout(select_row)

        self.list = QListWidget()
        for url in urls:
            item = QListWidgetItem(_short(url))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setToolTip(url)
            self.list.addItem(item)
        self.list.itemChanged.connect(lambda _i: self._update_count())
        layout.addWidget(self.list)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update_count()

    # -------------------------------------------------------------- result

    def selected_urls(self) -> list[str]:
        return [
            url
            for row, url in enumerate(self.urls)
            if self.list.item(row).checkState() is Qt.CheckState.Checked
        ]

    # ------------------------------------------------------------ internals

    def _apply_filter(self) -> None:
        needle = self.filter_box.text().strip().lower()
        for row in range(self.list.count()):
            item = self.list.item(row)
            item.setHidden(bool(needle) and needle not in self.urls[row].lower())

    def _select_by_ext(self, exts: tuple[str, ...]) -> None:
        for row in range(self.list.count()):
            path = urlsplit(self.urls[row]).path.lower()
            if any(path.endswith(ext) for ext in exts):
                self.list.item(row).setCheckState(Qt.CheckState.Checked)

    def _set_all(self, state: Qt.CheckState) -> None:
        for row in range(self.list.count()):
            self.list.item(row).setCheckState(state)

    def _set_visible(self, state: Qt.CheckState) -> None:
        for row in range(self.list.count()):
            item = self.list.item(row)
            if not item.isHidden():
                item.setCheckState(state)

    def _update_count(self) -> None:
        count = len(self.selected_urls())
        self._count_label.setText(t("{count} selected", count=count))
        self._ok_button.setText(t("Download {count}", count=count) if count else t("Download"))
        self._ok_button.setEnabled(count > 0)
