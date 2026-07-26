"""Playlist selection panel (F1.7): fast flat listing → checkbox list with
select all/none, a quality choice applied to the batch, and a sane
preselection cap so a 2,000-video playlist doesn't queue itself.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import t
from app.engines.smart import (
    PlaylistEntry,
    PlaylistInfo,
    QualityOption,
    generic_quality_options,
)
from app.ui import chrome
from app.ui.format import duration_text


class PlaylistPanel(chrome.Dialog):
    def __init__(
        self,
        playlist: PlaylistInfo,
        *,
        preselect_cap: int = 30,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.playlist = playlist
        self.setWindowTitle(t("Playlist"))
        self.setMinimumSize(480, 420)

        layout = QVBoxLayout(self)
        title = QLabel(f"{playlist.title}  " + t("({count} items)", count=len(playlist.entries)))
        title.setWordWrap(True)
        title.setStyleSheet("font-weight: 600; font-size: 14px;")
        layout.addWidget(title)
        if playlist.uploader:
            uploader = QLabel(playlist.uploader)
            uploader.setStyleSheet("color: gray;")
            layout.addWidget(uploader)

        select_row = QHBoxLayout()
        select_all = QPushButton(t("Select all"))
        select_none = QPushButton(t("Select none"))
        select_all.clicked.connect(lambda: self._set_all(Qt.CheckState.Checked))
        select_none.clicked.connect(lambda: self._set_all(Qt.CheckState.Unchecked))
        select_row.addWidget(select_all)
        select_row.addWidget(select_none)
        select_row.addStretch(1)
        self._selection_label = QLabel("")
        select_row.addWidget(self._selection_label)
        layout.addLayout(select_row)

        self.entry_list = QListWidget()
        for entry in playlist.entries:
            text = f"{entry.index:>3}.  {entry.title}"
            if entry.duration:
                text += f"   ({duration_text(entry.duration)})"
            item = QListWidgetItem(text)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if entry.index <= preselect_cap else Qt.CheckState.Unchecked
            )
            self.entry_list.addItem(item)
        self.entry_list.itemChanged.connect(lambda _item: self._update_count())
        layout.addWidget(self.entry_list)
        if len(playlist.entries) > preselect_cap:
            note = QLabel(
                t(
                    "The first {count} are preselected. Use Select all for the rest.",
                    count=preselect_cap,
                )
            )
            note.setStyleSheet("color: gray; font-size: 11px;")
            layout.addWidget(note)

        quality_row = QHBoxLayout()
        quality_row.addWidget(QLabel(t("Quality for all:")))
        self.quality_combo = QComboBox()
        for option in generic_quality_options():
            label = option.label + ("  " + t("(audio only)") if option.kind == "audio" else "")
            self.quality_combo.addItem(label, option)
        quality_row.addWidget(self.quality_combo, 1)
        layout.addLayout(quality_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update_count()

    # -------------------------------------------------------------- result

    def selected_entries(self) -> list[PlaylistEntry]:
        return [
            entry
            for row, entry in enumerate(self.playlist.entries)
            if self.entry_list.item(row).checkState() is Qt.CheckState.Checked
        ]

    def selected_option(self) -> QualityOption:
        option = self.quality_combo.currentData()
        assert isinstance(option, QualityOption)
        return option

    # ------------------------------------------------------------ internals

    def _set_all(self, state: Qt.CheckState) -> None:
        for row in range(self.entry_list.count()):
            self.entry_list.item(row).setCheckState(state)

    def _update_count(self) -> None:
        count = len(self.selected_entries())
        self._selection_label.setText(t("{count} selected", count=count))
        self._ok_button.setText(t("Download {count}", count=count) if count else t("Download"))
        self._ok_button.setEnabled(count > 0)
