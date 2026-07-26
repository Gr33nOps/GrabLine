"""The keyboard-shortcut registry, persistence, and wiring.

These guard the invariants that keep the shortcut table honest: no two default
keys collide, every registered action has a handler on the window (and vice
versa), and user overrides round-trip and win over the defaults.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication, QWidget

from app.core.manager import DownloadManager
from app.core.settings import Settings
from app.db.database import Database
from app.ui.main_window import MainWindow
from app.ui.shortcuts import DEFAULTS, ShortcutManager, by_category, conflicts, normalize


def _qapp() -> QApplication:
    instance = QApplication.instance()
    return instance if isinstance(instance, QApplication) else QApplication([])


def test_ids_are_unique():
    ids = [shortcut.id for shortcut in DEFAULTS]
    assert len(ids) == len(set(ids))


def test_every_default_parses():
    _qapp()
    for shortcut in DEFAULTS:
        assert not QKeySequence(shortcut.default).isEmpty(), shortcut.id
        assert normalize(shortcut.default), shortcut.id


def test_no_default_key_collisions():
    """The core invariant: shipping two actions on one key would make Qt fire
    an ambiguous shortcut and neither would work. Scope is ignored on purpose -
    an app-wide key still shadows a list-scoped one when the table has focus."""
    _qapp()
    assert conflicts({shortcut.id: shortcut.default for shortcut in DEFAULTS}) == {}


def test_by_category_covers_every_shortcut():
    grouped = {shortcut.id for _name, items in by_category() for shortcut in items}
    assert grouped == {shortcut.id for shortcut in DEFAULTS}


def test_settings_overrides_round_trip(db: Database):
    settings = Settings(db)
    assert settings.shortcuts == {}
    settings.shortcuts = {"download.add": "Ctrl+Shift+D", "view.theme": ""}
    assert settings.shortcuts == {"download.add": "Ctrl+Shift+D", "view.theme": ""}


def test_reset_restores_default_keys(db: Database):
    """Overrides are ordinary settings, so a full reset drops them."""
    settings = Settings(db)
    settings.shortcuts = {"download.add": "Ctrl+Shift+D"}
    settings.reset()
    assert settings.shortcuts == {}


def test_manager_effective_merges_and_unbinds(db: Database):
    _qapp()
    settings = Settings(db)
    settings.shortcuts = {"download.add": "Ctrl+Shift+D", "view.theme": ""}
    manager = ShortcutManager(QWidget(), settings, {}, None)
    effective = manager.effective()
    assert effective["download.add"] == normalize("Ctrl+Shift+D")  # override wins
    assert effective["view.theme"] == ""  # explicit unbind
    assert effective["download.paste"] == normalize("Ctrl+V")  # untouched default


def test_conflicts_flags_a_user_collision():
    _qapp()
    bindings = {shortcut.id: shortcut.default for shortcut in DEFAULTS}
    bindings["view.theme"] = bindings["download.add"]  # both now on Ctrl+N
    clashes = conflicts(bindings)
    assert normalize("Ctrl+N") in clashes
    assert set(clashes[normalize("Ctrl+N")]) == {"download.add", "view.theme"}


def test_cheatsheet_builds_and_filters(db: Database):
    from app.ui.shortcuts_dialog import ShortcutsDialog

    _qapp()
    manager = ShortcutManager(QWidget(), Settings(db), {}, None)
    dialog = ShortcutsDialog(manager.effective())
    assert len(dialog._rows) == len(DEFAULTS)  # every registry row is shown
    dialog._filter("theme")  # narrows toward the theme toggle
    shown = [row for row, _haystack in dialog._rows if not row.isHidden()]
    assert 1 <= len(shown) < len(DEFAULTS)


def test_window_action_map_matches_registry(db: Database):
    """Every registered shortcut has a handler on the window, and every handler
    the window registers is a real registry id - no dangling either way."""
    _qapp()
    settings = Settings(db)
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        window = MainWindow(manager, settings)
        assert window._shortcuts.action_ids == {shortcut.id for shortcut in DEFAULTS}
    finally:
        manager.shutdown()


def test_window_rebind_is_live(db: Database, tmp_path: Path):
    """A saved override plus reload() changes the effective binding without a
    restart (and installing the QShortcuts does not raise)."""
    _qapp()
    settings = Settings(db)
    settings.download_dir = tmp_path
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    try:
        window = MainWindow(manager, settings)
        assert window._shortcuts.effective()["download.add"] == normalize("Ctrl+N")
        settings.shortcuts = {"download.add": "Ctrl+Shift+D"}
        window._shortcuts.reload()
        assert window._shortcuts.effective()["download.add"] == normalize("Ctrl+Shift+D")
    finally:
        manager.shutdown()
