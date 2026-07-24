"""Custom window chrome for the frameless main window.

``TitleBar`` is the caption bar (logo, title, min/max/close, drag-to-move,
double-click-to-maximize). ``EdgeResizer`` restores edge/corner drag-resizing
that the frameless hint takes away. ``Dialog`` is a plain QDialog base that
keeps the native OS title bar - only the main window uses custom chrome.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt
from PySide6.QtGui import QGuiApplication, QRegion
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QPushButton,
    QWidget,
)

from app.core.i18n import t
from app.ui import design, theme
from app.ui.icons import svg_icon

_BAR_HEIGHT = 38
# The edges are a thin sliver so a scrollbar sitting at the window's right or
# bottom edge stays grabbable; the corners are a fat square so diagonal resize
# is still easy to hit.
_EDGE_MARGIN = 4
_CORNER_MARGIN = 20


class _CaptionButton(QPushButton):
    """A flat 34px window-control button (minimize / maximize / close)."""

    def __init__(self, icon_name: str, tooltip: str, *, danger: bool = False) -> None:
        super().__init__()
        self._icon_name = icon_name
        self._danger = danger
        self.setFixedSize(40, _BAR_HEIGHT - 8)
        self.setToolTip(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.retint()

    def set_icon_name(self, name: str) -> None:
        self._icon_name = name
        self.retint()

    def retint(self) -> None:
        p = theme.current()
        self.setIcon(svg_icon(self._icon_name, p.text2))
        hover = p.warn if self._danger else p.row_hover
        hover_fg = "#ffffff" if self._danger else p.text
        self.setStyleSheet(
            f"QPushButton {{ border: none; border-radius: {design.RADIUS['sm']}px;"
            f" background: transparent; }}"
            f" QPushButton:hover {{ background: {hover}; color: {hover_fg}; }}"
        )


class TitleBar(QFrame):
    """The custom caption bar: drag to move, double-click to maximize, and
    the standard window controls. ``dialog=True`` shows only Close."""

    def __init__(self, window: QWidget, *, dialog: bool = False) -> None:
        super().__init__()
        self._window = window
        self.setObjectName("TitleBar")
        self.setFixedHeight(_BAR_HEIGHT)
        # Qt6 + FramelessWindowHint on Windows can leave isMaximized() false
        # after a successful visual maximize. Without our own flag, the first
        # Restore click takes the Maximize branch again and the user has to
        # press twice. Track the chrome state ourselves and keep a geometry to
        # put back.
        self._filled_screen = False
        self._restore_geometry: QRect | None = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 6, 0)
        lay.setSpacing(8)

        from app.ui import components

        # Every bar carries the mark next to its title - main window included.
        lay.addWidget(components.app_logo(18))
        self._title = components.role_label(window.windowTitle() or "GrabLine", "strong")
        window.windowTitleChanged.connect(self._title.setText)
        lay.addWidget(self._title)
        lay.addStretch(1)

        self._buttons: list[_CaptionButton] = []
        self._max_btn: _CaptionButton | None = None
        if not dialog:
            mini = _CaptionButton("minimize", "Minimize")
            mini.clicked.connect(window.showMinimized)
            self._max_btn = _CaptionButton("maximize", "Maximize")
            self._max_btn.clicked.connect(self._toggle_maximized)
            self._buttons += [mini, self._max_btn]
        close = _CaptionButton("cancel", "Close", danger=True)
        close.clicked.connect(window.close)
        self._buttons.append(close)
        for btn in self._buttons:
            lay.addWidget(btn)
        window.installEventFilter(self)

    def retint(self) -> None:
        for btn in self._buttons:
            btn.retint()

    def caption_buttons_region(self) -> QRegion:
        """The window controls' rects, in the top-level window's coordinates.

        The EdgeResizer overlay covers the window's border - including the top
        strip and the top-right corner square, which sit right over these
        buttons. Without carving them out, a click on the top of Maximize/Close
        lands on the resizer (a no-op) and the button needs a second, lower
        press (the reported bug). The resizer subtracts this from its mask."""
        window = self.window()
        region = QRegion()
        for btn in self._buttons:
            top_left = btn.mapTo(window, btn.rect().topLeft())
            region = region.united(QRegion(QRect(top_left, btn.size())))
        return region

    def fills_screen(self) -> bool:
        """True while the caption chrome considers the window maximized."""
        return self._filled_screen or self._window.isMaximized()

    def _geometry_fills_screen(self) -> bool:
        screen = self._window.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return False
        available = screen.availableGeometry()
        geo = self._window.geometry()
        return (
            abs(geo.width() - available.width()) <= 2
            and abs(geo.height() - available.height()) <= 2
            and abs(geo.x() - available.x()) <= 2
            and abs(geo.y() - available.y()) <= 2
        )

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self._window and event.type() == QEvent.Type.WindowStateChange:
            # Win+Up / taskbar maximize: adopt native state. Win+Down / restore:
            # clear our flag so the button and edge resizer agree. Ignore the
            # spurious "not maximized" Qt emits after a frameless showMaximized
            # while the window still fills the screen.
            if self._window.isMaximized():
                if self._restore_geometry is None:
                    self._restore_geometry = QRect(self._window.normalGeometry())
                self._filled_screen = True
            elif not self._window.isMinimized():
                self._filled_screen = self._geometry_fills_screen()
            self._sync_max_button()
        return super().eventFilter(obj, event)

    def _toggle_maximized(self) -> None:
        if self.fills_screen():
            self._restore_window()
        else:
            self._maximize_window()

    def _maximize_window(self) -> None:
        win = self._window
        self._restore_geometry = QRect(win.geometry())
        self._filled_screen = True
        win.showMaximized()
        # Qt6 frameless on Windows: first showMaximized() can paint full-screen
        # while leaving isMaximized() false. Fill available geometry ourselves
        # so Restore has a real pre-maximize rect to put back in one click.
        if not win.isMaximized():
            screen = win.screen() or QGuiApplication.primaryScreen()
            if screen is not None:
                win.setGeometry(screen.availableGeometry())
        # Re-assert after Qt's confused state events (see eventFilter).
        self._filled_screen = True
        self._sync_max_button()
        # Edge resizer listens for WindowStateChange; nudge it for the
        # pseudo-maximized (geometry-only) path too.
        resizer = getattr(win, "_resizer", None)
        if resizer is not None:
            resizer._sync()

    def _restore_window(self) -> None:
        win = self._window
        geo = self._restore_geometry
        self._filled_screen = False
        if win.isMaximized():
            win.showNormal()
        # Always re-apply the saved rect: when Qt lied about isMaximized(),
        # showNormal() is a no-op and only setGeometry actually shrinks the
        # window (the second click the user had to make before).
        if geo is not None and geo.isValid():
            win.setGeometry(geo)
        self._restore_geometry = None
        self._filled_screen = False
        self._sync_max_button()
        resizer = getattr(win, "_resizer", None)
        if resizer is not None:
            resizer._sync()

    def _sync_max_button(self) -> None:
        if self._max_btn is None:
            return
        if self.fills_screen():
            self._max_btn.set_icon_name("restore")
            self._max_btn.setToolTip(t("Restore"))
        else:
            self._max_btn.set_icon_name("maximize")
            self._max_btn.setToolTip(t("Maximize"))

    def mousePressEvent(self, event: object) -> None:
        ev = event
        if ev.button() == Qt.MouseButton.LeftButton:  # type: ignore[attr-defined]
            handle = self._window.windowHandle()
            if handle is not None:
                handle.startSystemMove()
                return
        super().mousePressEvent(ev)  # type: ignore[arg-type]

    def mouseDoubleClickEvent(self, event: object) -> None:
        if self._max_btn is not None:
            self._toggle_maximized()
        super().mouseDoubleClickEvent(event)  # type: ignore[arg-type]


_RESIZE_CURSORS = {
    Qt.Edge.LeftEdge: Qt.CursorShape.SizeHorCursor,
    Qt.Edge.RightEdge: Qt.CursorShape.SizeHorCursor,
    Qt.Edge.TopEdge: Qt.CursorShape.SizeVerCursor,
    Qt.Edge.BottomEdge: Qt.CursorShape.SizeVerCursor,
    Qt.Edge.LeftEdge | Qt.Edge.TopEdge: Qt.CursorShape.SizeFDiagCursor,
    Qt.Edge.RightEdge | Qt.Edge.BottomEdge: Qt.CursorShape.SizeFDiagCursor,
    Qt.Edge.RightEdge | Qt.Edge.TopEdge: Qt.CursorShape.SizeBDiagCursor,
    Qt.Edge.LeftEdge | Qt.Edge.BottomEdge: Qt.CursorShape.SizeBDiagCursor,
}


class EdgeResizer(QWidget):
    """Restores edge- and corner-drag resizing on a frameless top-level window.

    A previous version filtered mouse events on the window itself, but the
    window's children cover every edge and either consume those events or -
    lacking mouse tracking - never generate them, so resizing worked only in
    stray gaps and the resize cursor could stick. This is a thin overlay
    instead: it covers the window but is masked to just the outer margin, so
    only that border is interactive (clicks in the center pass straight through
    to the content). Because the overlay owns its border region, it always sees
    its own move/press/leave events - resizing works over any child, and the
    cursor resets the moment the pointer leaves the border."""

    def __init__(self, window: QWidget, title_bar: TitleBar | None = None) -> None:
        super().__init__(window)
        self._window = window
        # The caption buttons are carved out of the resize mask so the overlay
        # never sits over Minimize/Maximize/Close and swallows their clicks.
        self._title_bar = title_bar
        self.setMouseTracking(True)
        self.setStyleSheet("background: transparent;")
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        window.installEventFilter(self)
        self._sync()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self._window and event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.WindowStateChange,
        ):
            self._sync()
        return super().eventFilter(obj, event)

    def _sync(self) -> None:
        """Match the window's size, and mask to a thin border frame plus a fat
        square at each corner - the only regions that grab the resize. Hidden
        while maximized/fullscreen, where there is nothing to resize."""
        filled = self._window.isMaximized() or self._window.isFullScreen()
        if not filled and self._title_bar is not None:
            filled = self._title_bar.fills_screen()
        if filled:
            self.hide()
            return
        self.setGeometry(self._window.rect())
        rect = self.rect()
        e, c = _EDGE_MARGIN, _CORNER_MARGIN
        mask = QRegion(rect).subtracted(QRegion(rect.adjusted(e, e, -e, -e)))
        w, h = rect.width(), rect.height()
        for cx, cy in ((0, 0), (w - c, 0), (0, h - c), (w - c, h - c)):
            mask = mask.united(QRegion(cx, cy, c, c))
        if self._title_bar is not None:
            # Never grab where the window controls are - clicks belong to them.
            mask = mask.subtracted(self._title_bar.caption_buttons_region())
        self.setMask(mask)
        self.show()
        self.raise_()

    def _edges(self, pos: QPoint) -> Qt.Edge:
        rect = self.rect()
        e, c = _EDGE_MARGIN, _CORNER_MARGIN
        x, y, w, h = pos.x(), pos.y(), rect.width(), rect.height()
        # A corner grab: within the fat corner square on both axes.
        near_l, near_r = x <= c, x >= w - c
        near_t, near_b = y <= c, y >= h - c
        if (near_l or near_r) and (near_t or near_b):
            edges = Qt.Edge(0)
            if near_l:
                edges |= Qt.Edge.LeftEdge
            if near_r:
                edges |= Qt.Edge.RightEdge
            if near_t:
                edges |= Qt.Edge.TopEdge
            if near_b:
                edges |= Qt.Edge.BottomEdge
            return edges
        # Otherwise a thin edge grab.
        edges = Qt.Edge(0)
        if x <= e:
            edges |= Qt.Edge.LeftEdge
        if x >= w - e:
            edges |= Qt.Edge.RightEdge
        if y <= e:
            edges |= Qt.Edge.TopEdge
        if y >= h - e:
            edges |= Qt.Edge.BottomEdge
        return edges

    def mouseMoveEvent(self, event: object) -> None:
        edges = self._edges(event.position().toPoint())  # type: ignore[attr-defined]
        self.setCursor(_RESIZE_CURSORS.get(edges, Qt.CursorShape.ArrowCursor))

    def mousePressEvent(self, event: object) -> None:
        if event.button() == Qt.MouseButton.LeftButton:  # type: ignore[attr-defined]
            edges = self._edges(event.position().toPoint())  # type: ignore[attr-defined]
            handle = self._window.windowHandle()
            if edges and handle is not None:
                handle.startSystemResize(edges)

    def leaveEvent(self, event: object) -> None:
        # The pointer left the border (into the content or off the window):
        # drop the resize cursor so it never sticks.
        self.unsetCursor()
        super().leaveEvent(event)  # type: ignore[arg-type]


class Dialog(QDialog):
    """A plain QDialog that keeps the native OS title bar.

    Dialogs used to be wrapped in the same frameless custom bar as the main
    window, but the native bar is cleaner, already carries the app icon and
    the standard window controls, and is properly movable everywhere. Only the
    main window keeps custom chrome now. Subclasses need no change - this is a
    thin alias so the ``chrome.Dialog`` base can stay in place."""
