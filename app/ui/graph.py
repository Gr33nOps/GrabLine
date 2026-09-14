"""A small multi-series time-graph widget for the dashboard - filled line
charts that scroll left as new samples arrive. Theme-aware: it paints with
the current palette so it reads in light and dark.
"""

from __future__ import annotations

import contextlib
import time
from collections import deque
from collections.abc import Callable

from PySide6.QtCore import QPointF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from app.ui import motion

_HISTORY = 120  # samples kept (~1 min at the 500ms refresh)


class Series:
    def __init__(self, name: str, color: QColor) -> None:
        self.name = name
        self.color = color
        # One past the visible window, so the oldest point always sits off the
        # left edge and the curve slides out under the clip instead of the
        # left end popping between positions. See TimeGraph.paintEvent.
        self.samples: deque[float] = deque(maxlen=_HISTORY + 1)


class TimeGraph(QWidget):
    """One or more series on a shared, auto-scaling y-axis. ``fmt`` renders the
    latest value into the corner readout (bytes/sec, percent, ...)."""

    def __init__(
        self,
        title: str,
        series: list[Series],
        fmt: Callable[[float], str],
        *,
        fixed_max: float | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.title = title
        self.series = series
        self._fmt = fmt
        self._fixed_max = fixed_max
        self._last_push = 0.0
        self._push_interval = 0.5
        self._animating = False
        self._scale = 0.0
        self.setMinimumHeight(110)

    def push(self, values: list[float]) -> None:
        now = time.monotonic()
        if self._last_push:
            # Track the real arrival rate: the dashboard refresh is a setting,
            # and a busy machine delivers late.
            gap = min(2.0, max(0.05, now - self._last_push))
            self._push_interval = self._push_interval * 0.7 + gap * 0.3
        self._last_push = now
        for value, serie in zip(values, self.series, strict=False):
            serie.samples.append(max(0.0, value))
        self._sync_animation()
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(260, 120)

    # Repaint on the shared 60fps ticker while on screen, so the curve scrolls
    # between pushes rather than jumping a whole sample every refresh - but
    # only while there is a curve to scroll. A graph of flat zeros paints
    # nothing, and animating nothing is what an idle GrabLine was doing.
    def _has_motion(self) -> bool:
        return self.isVisible() and any(any(s.samples) for s in self.series)

    def _sync_animation(self) -> None:
        wanted = self._has_motion()
        if wanted == self._animating:
            return
        self._animating = wanted
        if wanted:
            motion.ticker().tick.connect(self.update)
            motion.ticker().subscribe()
            return
        with contextlib.suppress(RuntimeError, TypeError):  # app teardown
            motion.ticker().tick.disconnect(self.update)
            motion.ticker().unsubscribe()

    def showEvent(self, event: object) -> None:
        super().showEvent(event)  # type: ignore[arg-type]
        self._sync_animation()

    def hideEvent(self, event: object) -> None:
        super().hideEvent(event)  # type: ignore[arg-type]
        self._sync_animation()

    def _scale_max(self) -> float:
        if self._fixed_max is not None:
            return self._fixed_max
        peak = max((max(s.samples, default=0.0) for s in self.series), default=0.0)
        self._scale = motion.ease_scale(self._scale, peak)
        return self._scale if self._scale > 0 else 1.0

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        text_color = self.palette().color(self.palette().ColorRole.WindowText)
        muted = QColor(text_color)
        muted.setAlpha(120)

        border = QColor(text_color)
        border.setAlpha(40)
        frame = self.rect().adjusted(1, 1, -2, -2)
        painter.setPen(QPen(border, 1))
        painter.drawRect(frame)

        plot = frame.adjusted(6, 20, -6, -6)
        scale = self._scale_max()
        if plot.width() > 4 and plot.height() > 4:
            step = plot.width() / (_HISTORY - 1)
            frac = 1.0
            if self._last_push:
                frac = min(1.0, (time.monotonic() - self._last_push) / self._push_interval)
            # A whole sample of travel, paid off smoothly until the next push.
            newest_x = plot.left() + plot.width() + (1.0 - frac) * step
            painter.setClipRect(plot)
            for serie in self.series:
                if len(serie.samples) < 2:
                    continue
                baseline = plot.bottom()
                last = len(serie.samples) - 1
                points = [
                    QPointF(
                        newest_x - (last - i) * step,
                        baseline - min(1.0, value / scale) * plot.height() * motion.GRAPH_HEADROOM,
                    )
                    for i, value in enumerate(serie.samples)
                ]
                fill = QColor(serie.color)
                fill.setAlpha(45)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(fill)
                painter.drawPolygon(
                    QPolygonF(
                        [
                            QPointF(points[0].x(), baseline),
                            *points,
                            QPointF(points[-1].x(), baseline),
                        ]
                    )
                )
                painter.setPen(QPen(serie.color, 1.5))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPolyline(QPolygonF(points))
            painter.setClipping(False)  # the title and readout live outside the plot

        painter.setPen(text_color)
        painter.drawText(
            frame.adjusted(6, 3, -6, 0),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop),
            self.title,
        )
        # Latest value(s) in the top-right, colored per series.
        readouts = " · ".join(self._fmt(s.samples[-1]) for s in self.series if s.samples)
        painter.setPen(muted)
        painter.drawText(
            frame.adjusted(6, 3, -6, 0),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop),
            readouts,
        )
        painter.end()
