"""The Download Info dialog: a fast, IDM-style confirmation shown when a
download starts from the browser. It carries the name, category, save location
and - for a video URL - a quality choice, with Start / Download Later / Cancel.
It opens instantly (no analysis, generic quality tiers that resolve at download
time) so it feels as quick as clicking Download in IDM.
"""

from __future__ import annotations

from collections.abc import Sequence
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

#: What the "no named queue" choice is called in the Queue picker. Picking it
#: is an explicit choice (manager.AUTO_QUEUE is what "nothing was chosen"
#: means), so a download sent here is never re-routed by the category rules.
DEFAULT_QUEUE_LABEL = N_("Default")


def queue_choices(manager: object) -> list[tuple[int | None, str]]:
    """(queue id, name) pairs for the picker: Default first, then the user's
    own queues in their Queue Manager order. Takes the manager duck-typed so
    the dialog keeps no import edge into the core package."""
    listed = getattr(manager, "list_queues", None)
    queues = list(listed()) if callable(listed) else []
    return [(None, t(DEFAULT_QUEUE_LABEL)), *((q.id, q.name) for q in queues)]


class AddDownloadDialog(chrome.Dialog):
    def __init__(
        self,
        url: str,
        *,
        suggested_name: str,
        category: str,
        download_dir: str,
        with_quality: bool = False,
        queues: Sequence[tuple[int | None, str]] | None = None,
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

        # Which queue this download joins. Always shown - with no custom queues
        # it is simply "Default", which is what the scheduler already does -
        # so there is one consistent place to answer "where does this go?".
        self._queue = QComboBox()
        for queue_id, name in queues if queues is not None else [(None, t(DEFAULT_QUEUE_LABEL))]:
            self._queue.addItem(name, queue_id)
        form.addRow(t("Queue"), self._queue)
        layout.addLayout(form)

        # Per-download HTTPS escape hatch. Off by default and never sticky: it
        # applies to this download alone and does not touch Settings.
        self._insecure = QCheckBox(t("Ignore HTTPS certificate errors for this download"))
        self._insecure.setToolTip(
            t(
                "Only for a server whose certificate you already trust (a NAS, a "
                "lab machine). With this on, nothing proves the server is who the "
                "address says it is."
            )
        )
        layout.addWidget(self._insecure)

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

    def chosen_directory(self) -> str:
        return self._directory.text().strip()

    def dont_ask_again(self) -> bool:
        return self._dont_ask.isChecked()

    def chosen_queue(self) -> int | None:
        """The queue id the user picked, or None for the default queue. Always
        an explicit answer - the caller passes it straight to the manager."""
        data = self._queue.currentData()
        return int(data) if data is not None else None

    def ignore_certificate_errors(self) -> bool:
        """True when this one download may accept an invalid HTTPS certificate.
        Deliberately has no setter side effect: the global Settings value is
        untouched either way."""
        return self._insecure.isChecked()


class AddUrlDialog(chrome.Dialog):
    """The toolbar's "Add download" prompt.

    It used to be a bare ``QInputDialog.getText``, which is why a URL typed or
    pasted into GrabLine had no way to say which queue it belonged to - the
    queue picker existed only on the browser-handoff dialog. Same one-line
    prompt, plus the two per-download choices, so every normal entry path can
    answer "which queue?" before the job is created.
    """

    def __init__(
        self,
        *,
        queues: Sequence[tuple[int | None, str]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("Add download"))
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.addWidget(
            components.role_label(t("Add download"), "strong", size=design.FONT["h1"], bold=True)
        )

        form = QFormLayout()
        self._url = QLineEdit()
        self._url.setPlaceholderText("https://…")
        form.addRow(t("URL (ranges like file[1-20].jpg expand):"), self._url)

        self._queue = QComboBox()
        for queue_id, name in queues if queues is not None else [(None, t(DEFAULT_QUEUE_LABEL))]:
            self._queue.addItem(name, queue_id)
        form.addRow(t("Queue"), self._queue)
        layout.addLayout(form)

        self._insecure = QCheckBox(t("Ignore HTTPS certificate errors for this download"))
        layout.addWidget(self._insecure)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton(t("Cancel"))
        cancel.clicked.connect(self.reject)
        add = components.accent_button(t("Add"))
        add.setDefault(True)
        add.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(add)
        layout.addLayout(buttons)
        self._url.returnPressed.connect(self.accept)
        components.cap_field_widths(self, width=380)

    def url(self) -> str:
        return self._url.text().strip()

    def chosen_queue(self) -> int | None:
        data = self._queue.currentData()
        return int(data) if data is not None else None

    def ignore_certificate_errors(self) -> bool:
        return self._insecure.isChecked()
