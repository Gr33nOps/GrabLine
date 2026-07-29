"""Reusable, theme-aware widgets shared across the redesigned screens: status
pills, stat tiles, tag chips, flat icon buttons, sidebar nav buttons, section
labels, and a small area-graph card. Every one reads its colors from
``theme.current()`` so light/dark just works.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable

from PySide6.QtCore import QPointF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import N_, t
from app.ui import design, theme
from app.ui.icons import svg_icon

_GRAPH_HISTORY = 120

_STATUS_LABEL = {
    "downloading": N_("Downloading"),
    "queued": N_("Queued"),
    "paused": N_("Paused"),
    "completed": N_("Completed"),
    "failed": N_("Failed"),
    "cancelled": N_("Cancelled"),
}


def role_label(text: str, role: str, *, size: int | None = None, bold: bool = False) -> QLabel:
    """A label whose *color* comes from the global stylesheet (``role`` -> a
    QLabel[role=...] rule), so it re-tints automatically on a theme swap. Size
    and weight are set on the font here since QSS can't be re-read per widget."""
    lbl = QLabel(text)
    lbl.setProperty("role", role)
    if size is not None or bold:
        font = lbl.font()
        if size is not None:
            font.setPointSize(size)
        font.setBold(bold)
        lbl.setFont(font)
    return lbl


