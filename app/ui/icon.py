"""The GrabLine app icon: the brand logo (a hand gripping a download arrow),
loaded from the shipped asset, with the original painted glyph as a fallback
so a broken install still shows *something* recognizable.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap


def logo_pixmap() -> QPixmap | None:
    """The full-square brand logo, or None if the asset is missing."""
    candidates = (
        Path(__file__).resolve().parent / "assets" / "logo.png",  # source checkout
        Path(getattr(sys, "_MEIPASS", "")) / "app" / "ui" / "assets" / "logo.png",  # frozen
    )
    for path in candidates:
        if path.is_file():
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                return pixmap
    return None


def make_app_icon(size: int = 64) -> QIcon:
    logo = logo_pixmap()
    if logo is not None:
        # Pre-scale every size the OS may ask for (taskbar 16-32, alt-tab 48,
        # desktop 64-256…) with smooth filtering. A single-size QIcon leaves
        # the scaling to the platform at draw time, which is what made the
        # icon look blurred on the desktop and in the taskbar.
        icon = QIcon()
        for edge in (16, 20, 24, 32, 40, 48, 64, 96, 128, 256, 512):
            icon.addPixmap(
                logo.scaled(
                    edge,
                    edge,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        return icon
    return _painted_icon(size)


def _painted_icon(size: int) -> QIcon:
    """Fallback: the original in-code download glyph on a blue disc."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#0170fd"))
    margin = size * 0.06
    painter.drawEllipse(QPointF(size / 2, size / 2), size / 2 - margin, size / 2 - margin)

    pen = QPen(QColor("white"), size * 0.10)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    # Arrow shaft, arrow head, and a base line: the classic download glyph.
    painter.drawLine(QPointF(size * 0.50, size * 0.24), QPointF(size * 0.50, size * 0.58))
    painter.drawPolyline(
        [
            QPointF(size * 0.34, size * 0.46),
            QPointF(size * 0.50, size * 0.62),
            QPointF(size * 0.66, size * 0.46),
        ]
    )
    painter.drawLine(QPointF(size * 0.32, size * 0.76), QPointF(size * 0.68, size * 0.76))
    painter.end()
    return QIcon(pixmap)
