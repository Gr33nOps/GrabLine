"""Actual account dialogs and disposable Windows vault entries for local QA."""

from __future__ import annotations

import atexit
import os
import uuid

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import keyring
from common import record
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialogButtonBox

from app.core import credentials
from app.tests.qt_app import ensure_app
from app.ui.cloud_dialog import CloudAccountsDialog, CloudFolderDialog, _AccountEditor

app = ensure_app()
credentials._KEYRING_SERVICE = "grabline-windows-qa-" + uuid.uuid4().hex
tokens = set()


def cleanup():
    errors = []
    for token in tokens:
        try:
            if keyring.get_password(credentials._KEYRING_SERVICE, token) is not None:
                keyring.delete_password(credentials._KEYRING_SERVICE, token)
            assert keyring.get_password(credentials._KEYRING_SERVICE, token) is None
        except Exception as exc:
            errors.append(str(exc))
    record(
        "VAULT-CLEANUP-" + credentials._KEYRING_SERVICE[-8:],
        "FAIL" if errors else "PASS",
        "Windows Credential Manager",
        errors or "Every disposable secret removed",
        entries=len(tokens),
    )


atexit.register(cleanup)


def ui_save(store, account, secret):
    dialog = CloudAccountsDialog(store)
    errors = []

    def fill():
        try:
            editor = app.activeModalWidget()
            assert isinstance(editor, _AccountEditor), type(editor)
            editor.service.setCurrentText(account.service)
            for field, value in {
                "host": account.host,
                "username": account.username,
                "port": str(account.port),
                "key_file": account.key_file,
                "endpoint": account.endpoint_url,
                "label": account.label,
                "secret": secret or "",
            }.items():
                getattr(editor, field).setText(value)
            box = editor.findChild(QDialogButtonBox)
            QTest.mouseClick(
                box.button(QDialogButtonBox.StandardButton.Ok), Qt.MouseButton.LeftButton
            )
        except Exception as exc:
            errors.append(str(exc))
            if app.activeModalWidget():
                app.activeModalWidget().reject()

    QTimer.singleShot(0, fill)
    dialog._add()
    assert not errors, errors
    saved = store.account_for(account.service, account.host, account.username)
    assert saved is not None
    tokens.add(saved.token())
    assert store.secret_for(saved) == secret
    assert dialog.list.count() > 0
    dialog.reject()


def ui_pick(folder_url, files):
    """Exercise the real picker against listing objects from an authenticated server."""
    panel = CloudFolderDialog(folder_url, files)
    panel.show()
    app.processEvents()
    assert panel.selected_urls() == [f.url for f in files]
    for index in range(panel.tree.topLevelItemCount()):
        panel.tree.topLevelItem(index).setCheckState(0, Qt.CheckState.Unchecked)
    assert panel.selected_urls() == []
    panel.tree.topLevelItem(0).setCheckState(0, Qt.CheckState.Checked)
    selected = panel.selected_urls()
    box = panel.findChild(QDialogButtonBox)
    QTest.mouseClick(box.button(QDialogButtonBox.StandardButton.Ok), Qt.MouseButton.LeftButton)
    assert panel.result() == panel.DialogCode.Accepted and selected == [files[0].url]
    cancelled = CloudFolderDialog(folder_url, files)
    cancelled.show()
    QTest.mouseClick(
        cancelled.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Cancel),
        Qt.MouseButton.LeftButton,
    )
    assert cancelled.result() == cancelled.DialogCode.Rejected
    return selected


def ui_remove(store, account):
    panel = CloudAccountsDialog(store)
    panel.list.setCurrentRow(0)
    panel._remove()
    assert panel.list.count() == 0
    assert store.account_for(account.service, account.host, account.username) is None
    assert keyring.get_password(credentials._KEYRING_SERVICE, account.token()) is None
    panel.reject()
