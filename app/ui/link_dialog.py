"""The "Link to another PC" dialog (PROTOCOL.md section 21).

The person types the address and the code that GrabLine on the other PC shows
under "Pair a phone or PC". The code is cleaned the way the phone cleans it
(spaces, dashes, case, look-alike letters) and checked here before anything is
sent. While the other PC asks its user to press Allow the dialog says
"Confirm on the other PC".

The dialog talks to ``app.remote.client.service.LinkService`` only through its
public API and Qt signals. The code is typed into a field, handed to the
service, and cleared the moment the attempt ends; it is never logged, stored or
put in an accessible description.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import N_, t
from app.ui import chrome, components, design

#: ``linkFinished`` code -> the words for it (plain text, never rich text).
_FAILURES = {
    "invalidCode": N_("That code doesn't look right"),
    "badAddress": N_("That address doesn't look right"),
    "badCode": N_("That code didn't match"),
    "proofMismatch": N_("That code didn't match"),
    "locked": N_("Too many wrong codes. Show a new code on the other PC"),
    "expired": N_("Code expired. Show a new code on the other PC"),
    "declined": N_("The other PC declined"),
    "notFound": N_("PC not found"),
    "timeout": N_("The other PC didn't answer"),
    "connectionLost": N_("The connection was lost. Try again"),
    "full": N_("That PC already has 8 devices linked"),
    "hostTooOld": N_("Update GrabLine on the other PC"),
    "clientTooOld": N_("Update GrabLine on this PC"),
    "busy": N_("Linking is already in progress"),
}


def failure_text(code: str) -> str:
    """The words for a failed link attempt; unknown codes get the generic line."""
    return t(_FAILURES.get(code, N_("Pairing failed. Try again.")))


def _wrapping(label: QLabel) -> QLabel:
    label.setWordWrap(True)
    label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    label.setMinimumWidth(0)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def _mono(size: int) -> QFont:
    font = QFont("IBM Plex Mono")
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setFixedPitch(True)
    font.setPointSize(size)
    return font


class LinkDialog(chrome.Dialog):
    """Not modal: GrabLine keeps working while the other PC's person answers."""

    def __init__(self, links: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._links = links
        self._busy = False
        self._done = False
        self.setModal(False)
        self.setWindowTitle(t("Link to another PC"))
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        self.instruction = _wrapping(
            components.role_label(
                t(
                    "On the other PC, open GrabLine, go to Devices and choose "
                    '"Pair a phone or PC". Then type the address and the code it shows.'
                ),
                "muted",
            )
        )
        layout.addWidget(self.instruction)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        self.address_edit = QLineEdit()
        self.address_edit.setPlaceholderText("192.168.1.20:47821")
        self.address_edit.setAccessibleName(t("Address"))
        self.address_edit.setClearButtonEnabled(True)
        self.address_edit.setFont(_mono(design.FONT["body"]))
        self.address_edit.textChanged.connect(self._update_enabled)
        self.address_edit.returnPressed.connect(self._submit)
        address_label = QLabel(t("Address"))
        address_label.setBuddy(self.address_edit)
        form.addRow(address_label, self.address_edit)
        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText("XXXX-XXXX-XXXX")
        self.code_edit.setAccessibleName(t("Code"))
        self.code_edit.setMaxLength(32)
        self.code_edit.setFont(_mono(design.FONT["body"]))
        self.code_edit.textChanged.connect(self._update_enabled)
        self.code_edit.returnPressed.connect(self._submit)
        code_label = QLabel(t("Code"))
        code_label.setBuddy(self.code_edit)
        form.addRow(code_label, self.code_edit)
        layout.addLayout(form)

        self.status_label = _wrapping(components.role_label("", "dim"))
        self.status_label.setAccessibleName(t("Link to another PC"))
        self.status_label.hide()
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.link_button = components.accent_button(t("Link"))
        self.link_button.setAccessibleName(t("Link"))
        self.link_button.clicked.connect(self._submit)
        buttons.addWidget(self.link_button)
        self.close_button = QPushButton(t("Cancel"))
        self.close_button.setAccessibleName(t("Cancel"))
        self.close_button.clicked.connect(self.close)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        links.linkProgress.connect(self._on_progress)
        links.linkFinished.connect(self._on_finished)
        self._update_enabled()

    # ---------------------------------------------------------------- input

    def _update_enabled(self) -> None:
        ready = bool(self.address_edit.text().strip() and self.code_edit.text().strip())
        self.link_button.setEnabled(ready and not self._busy and not self._done)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.address_edit.setEnabled(not busy)
        self.code_edit.setEnabled(not busy)
        self._update_enabled()

    def _show(self, text: str, role: str) -> None:
        self.status_label.setProperty("role", role)
        style = self.status_label.style()
        style.unpolish(self.status_label)
        style.polish(self.status_label)
        self.status_label.setText(text)
        self.status_label.setAccessibleName(text)
        self.status_label.show()

    def _submit(self) -> None:
        if self._busy or self._done or not self.link_button.isEnabled():
            return
        self._set_busy(True)
        self._show(t("Connecting…"), "dim")
        started = self._links.add_link(self.address_edit.text(), self.code_edit.text())
        if started is False:
            return  # the service answers through linkFinished, at once

    # -------------------------------------------------------------- service

    def _on_progress(self, step: str) -> None:
        if not self._busy:
            return
        if step == "confirm":
            self._show(t("Confirm on the other PC"), "strong")
        elif step in ("connecting", "sending"):
            self._show(t("Connecting…"), "dim")

    def _on_finished(self, outcome: str, pc_id: str) -> None:
        if not self._busy:
            return
        self._set_busy(False)
        if outcome == "ok":
            info = self._links.link(pc_id)
            name = str(info.name) if info is not None else ""
            self._done = True
            self.code_edit.clear()  # the code is of no use any more
            self._show(t("Linked to {name}.", name=name), "ok")
            self.link_button.hide()
            self.close_button.setText(t("Close"))
            self.close_button.setAccessibleName(t("Close"))
            self.address_edit.setEnabled(False)
            self.code_edit.setEnabled(False)
            return
        self._show(failure_text(outcome), "warn")
        self.code_edit.selectAll()
        self.code_edit.setFocus()

    def done(self, result: int) -> None:
        if self._busy:
            self._links.cancel_add()
        self.code_edit.clear()
        super().done(result)


__all__ = ["LinkDialog", "failure_text"]
