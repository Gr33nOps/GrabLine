"""In-app light/dark theme, driven by the design system (app.ui.design).

"light" and "dark" apply the GrabLine palette + global stylesheet. "system"
follows the OS: it picks light or dark from the platform's color scheme (and
keeps the same GrabLine look, rather than falling back to a bare Qt style, so
the app is visually consistent everywhere).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication

from app.ui import design

THEMES = ("system", "light", "dark")

#: The palette currently applied: widgets that custom-paint read this.
_current: design.Palette = design.LIGHT


def current() -> design.Palette:
    """The active token palette (for custom-painted widgets)."""
    return _current


def is_dark() -> bool:
    return _current.dark


def _system_prefers_dark(app: QApplication) -> bool:
    hints = app.styleHints()
    try:
        return hints.colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:  # pragma: no cover - very old Qt
        # Fallback: infer from the default palette's window lightness.
        return app.palette().color(QPalette.ColorRole.Window).lightness() < 128


#: Kept for API compatibility; the design system replaces the platform default.
def remember_default(app: QApplication) -> None:
    """No-op retained for callers/tests; the design system owns the look."""


def apply_theme(app: QApplication, mode: str, accent: str | None = None) -> None:
    """Apply "system", "light", or "dark" - optionally re-tinted around an
    ``accent`` hex (Settings → Appearance; None/"" = the brand blue)."""
    global _current
    if mode not in THEMES:
        mode = "system"
    dark = _system_prefers_dark(app) if mode == "system" else mode == "dark"
    palette = design.DARK if dark else design.LIGHT
    if accent:
        palette = design.with_accent(palette, accent)
    _current = palette
    app.setStyle("Fusion")  # a consistent base for the stylesheet everywhere
    app.setPalette(design.qpalette(_current))
    app.setStyleSheet(design.stylesheet(_current))
