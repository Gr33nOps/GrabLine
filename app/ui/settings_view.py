"""The embedded Settings page: a left nav (search + tab list) and a scrolling
content pane on the right, matching the redesign mockups.

It reuses the existing SettingsDialog for all the field building and the save
logic - the dialog is constructed but never shown; its tab pages are lifted
out and re-hosted here, and Save calls the dialog's ``apply()``.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import t
from app.core.settings import Settings
from app.ui import components, design, theme
from app.ui.icons import svg_icon
from app.ui.settings_dialog import SettingsDialog


class SettingsView(QWidget):
    def __init__(
        self,
        settings: Settings,
        on_applied: Callable[[], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_applied = on_applied
        # Build the dialog (never shown) to own the fields + apply() logic.
        self._dialog = SettingsDialog(settings)
        # A reset writes straight to the store, so the window has to re-read it
        # the same way it does after Save.
        self._dialog.settings_reset.connect(on_applied)
        p = theme.current()

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        # ---- left nav -------------------------------------------------------
        nav = QFrame()
        nav.setObjectName("SettingsNav")
        nav.setFixedWidth(196)
        nlay = QVBoxLayout(nav)
        nlay.setContentsMargins(10, 12, 10, 12)
        nlay.setSpacing(8)

        self._search = QLineEdit()
        self._search.setPlaceholderText(t("Search settings…"))
        self._search.addAction(
            svg_icon("search", p.text3), QLineEdit.ActionPosition.LeadingPosition
        )
        self._search.textChanged.connect(self._filter)
        nlay.addWidget(self._search)

        self._list = QListWidget()
        self._list.setObjectName("SettingsList")
        self._list.currentRowChanged.connect(self._on_nav)
        nlay.addWidget(self._list, 1)
        row.addWidget(nav)

        # ---- content --------------------------------------------------------
        content = QWidget()
        clay = QVBoxLayout(content)
        clay.setContentsMargins(0, 0, 0, 0)
        clay.setSpacing(0)

        self._title = components.role_label("", "strong", size=design.FONT["h1"], bold=True)
        self._title.setContentsMargins(24, 18, 24, 8)
        clay.addWidget(self._title)

        self._stack = QStackedWidget()
        # Lift each tab page out of the dialog's QTabWidget into our stack,
        # wrapped in a scroll area so long tabs scroll.
        tabs = self._dialog.tabs
        self._titles: list[str] = []
        pages = [(tabs.tabText(i).replace("&&", "&"), tabs.widget(i)) for i in range(tabs.count())]
        for title, page in pages:
            if page is None:
                continue
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            holder = QWidget()
            # Let free-form pages (Video, About, …) shrink with the viewport so
            # wrapping labels reflow instead of forcing a horizontal clip.
            holder.setMinimumWidth(0)
            hl = QVBoxLayout(holder)
            hl.setContentsMargins(24, 8, 24, 20)
            # A page that manages its own scrolling (Shortcuts) fills the height
            # so its list uses the whole pane instead of a short box over a gap;
            # a form page keeps its natural height with a stretch below it.
            if getattr(page, "fills_height", False):
                hl.addWidget(page, 1)
            else:
                hl.addWidget(page)
                hl.addStretch(1)
            # The QTabWidget explicitly hides its pages; lifting one out keeps it
            # hidden, so re-show it or the content pane renders blank.
            page.show()
            scroll.setWidget(holder)
            self._stack.addWidget(scroll)
            self._titles.append(title)
            item = QListWidgetItem(title)
            self._list.addItem(item)
        clay.addWidget(self._stack, 1)

        footer = QFrame()
        footer.setObjectName("SettingsFooter")
        flay = QHBoxLayout(footer)
        flay.setContentsMargins(24, 10, 24, 10)
        flay.addStretch(1)
        self._saved = components.role_label("", "ok", size=design.FONT["small"])
        flay.addWidget(self._saved)
        save = components.accent_button(t("Save changes"))
        save.clicked.connect(self._save)
        flay.addWidget(save)
        clay.addWidget(footer)

        row.addWidget(content, 1)
        self._list.setCurrentRow(0)

    def _on_nav(self, index: int) -> None:
        if 0 <= index < self._stack.count():
            self._stack.setCurrentIndex(index)
            self._title.setText(self._titles[index])
            self._saved.setText("")

    def _filter(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self._list.count()):
            item = self._list.item(i)
            item.setHidden(bool(needle) and needle not in item.text().lower())

    def _save(self) -> None:
        if self._dialog.apply():
            self._saved.setText(t("Saved"))
            self._on_applied()
