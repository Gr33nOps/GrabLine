#!/usr/bin/env python3
"""Render the Grabline icon to the PNG sizes the extension manifest needs.

Run from the repo root with the project venv (PySide6 offscreen):
    QT_QPA_PLATFORM=offscreen python scripts/render_extension_icons.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

from app.ui.icon import make_app_icon

SIZES = (16, 32, 48, 128)
TARGET = Path(__file__).resolve().parents[1] / "extension" / "icons"


def main() -> int:
    QApplication([])
    TARGET.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        icon = make_app_icon(size)
        path = TARGET / f"icon{size}.png"
        icon.pixmap(size, size).save(str(path), "PNG")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
