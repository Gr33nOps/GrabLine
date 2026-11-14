"""Qt scaling/density matrix without changing host display configuration."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from common import ROOT, case

parser = argparse.ArgumentParser()
parser.add_argument("--factor", type=float)
args = parser.parse_args()
if args.factor is None:
    for factor in (1.0, 1.25, 1.5, 2.0):
        env = {
            **os.environ,
            "QT_QPA_PLATFORM": "offscreen",
            "QT_SCALE_FACTOR": str(factor),
            "LOCALAPPDATA": str(ROOT / "scale-profile"),
        }
        result = subprocess.run(
            [sys.executable, __file__, "--factor", str(factor)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        print(result.stdout, end="", flush=True)
        assert result.returncode == 0, result.stderr
else:
    from pathlib import Path

    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import QScrollArea

    from app.core.manager import DownloadManager
    from app.core.settings import Settings
    from app.db.database import Database
    from app.tests.qt_app import ensure_app
    from app.ui import threads
    from app.ui.main_window import MainWindow
    from app.ui.settings_view import SettingsView

    app = ensure_app()
    QFontDatabase.addApplicationFont(str(Path(os.environ["SYSTEMROOT"]) / "Fonts/segoeui.ttf"))
    app.setFont(QFont("Segoe UI", 10))
    root = ROOT / "scaling" / str(args.factor)
    root.mkdir(parents=True, exist_ok=True)
    db = Database(root / "test.db")
    settings = Settings(db)
    settings.download_dir = root
    settings.check_updates = False
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        for density in ("comfortable", "compact"):
            with case(
                f"VISUAL-SCALE-{args.factor}-{density.upper()}",
                "offscreen Qt device scaling + actual density effect",
            ) as evidence:
                settings.ui_density = density
                window = MainWindow(manager, settings)
                window.resize(1040, 760)
                window.show()
                app.processEvents()
                assert window.table.verticalHeader().defaultSectionSize() == (
                    26 if density == "compact" else 34
                )
                ratio = window.devicePixelRatioF()
                assert abs(ratio - args.factor) < 0.02, ratio
                image = window.grab()
                assert abs(image.width() - 1040 * args.factor) <= 1
                main = root / (density + "-downloads.png")
                image.save(str(main))
                view = SettingsView(settings, lambda: None)
                view.resize(1040, 760)
                view.show()
                screenshots = [str(main)]
                for index in range(19):
                    view._list.setCurrentRow(index)
                    app.processEvents()
                    scroll = view._stack.currentWidget()
                    assert isinstance(scroll, QScrollArea)
                    assert scroll.horizontalScrollBar().maximum() == 0, (index, density, ratio)
                    if index in (0, 4, 18):
                        image_path = root / f"{density}-settings-{index}.png"
                        view.grab().save(str(image_path))
                        screenshots.append(str(image_path))
                evidence.update(
                    ratio=ratio,
                    density=density,
                    screenshots=screenshots,
                    sections_checked=19,
                    host_display_settings_changed=False,
                )
                window.shutdown()
                window.hide()
                view.close()
                view._dialog.close()
    finally:
        manager.shutdown()
        db.close()
        threads.shutdown()
