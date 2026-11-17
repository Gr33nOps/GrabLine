"""Settings -> Phone: pair Android phones, list and remove them, turn the phone
remote on or off.

The page talks to ``app.remote.service.RemoteService`` only through its public
API and Qt signals. The pairing code lives in the pairing dialog and nowhere
else: it is never logged, stored or put in an accessible description.
"""

from __future__ import annotations

import io
import time
from typing import Any

from PySide6.QtCore import QDateTime, QLocale, Qt, QTimer
from PySide6.QtGui import QFont, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import t
from app.ui import chrome, components, design, theme

_QR_TARGET_PX = 260
_QUIET_ZONE_MODULES = 4


def qr_pixmap(uri: str, target: int = _QR_TARGET_PX) -> QPixmap:
    """The pairing QR as dark modules on white with a quiet zone, whole-pixel
    scaled to about ``target`` px so it stays crisp and scans in dark theme."""
    import segno

    qr = segno.make(uri, error="m")
    modules = qr.symbol_size(scale=1, border=_QUIET_ZONE_MODULES)[0]
    scale = max(1, target // modules)
    buffer = io.BytesIO()
    qr.save(
        buffer, kind="png", scale=scale, border=_QUIET_ZONE_MODULES, dark="#000000", light="#ffffff"
    )
    pixmap = QPixmap()
    pixmap.loadFromData(buffer.getvalue(), "PNG")
    return pixmap


def _wrapping(label: QLabel) -> QLabel:
    label.setWordWrap(True)
    label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    label.setMinimumWidth(0)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def _mono_font(point_size: int) -> QFont:
    font = QFont("IBM Plex Mono")
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setFixedPitch(True)
    font.setPointSize(point_size)
    return font


def _countdown_text(seconds: int) -> str:
    seconds = max(0, seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


class PairingDialog(chrome.Dialog):
    """Shows the QR code and manual code while a pairing is open. Not modal;
    closing it cancels the pairing."""

    def __init__(self, service: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service = service
        self._active = False
        self._expires_at = 0
        self.setModal(False)
        self.setWindowTitle(t("Pair a phone"))
        self.setMinimumWidth(340)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self.instruction = _wrapping(
            components.role_label(t("Scan this code with GrabLine on your phone."), "muted")
        )
        layout.addWidget(self.instruction)

        # White card so the code scans the same in light and dark themes.
        self.qr_frame = QFrame()
        self.qr_frame.setStyleSheet("QFrame { background: #ffffff; border-radius: 8px; }")
        qr_layout = QVBoxLayout(self.qr_frame)
        qr_layout.setContentsMargins(8, 8, 8, 8)
        self.qr_label = QLabel()
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_label.setFixedSize(_QR_TARGET_PX + 16, _QR_TARGET_PX + 16)
        self.qr_label.setAccessibleName(t("Pairing QR code"))
        qr_layout.addWidget(self.qr_label)
        layout.addWidget(self.qr_frame, 0, Qt.AlignmentFlag.AlignHCenter)

        self.manual_caption = _wrapping(
            components.role_label(t("Can't scan? Type this into GrabLine on your phone:"), "muted")
        )
        layout.addWidget(self.manual_caption)
        self.manual_label = QLabel()
        self.manual_label.setFont(_mono_font(design.FONT["body"]))
        self.manual_label.setTextFormat(Qt.TextFormat.PlainText)
        self.manual_label.setWordWrap(True)
        self.manual_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.manual_label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.manual_label.setAccessibleName(t("Pair a phone"))
        layout.addWidget(self.manual_label)

        self.countdown_label = components.role_label("", "strong", bold=True)
        layout.addWidget(self.countdown_label)

        self.banner = _wrapping(QLabel(""))
        self.banner.setStyleSheet(f"color: {theme.current().warn};")
        self.banner.hide()
        layout.addWidget(self.banner)

        self.status_label = _wrapping(QLabel(""))
        self.status_label.hide()
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.new_code_button = components.accent_button(t("New code"))
        self.new_code_button.setAccessibleName(t("New code"))
        self.new_code_button.clicked.connect(self._new_code)
        self.new_code_button.hide()
        buttons.addWidget(self.new_code_button)
        self.close_button = QPushButton(t("Close"))
        self.close_button.setAccessibleName(t("Close"))
        self.close_button.clicked.connect(self.close)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

    # ------------------------------------------------------------ service

    def show_code(self, qr_uri: str, manual_text: str, expires_at_unix: int) -> None:
        self._active = True
        self._expires_at = expires_at_unix
        self.qr_label.setPixmap(qr_pixmap(qr_uri))
        self.manual_label.setText(manual_text)
        self.qr_frame.show()
        self.instruction.show()
        self.manual_caption.show()
        self.manual_label.show()
        self.countdown_label.show()
        self.banner.hide()
        self.status_label.hide()
        self.new_code_button.hide()
        self._tick()
        self._timer.start()

    def show_locked_warning(self) -> None:
        self.banner.setText(t("Too many wrong codes. Show a new code."))
        self.banner.show()
        self.new_code_button.show()

    def finished_with(self, outcome: str) -> None:
        """The service ended this pairing. The code is gone, so hide it."""
        self._active = False
        self._timer.stop()
        if outcome == "cancelled":
            return
        self._hide_code()
        if outcome == "approved":
            self._set_status(t("Phone paired. It can now control GrabLine."))
        elif outcome == "denied":
            self._set_status(t("Pairing cancelled"))
        elif outcome == "expired":
            self.countdown_label.setText(t("Code expired"))
            self.countdown_label.show()
            self.new_code_button.show()
        elif outcome == "locked":
            self.show_locked_warning()
        else:
            self._set_status(t("Pairing failed. Try again."))
            self.new_code_button.show()
        self._fit_to_content()

    # ------------------------------------------------------------- internal

    def _hide_code(self) -> None:
        self.qr_label.clear()
        self.manual_label.clear()
        self.qr_frame.hide()
        self.instruction.hide()
        self.manual_caption.hide()
        self.manual_label.hide()
        self.countdown_label.hide()

    def _fit_to_content(self) -> None:
        """Shrink to the remaining widgets once the QR area is hidden, so an end
        state is a small message rather than a tall empty dialog."""
        layout = self.layout()
        if layout is not None:
            layout.activate()
        self.adjustSize()

    def _set_status(self, text: str) -> None:
        self.banner.hide()
        self.status_label.setText(text)
        self.status_label.show()

    def _tick(self) -> None:
        remaining = int(self._expires_at - time.time() + 0.999)
        if remaining <= 0:
            self._timer.stop()
            self._expire()
            return
        self.countdown_label.setText(t("Code expires in {time}", time=_countdown_text(remaining)))

    def _expire(self) -> None:
        # Tell the service so the code stops working, then show the expired view.
        if self._active:
            self._service.cancel_pairing()
        self.finished_with("expired")

    def _new_code(self) -> None:
        self.new_code_button.hide()
        self._service.start_pairing()

    def done(self, result: int) -> None:
        self._timer.stop()
        if self._active:
            self._active = False
            self._service.cancel_pairing()
        super().done(result)


class PhonePage(QWidget):
    """The Settings "Phone" page."""

    def __init__(self, service: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service = service
        self._error: str | None = None
        self._listening = False
        self.pairing_dialog: PairingDialog | None = None
        self.allow_box: QMessageBox | None = None
        self._allow_button: QPushButton | None = None
        self._announced_pairing = False

        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.setMinimumWidth(0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        layout.addWidget(
            _wrapping(
                components.role_label(
                    t("Control GrabLine from your Android phone on the same Wi-Fi."),
                    "muted",
                )
            )
        )

        pair_row = QHBoxLayout()
        self.pair_button = components.accent_button(t("Pair a phone"))
        self.pair_button.setAccessibleName(t("Pair a phone"))
        self.pair_button.clicked.connect(self._pair)
        pair_row.addWidget(self.pair_button)
        pair_row.addStretch(1)
        layout.addLayout(pair_row)

        layout.addWidget(components.SectionLabel(t("Paired phones")))
        self.devices_box = QWidget()
        self.devices_box.setAccessibleName(t("Paired phones"))
        self._devices_layout = QVBoxLayout(self.devices_box)
        self._devices_layout.setContentsMargins(0, 0, 0, 0)
        self._devices_layout.setSpacing(8)
        layout.addWidget(self.devices_box)

        self.enabled_check = QCheckBox(t("Phone remote"))
        self.enabled_check.setAccessibleName(t("Phone remote"))
        self.enabled_check.setChecked(bool(service.enabled_setting()))
        self.enabled_check.clicked.connect(self._toggle_enabled)
        layout.addWidget(self.enabled_check)

        self.status_label = _wrapping(QLabel(""))
        self.status_label.setAccessibleName(t("Phone remote"))
        layout.addWidget(self.status_label)

        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setText(t("Advanced"))
        self.advanced_toggle.setAccessibleName(t("Advanced"))
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        layout.addWidget(self.advanced_toggle, 0, Qt.AlignmentFlag.AlignLeft)

        self.advanced_box = QWidget()
        advanced_row = QHBoxLayout(self.advanced_box)
        advanced_row.setContentsMargins(18, 0, 0, 0)
        port_label = QLabel(t("Port:"))
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(int(service.port_setting()))
        self.port_spin.setAccessibleName(t("Port:"))
        self.port_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.port_spin.setMaximumWidth(110)
        self.port_spin.editingFinished.connect(self._apply_port)
        port_label.setBuddy(self.port_spin)
        advanced_row.addWidget(port_label)
        advanced_row.addWidget(self.port_spin)
        advanced_row.addStretch(1)
        self.advanced_box.hide()
        layout.addWidget(self.advanced_box)
        layout.addStretch(1)

        service.devicesChanged.connect(self.refresh)
        service.stateChanged.connect(self._on_state)
        service.errorOccurred.connect(self._on_error)
        service.pairingStarted.connect(self._on_pairing_started)
        service.pairingPending.connect(self._on_pairing_pending)
        service.pairingLockedWarning.connect(self._on_locked_warning)
        service.pairingFinished.connect(self._on_pairing_finished)

        self._listening = self._read_listening()
        self.refresh()

    # --------------------------------------------------------------- state

    def _read_listening(self) -> bool:
        try:
            if not self._service.listener_created:
                return False
            return bool(self._service.listener.running)
        except Exception:
            return False

    def _address_count(self) -> int:
        try:
            return len(self._service.listener.bound_addresses())
        except Exception:
            return 0

    def refresh(self) -> None:
        """Rebuild the paired-phone list and the status line."""
        while self._devices_layout.count():
            item = self._devices_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        devices = list(self._service.store.list())
        if not devices:
            empty = _wrapping(components.role_label(t("No phones are paired."), "muted"))
            self._devices_layout.addWidget(empty)
        for device in devices:
            self._devices_layout.addWidget(self._device_row(device))
        self._update_status()

    def _format_time(self, unix: int) -> str:
        return QLocale().toString(
            QDateTime.fromSecsSinceEpoch(int(unix)), QLocale.FormatType.ShortFormat
        )

    def _device_row(self, device: Any) -> QWidget:
        card = components.card_frame()
        row = QHBoxLayout(card)
        row.setContentsMargins(12, 8, 12, 8)
        text = QVBoxLayout()
        text.setSpacing(2)
        name = _wrapping(components.role_label(str(device.name), "strong", bold=True))
        text.addWidget(name)
        seen = (
            t("Last seen {when}", when=self._format_time(device.last_seen_at))
            if device.last_seen_at
            else t("Not connected yet")
        )
        detail = f"{t('Paired {date}', date=self._format_time(device.created_at))}  ·  {seen}"
        text.addWidget(_wrapping(components.role_label(detail, "muted", size=design.FONT["small"])))
        row.addLayout(text, 1)
        remove = QPushButton(t("Remove"))
        remove.setAccessibleName(f"{t('Remove')} {device.name}")
        remove.clicked.connect(lambda _checked=False, d=device: self._remove(d))
        row.addWidget(remove, 0, Qt.AlignmentFlag.AlignVCenter)
        return card

    def _update_status(self) -> None:
        if self._error == "portInUse":
            text = t(
                "Port {port} is in use. Choose another in Advanced.", port=self.port_spin.value()
            )
        elif self._error == "noAddress":
            text = t("This PC isn't on a private network.")
        elif self._error is not None:
            text = t("Phone remote could not start.")
        elif not self._service.enabled_setting():
            text = t("Phone remote is off.")
        elif self._listening:
            count = self._address_count()
            text = (
                t("Listening on 1 address.")
                if count == 1
                else t("Listening on {count} addresses.", count=count)
            )
        else:
            text = t("Nothing is listening until you pair a phone.")
        self.status_label.setText(text)

    def _on_state(self, running: bool) -> None:
        self._listening = bool(running)
        if running:
            self._error = None
        self._update_status()

    def _on_error(self, code: str) -> None:
        self._error = code or "error"
        self._update_status()

    # ------------------------------------------------------------- actions

    def _toggle_enabled(self, checked: bool) -> None:
        self._error = None
        self._service.set_enabled(bool(checked))
        self._update_status()

    def _toggle_advanced(self, shown: bool) -> None:
        self.advanced_box.setVisible(shown)
        self.advanced_toggle.setArrowType(
            Qt.ArrowType.DownArrow if shown else Qt.ArrowType.RightArrow
        )

    def _apply_port(self) -> None:
        port = self.port_spin.value()
        if port == self._service.port_setting():
            return
        self._error = None
        self._service.set_port(port)
        self._update_status()

    def _confirm_remove(self, name: str) -> bool:
        box = QMessageBox(self.window())
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(t("Remove"))
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(t("Remove {name}? It will stop working at once.", name=name))
        remove = box.addButton(t("Remove"), QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(t("Cancel"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        return box.clickedButton() is remove

    def _remove(self, device: Any) -> None:
        if not self._confirm_remove(str(device.name)):
            return
        self._service.remove_device(device.device_id)
        self.refresh()

    # ------------------------------------------------------------- pairing

    def _pair(self) -> None:
        self._error = None
        dialog = self.pairing_dialog
        if dialog is None:
            dialog = PairingDialog(self._service, self.window())
            dialog.finished.connect(self._on_dialog_closed)
            self.pairing_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        self._service.start_pairing()

    def _on_dialog_closed(self, _result: int) -> None:
        self.pairing_dialog = None
        self._dismiss_allow_box()

    def _on_pairing_started(self, qr_uri: str, manual_text: str, expires_at: int) -> None:
        dialog = self.pairing_dialog
        if dialog is not None:
            dialog.show_code(qr_uri, manual_text, int(expires_at))

    def _on_locked_warning(self) -> None:
        if self.pairing_dialog is not None:
            self.pairing_dialog.show_locked_warning()

    def _on_pairing_pending(self, device_name: str) -> None:
        self._dismiss_allow_box()
        box = QMessageBox(self.window())
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(t("Pair a phone"))
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(t('Allow "{name}" to control GrabLine?', name=device_name))
        allow = box.addButton(t("Allow"), QMessageBox.ButtonRole.AcceptRole)
        deny = box.addButton(t("Deny"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(deny)
        box.setEscapeButton(deny)
        allow.setAccessibleName(t("Allow"))
        deny.setAccessibleName(t("Deny"))
        box.setWindowModality(Qt.WindowModality.WindowModal)
        box.finished.connect(lambda _result, b=box: self._on_allow_answer(b))
        self.allow_box = box
        self._allow_button = allow
        box.open()

    def _on_allow_answer(self, box: QMessageBox) -> None:
        if self.allow_box is not box:
            return  # dismissed because the pairing ended on its own
        allowed = box.clickedButton() is self._allow_button
        self.allow_box = None
        self._allow_button = None
        box.deleteLater()
        if allowed:
            self._service.approve_pairing()
        else:
            self._service.deny_pairing()

    def _dismiss_allow_box(self) -> None:
        box = self.allow_box
        if box is None:
            return
        self.allow_box = None
        self._allow_button = None
        box.done(0)
        box.deleteLater()

    def _on_pairing_finished(self, outcome: str) -> None:
        if outcome not in ("approved", "denied"):
            self._dismiss_allow_box()
        dialog = self.pairing_dialog
        if dialog is not None:
            dialog.finished_with(outcome)
        if outcome == "approved":
            self.refresh()

    def closeEvent(self, event: Any) -> None:  # pragma: no cover - widget teardown
        if self.pairing_dialog is not None:
            self.pairing_dialog.close()
        super().closeEvent(event)


__all__ = ["PairingDialog", "PhonePage", "qr_pixmap"]
