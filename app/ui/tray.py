"""System tray icon with show/hide and quit (part of F0.4)."""

from __future__ import annotations

from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from app.core.i18n import t
from app.ui.icon import make_app_icon
from app.ui.main_window import MainWindow


class GrabLineTray(QSystemTrayIcon):
    def __init__(self, window: MainWindow) -> None:
        super().__init__(make_app_icon())
        self.setToolTip("GrabLine")
        self._window = window
        self._menu = QMenu()
        show_action = self._menu.addAction(t("Show / Hide"))
        show_action.triggered.connect(self._toggle_window)
        self._menu.addSeparator()
        quit_action = self._menu.addAction(t("Quit GrabLine"))
        quit_action.triggered.connect(self._quit)
        self.setContextMenu(self._menu)
        self.activated.connect(self._on_activated)

    def _quit(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _toggle_window(self) -> None:
        if self._window.isVisible():
            self._window.hide()
        else:
            self._window.show()
            self._window.raise_()
            self._window.activateWindow()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_window()
