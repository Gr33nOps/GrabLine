"""Keep the test QApplication alive across GUI tests.

Creating one in a short-lived local variable lets its Python wrapper destroy
Qt's shared application while widgets and deferred-delete events still exist.
That can abort pytest after every assertion has already passed.
"""

from __future__ import annotations

from PySide6.QtWidgets import QApplication

_APP: QApplication | None = None


def ensure_app() -> QApplication:
    global _APP
    instance = QApplication.instance()
    _APP = instance if isinstance(instance, QApplication) else QApplication([])
    return _APP
