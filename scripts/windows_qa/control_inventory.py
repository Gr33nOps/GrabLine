"""Inventory every exposed editable Settings field and its persisted property."""

from __future__ import annotations

import ast
import os
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
from common import ROOT, save
from PySide6.QtWidgets import QAbstractSpinBox, QCheckBox, QComboBox, QLineEdit, QPlainTextEdit

from app.core.settings import Settings
from app.db.database import Database
from app.tests.qt_app import ensure_app
from app.ui.settings_dialog import SettingsDialog
from app.ui.shortcuts import DEFAULTS

app = ensure_app()
db = Database(ROOT / "control-inventory.db")
settings = Settings(db)
dialog = SettingsDialog(settings)
tree = ast.parse(
    (Path(__file__).resolve().parents[2] / "app/ui/settings_dialog.py").read_text(encoding="utf-8")
)
wiring = {}
for node in ast.walk(tree):
    if not isinstance(node, ast.Assign):
        continue
    for target in node.targets:
        if not (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Attribute)
            and isinstance(target.value.value, ast.Name)
            and target.value.value.id == "self"
            and target.value.attr == "settings"
        ):
            continue
        for field in ast.walk(node.value):
            if (
                isinstance(field, ast.Attribute)
                and isinstance(field.value, ast.Name)
                and field.value.id == "self"
                and isinstance(
                    getattr(dialog, field.attr, None),
                    (QCheckBox, QComboBox, QLineEdit, QPlainTextEdit, QAbstractSpinBox),
                )
            ):
                wiring[field.attr] = target.attr
wiring["language_combo"] = "language"
wiring["watch_dir_edit"] = "torrent_watch_dir"
rows = []
for field, widget in vars(dialog).items():
    if not isinstance(widget, (QCheckBox, QComboBox, QLineEdit, QPlainTextEdit, QAbstractSpinBox)):
        continue
    section = next(
        (
            dialog.tabs.tabText(i)
            for i in range(dialog.tabs.count())
            if dialog.tabs.widget(i).isAncestorOf(widget)
        ),
        None,
    )
    prop = wiring.get(field)
    rows.append(
        {
            "field": field,
            "widget": widget.metaObject().className(),
            "section": section,
            "setting": prop,
            "expected_case": "QT-SETTING-" + prop.upper() if prop else None,
            "default": str(getattr(settings, prop, "")) if prop else None,
        }
    )
    if field == "autostart_check":
        rows[-1].update(setting="OS login registration", expected_case="VM-LOGIN")
for group in ("day_checks", "column_checks"):
    controls = getattr(dialog, group)
    values = controls.values() if isinstance(controls, dict) else controls
    for index, widget in enumerate(values):
        rows.append(
            {
                "field": group + "[" + str(index) + "]",
                "widget": widget.metaObject().className(),
                "section": next(
                    (
                        dialog.tabs.tabText(i)
                        for i in range(dialog.tabs.count())
                        if dialog.tabs.widget(i).isAncestorOf(widget)
                    ),
                    None,
                ),
                "setting": group,
                "expected_case": "QT-SETTING-DAYS-COLUMNS-QUIET",
            }
        )
for shortcut in DEFAULTS:
    rows.append(
        {
            "field": "shortcut." + shortcut.id,
            "widget": "QKeySequenceEdit",
            "section": "Shortcuts",
            "setting": "shortcuts." + shortcut.id,
            "expected_case": "QT-SHORTCUT-" + shortcut.id.upper().replace(".", "-"),
            "default": shortcut.default,
        }
    )
save("editable-control-inventory.json", rows)
print("Fields", len(rows), "sections", dialog.tabs.count())
existing = {p.stem for p in (ROOT / "cases").glob("*.json")}
print(
    "Missing direct case IDs",
    [
        (r["field"], r["setting"], r["expected_case"])
        for r in rows
        if r["expected_case"] not in existing
    ],
)
dialog.reject()
db.close()