class StatusPill(QLabel):
    """A quiet status readout: colored dot + tinted label, no fill. Eight
    filled chips in a column read as a wall of buttons; text reads as state."""

    def __init__(self, status: str = "queued", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._rendered: tuple[str, str] | None = None
        self.set_status(status)

    def set_status(self, status: str) -> None:
        p = theme.current()
        color = design.status_color(p, status)
        # setStyleSheet re-polishes the widget on every call, identical string
        # or not. The download list re-sends each row's status twice a second,
        # so skipping the unchanged ones is the difference between a quiet list
        # and a full-table restyle at 2Hz.
        if self._rendered == (status, color):
            return
        self._rendered = (status, color)
        label = t(_STATUS_LABEL.get(status, status.title()))
        self.setText(f"●  {label}")
        self.setStyleSheet(
            f"QLabel {{ color: {color}; background: transparent;"
            f" font-size: {design.FONT['small']}pt; font-weight: 600; }}"
        )


class Chip(QLabel):
    """A small tag/label chip; colours follow the theme via the stylesheet."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setProperty("chip", "true")


class SectionLabel(QLabel):
    """An all-caps, muted section heading."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text.upper(), parent)
        self.setProperty("role", "caption")
        font = self.font()
        font.setPointSize(design.FONT["caption"])
        font.setBold(True)
        self.setFont(font)


class StatTile(QFrame):
    """A big-number + caption tile (dashboard)."""

    def __init__(self, caption: str, accent: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("card", "true")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 11, 14, 11)
        lay.setSpacing(3)
        cap = role_label(caption.upper(), "caption", size=design.FONT["caption"])
        self.value = role_label(
            "", "accent" if accent else "strong", size=design.FONT["display"], bold=True
        )
        self.value.setFont(design.numeric_font(self.value.font()))
        self.sub = role_label("", "muted", size=design.FONT["small"])
        self.sub.hide()
        lay.addWidget(cap)
        lay.addWidget(self.value)
        lay.addWidget(self.sub)

    def set_value(self, text: str, sub: str = "") -> None:
        self.value.setText(text)
        if sub:
            self.sub.setText(sub)
            self.sub.show()


class IconButton(QPushButton):
    """A flat, icon-first toolbar button; optional text label."""

    def __init__(
        self,
        icon_name: str,
        label: str = "",
        *,
        danger: bool = False,
        tooltip: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._icon_name = icon_name
        self._danger = danger
        self.setProperty("flat", "true")
        if danger:
            self.setProperty("danger", "true")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if label:
            self.setText("  " + label)
        # Every button explains itself on hover - icon-only ones especially.
        if tooltip or label:
            self.setToolTip(tooltip or label)
        # Give assistive tech a real name: an icon-only button has no text for a
        # screen reader to announce, so without this it reads as "button".
        name = label or tooltip
        if name:
            self.setAccessibleName(name)
        self.setIconSize(QSize(16, 16))
        self.retint()

    def set_icon_name(self, icon_name: str) -> None:
        """Swap the glyph (e.g. a pause button flipping to resume)."""
        if icon_name != self._icon_name:
            self._icon_name = icon_name
            self.retint()

    def retint(self) -> None:
        # Danger buttons rest neutral and only turn red on hover: a toolbar
        # with a permanently red trash can reads as an alarm, not a tool.
        p = theme.current()
        self.setIcon(svg_icon(self._icon_name, p.text2))

    def enterEvent(self, event: object) -> None:
        if self._danger:
            self.setIcon(svg_icon(self._icon_name, theme.current().warn))
        super().enterEvent(event)  # type: ignore[arg-type]

    def leaveEvent(self, event: object) -> None:
        if self._danger:
            self.setIcon(svg_icon(self._icon_name, theme.current().text2))
        super().leaveEvent(event)  # type: ignore[arg-type]


class SidebarButton(QPushButton):
    """A 36px square nav button for the left rail; active state uses the accent."""

    def __init__(self, icon_name: str, tooltip: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._icon_name = icon_name
        self._active = False
        self.setToolTip(tooltip)
        # Icon-only nav button: name it for screen readers (it otherwise reads
        # as an unlabeled "button").
        self.setAccessibleName(tooltip)
        self.setFixedSize(38, 38)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setIconSize(QSize(19, 19))
        self.retint()

    def set_active(self, active: bool) -> None:
        self._active = active
        self.setChecked(active)
        self.retint()

    def retint(self) -> None:
        p = theme.current()
        color = p.accent if self._active else p.text3
        self.setIcon(svg_icon(self._icon_name, color))
        bg = p.accent_dim if self._active else "transparent"
        self.setStyleSheet(
            f"QPushButton {{ border: none; border-radius: {design.RADIUS['md']}px;"
            f" background: {bg}; }}"
            f" QPushButton:hover {{ background: {p.row_hover}; }}"
        )


class GraphCard(QFrame):
    """A titled area-chart card (dashboard). Feed samples with :meth:`push`;
    draws one or two series over a shared auto-scaled axis. Between pushes the
    plot scrolls smoothly (via the shared ticker) instead of stepping.

    Scroll geometry matches the detail-drawer sparkline / TimeGraph: one extra
    sample sits off the left edge so the curve slides out under the clip
    instead of the left end flickering when a point leaves the window.
    """

    def __init__(
        self,
        title: str,
        colors: list[str],
        fmt: Callable[[float], str],
        *,
        fixed_max: float | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.title = title
        self._colors = colors
        self._fmt = fmt
        self._fixed_max = fixed_max
        # +1 past the visible window so the oldest point always sits off the
        # left edge (same trick as TimeGraph / Sparkline).
        self._series: list[deque[float]] = [deque(maxlen=_GRAPH_HISTORY + 1) for _ in colors]
        self._last_push = 0.0
        self._push_interval = 0.5
        self._animating = False
        self._scale_value = 0.0
        self.setProperty("card", "true")
        self.setMinimumHeight(118)

    def push(self, values: list[float]) -> None:
        import time

        now = time.monotonic()
        if self._last_push:
            gap = min(2.0, max(0.05, now - self._last_push))
            self._push_interval = self._push_interval * 0.7 + gap * 0.3
        self._last_push = now
        for serie, v in zip(self._series, values, strict=False):
            serie.append(max(0.0, v))
        self.update()

    def _scroll_frac(self) -> float:
        """0..1 progress toward the next data point, for smooth scrolling."""
        import time

        if not self._last_push:
            return 1.0
        return min(1.0, (time.monotonic() - self._last_push) / self._push_interval)

    def showEvent(self, event: object) -> None:
        super().showEvent(event)  # type: ignore[arg-type]
        if not self._animating:
            from app.ui import motion

            motion.ticker().tick.connect(self.update)
            motion.ticker().subscribe()
            self._animating = True

    def hideEvent(self, event: object) -> None:
        super().hideEvent(event)  # type: ignore[arg-type]
        if self._animating:
            self._animating = False
            import contextlib

            from app.ui import motion

            with contextlib.suppress(RuntimeError, TypeError):  # app teardown
                motion.ticker().tick.disconnect(self.update)
                motion.ticker().unsubscribe()

    def _scale(self) -> float:
        from app.ui import motion

        if self._fixed_max is not None:
            return self._fixed_max
        peak = max((max(s, default=0.0) for s in self._series), default=0.0)
        # Ease down (peak scrolling out), snap up - same as the detail graph.
        self._scale_value = motion.ease_scale(self._scale_value, peak)
        return self._scale_value if self._scale_value > 0 else 1.0

    def paintEvent(self, _event: object) -> None:
        from app.ui import motion

        p = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        # title + latest readout
        painter.setPen(QColor(p.text2))
        painter.drawText(rect.adjusted(12, 9, -12, 0), int(Qt.AlignmentFlag.AlignLeft), self.title)
        latest = " · ".join(self._fmt(s[-1]) for s in self._series if s)
        painter.setPen(QColor(self._colors[0]))
        painter.drawText(rect.adjusted(12, 9, -12, 0), int(Qt.AlignmentFlag.AlignRight), latest)
        plot = rect.adjusted(12, 30, -12, -12)
        if plot.width() < 4 or plot.height() < 4:
            painter.end()
            return
        # subtle baseline grid
        grid = QColor(p.border2)
        painter.setPen(QPen(grid, 1, Qt.PenStyle.DashLine))
        for f in (0.5, 1.0):
            y = plot.bottom() - f * plot.height()
            painter.drawLine(plot.left(), int(y), plot.right(), int(y))
        scale = self._scale()
        step = plot.width() / (_GRAPH_HISTORY - 1)
        # Anchored on the newest sample at the right; walk back so the oldest
        # runs off the left under the clip (smooth exit, no left-edge blink).
        newest_x = plot.left() + plot.width() + (1.0 - self._scroll_frac()) * step
        painter.setClipRect(plot)
        for serie, hexc in zip(self._series, self._colors, strict=False):
            if len(serie) < 2:
                continue
            color = QColor(hexc)
            base = plot.bottom()
            last = len(serie) - 1
            pts = [
                QPointF(
                    newest_x - (last - i) * step,
                    base - min(1.0, v / scale) * plot.height() * motion.GRAPH_HEADROOM,
                )
                for i, v in enumerate(serie)
            ]
            fill = QColor(color)
            fill.setAlpha(38)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawPolygon(
                QPolygonF([QPointF(pts[0].x(), base), *pts, QPointF(pts[-1].x(), base)])
            )
            painter.setPen(QPen(color, 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolyline(QPolygonF(pts))
        painter.end()


def cap_field_widths(root: QWidget, width: int = 320) -> None:
    """Stop enum-ish fields (combos, spinners, time pickers) stretching to
    the form's full width: a three-option dropdown does not need 800px.
    Free-text inputs keep growing: paths and URLs genuinely want room."""
    from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QSpinBox, QTimeEdit

    for cls in (QComboBox, QSpinBox, QDoubleSpinBox, QTimeEdit):
        for field in root.findChildren(cls):
            if field.maximumWidth() > width:
                field.setMaximumWidth(width)


def accent_button(text: str) -> QPushButton:
    """A primary (accent-filled) button."""
    btn = QPushButton(text)
    btn.setProperty("accent", "true")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


def card_frame() -> QFrame:
    frame = QFrame()
    frame.setProperty("card", "true")
    return frame


def app_logo(size: int = 28) -> QLabel:
    """The brand logo as a rounded square. Uses the real logo asset; falls
    back to the drawn blue-square-with-arrow mark if the asset is missing."""
    from PySide6.QtGui import QPainterPath, QPixmap

    from app.ui.icon import logo_pixmap

    label = QLabel()
    label.setFixedSize(size, size)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    source = logo_pixmap()
    if source is None:
        label.setObjectName("AppLogo")  # blue rounded square via the stylesheet
        label.setPixmap(svg_icon("download", "#ffffff").pixmap(int(size * 0.6), int(size * 0.6)))
        return label
    ratio = label.devicePixelRatioF()
    px = int(size * ratio)
    scaled = source.scaled(
        px, px, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
    )
    rounded = QPixmap(px, px)
    rounded.fill(Qt.GlobalColor.transparent)
    painter = QPainter(rounded)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, px, px, px * 0.22, px * 0.22)
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, scaled)
    painter.end()
    rounded.setDevicePixelRatio(ratio)
    label.setPixmap(rounded)
    return label
