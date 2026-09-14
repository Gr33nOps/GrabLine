"""A tiny live sparkline of the total download speed, for the toolbar."""

from __future__ import annotations

from collections import deque

from PySide6.QtCore import QPointF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from app.core.i18n import t
from app.ui.format import human_bytes

_HISTORY = 60  # data points shown (about 30s at the 500ms refresh)


class Sparkline(QWidget):
    """Shows recent aggregate speed as a filled line plus a numeric readout."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._samples: deque[float] = deque(maxlen=_HISTORY)
        self.setMinimumSize(150, 26)
        self.setToolTip(t("Total download speed"))

    def push(self, bytes_per_second: float) -> None:
        self._samples.append(max(0.0, bytes_per_second))
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(160, 26)

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(2, 2, -2, -2)
        accent = self.palette().color(self.palette().ColorRole.Highlight)

        peak = max(self._samples, default=0.0)
        if peak > 0 and len(self._samples) > 1:
            step = rect.width() / (_HISTORY - 1)
            baseline = rect.bottom()
            points = [
                QPointF(rect.left() + i * step, baseline - (value / peak) * rect.height())
                for i, value in enumerate(self._samples)
            ]
            fill = QPolygonF(
                [QPointF(points[0].x(), baseline), *points, QPointF(points[-1].x(), baseline)]
            )
            faded = QColor(accent)
            faded.setAlpha(60)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(faded)
            painter.drawPolygon(fill)
            painter.setPen(QPen(accent, 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolyline(QPolygonF(points))

        current = self._samples[-1] if self._samples else 0.0
        painter.setPen(self.palette().color(self.palette().ColorRole.WindowText))
        painter.drawText(
            rect,
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            f"{human_bytes(current)}/s" if current else "",
        )
        painter.end()
