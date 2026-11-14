"""Render the real embedded Settings view with explicit offscreen system fonts."""

from __future__ import annotations

import os
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from common import ROOT, case, save
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase, QTextLayout
from PySide6.QtWidgets import QScrollArea

from app.core import i18n
from app.core.settings import Settings
from app.db.database import Database
from app.tests.qt_app import ensure_app
from app.ui import theme, threads
from app.ui.settings_view import SettingsView

app = ensure_app()
# The offscreen plugin does not discover Windows fonts like the native plugin.
fonts = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "Fonts"
loaded = []
for name in ("segoeui.ttf", "msyh.ttc", "YuGothM.ttc", "malgun.ttf", "Nirmala.ttf"):
    path = fonts / name
    if path.exists():
        loaded.append({"file": name, "id": QFontDatabase.addApplicationFont(str(path))})
font = QFont()
font.setFamilies(["Segoe UI", "Microsoft YaHei", "Yu Gothic", "Malgun Gothic", "Nirmala UI"])
font.setPointSize(10)
app.setFont(font)
root = ROOT / "visual"
root.mkdir(exist_ok=True)
db = Database(root / "settings.db")
settings = Settings(db)


def capture(view, name, sections):
    paths = []
    for index in sections:
        view._list.setCurrentRow(index)
        app.processEvents()
        scroll = view._stack.currentWidget()
        assert isinstance(scroll, QScrollArea)
        assert scroll.horizontalScrollBar().maximum() == 0
        target = root / f"{name}-{index:02d}.png"
        assert view.grab().save(str(target))
        paths.append(str(target))
    return paths


try:
    for language, _, _ in i18n.available_languages():
        with case(
            "VISUAL-LANGUAGE-" + language.upper(), "offscreen embedded UI render"
        ) as evidence:
            i18n.set_language(language)
            settings.language = language
            app.setLayoutDirection(
                Qt.LayoutDirection.RightToLeft if i18n.is_rtl() else Qt.LayoutDirection.LeftToRight
            )
            theme.apply_theme(app, "light")
            sample = i18n.t("Settings")
            layout = QTextLayout(sample, app.font())
            layout.beginLayout()
            layout.createLine()
            layout.endLayout()
            assert all(g != 0 for run in layout.glyphRuns() for g in run.glyphIndexes()), sample
            view = SettingsView(settings, lambda: None)
            view.resize(1100, 800)
            view.show()
            app.processEvents()
            assert view._list.count() == 19
            evidence["screenshots"] = capture(view, language, [0, 4, 18])
            assert view.layoutDirection() == app.layoutDirection()
            view.close()
            view._dialog.close()
    i18n.set_language("en")
    app.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
    for mode in theme.THEMES:
        with case("VISUAL-SECTIONS-" + mode.upper(), "offscreen embedded UI render") as evidence:
            theme.apply_theme(app, mode)
            view = SettingsView(settings, lambda: None)
            view.resize(1040, 760)
            view.show()
            app.processEvents()
            evidence["screenshots"] = capture(view, mode, range(19))
            view.close()
            view._dialog.close()
    save("visual/fonts.json", loaded)
finally:
    db.close()
    threads.shutdown()
