"""Settings -> Phone page, and the pairing dialog and Allow prompt that the
Devices page hosts, driven with a fake service that has the real service's API
and real Qt signals. (The Devices page itself: test_devices_page.py.)"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import cast

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMessageBox, QPushButton

from app.tests.phone_fakes import FakeService, device
from app.tests.qt_app import ensure_app
from app.ui import phone_page
from app.ui.devices_page import DevicesPage
from app.ui.phone_page import PairingDialog, PhonePage

QR_URI = "grabline://pair?v=1&a=192.168.1.20&p=47821&c=123456"
MANUAL = "192.168.1.20:47821  code 123456"


@pytest.fixture()
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeService:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    ensure_app()
    return FakeService([device("d1", "Pixel 8", last_seen=1_700_000_500), device("d2", "Galaxy S")])


def _names(page: DevicesPage) -> list[str]:
    return [
        str(page.device_list.item(i).data(Qt.ItemDataRole.AccessibleTextRole)).split(",")[0]
        for i in range(page.device_list.count())
    ]


# ------------------------------------------------------- the Settings page


def test_settings_page_keeps_only_the_switch_port_and_a_link_to_devices(
    service: FakeService,
) -> None:
    opened: list[str] = []
    page = PhonePage(service, open_devices=lambda: opened.append("devices"))
    # No second copy of the phone list or the Pair button.
    assert not hasattr(page, "pair_button") and not hasattr(page, "devices_box")
    assert [b.text() for b in page.findChildren(QPushButton)] == [
        "Pair and manage phones in Devices"
    ]
    texts = [label.text() for label in page.findChildren(QLabel)]
    assert "Paired phones are listed in Devices." in texts
    assert "Pixel 8" not in texts
    button = page.devices_button
    assert button.accessibleName() == "Pair and manage phones in Devices"
    button.click()
    assert opened == ["devices"]
    assert "start_pairing" not in service.names()


def test_settings_page_without_a_devices_page_disables_the_button(
    service: FakeService,
) -> None:
    assert not PhonePage(service).devices_button.isEnabled()


def test_switch_and_port(service: FakeService) -> None:
    page = PhonePage(service)
    assert page.enabled_check.isChecked()
    page.enabled_check.click()
    assert ("set_enabled", False) in service.calls
    assert "off" in page.status_label.text()
    assert page.advanced_box.isHidden()
    page.advanced_toggle.setChecked(True)
    assert not page.advanced_box.isHidden()
    page.port_spin.setValue(50000)
    page.port_spin.editingFinished.emit()
    assert ("set_port", 50000) in service.calls
    assert page.port_spin.minimum() == 1024
    assert page.port_spin.maximum() == 65535


def test_error_text_in_plain_words(service: FakeService) -> None:
    page = PhonePage(service)
    service.errorOccurred.emit("portInUse")
    assert page.status_label.text() == "Port 47821 is in use. Choose another in Advanced."
    service.errorOccurred.emit("noAddress")
    assert page.status_label.text() == "This PC isn't on a private network."
    service.errorOccurred.emit("identity")
    assert "could not start" in page.status_label.text()
    service.stateChanged.emit(True)
    assert "Listening on" in page.status_label.text()


def test_phone_intro_does_not_repeat_the_status_line(service: FakeService) -> None:
    page = PhonePage(service)
    texts = [label.text() for label in page.findChildren(QLabel)]
    intro = [text for text in texts if text.startswith("Control GrabLine")]
    assert intro == ["Control GrabLine from your Android phone on the same Wi-Fi."]


def test_settings_view_hosts_phone_page(
    service: FakeService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.settings import Settings
    from app.db.database import Database
    from app.ui.settings_view import SettingsView

    db = Database(tmp_path / "grabline-test.db")
    try:
        opened: list[str] = []
        view = SettingsView(
            Settings(db),
            lambda: None,
            remote_service=service,
            open_devices=lambda: opened.append("d"),
        )
        titles = [view._list.item(i).text() for i in range(view._list.count())]
        assert "Phone" in titles
        # Second section, right after General, so it is easy to find.
        assert titles.index("Phone") == 1
        assert titles[-1] == "About"
        view._list.setCurrentRow(1)  # sections are built when first opened
        phone = view.findChild(PhonePage)
        assert phone is not None
        phone.devices_button.click()
        assert opened == ["d"]
        without = SettingsView(Settings(db), lambda: None)
        assert without._list.count() == view._list.count() - 1
    finally:
        db.close()


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_disclosure_toggle_is_flat_with_visible_focus(service: FakeService, mode: str) -> None:
    from app.ui import theme

    app = ensure_app()
    theme.apply_theme(app, mode)
    try:
        sheet = app.styleSheet()
        assert "QToolButton {" in sheet
        block = sheet.split("QToolButton {", 1)[1].split("}", 1)[0]
        assert "border: 1px solid transparent" in block  # no native frame
        assert "QToolButton:hover" in sheet and "QToolButton:focus" in sheet
    finally:
        theme.apply_theme(app, "light")


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_port_value_is_not_clipped(service: FakeService, mode: str) -> None:
    from app.ui import theme

    app = ensure_app()
    theme.apply_theme(app, mode)
    try:
        page = PhonePage(service)
        spin = page.port_spin
        assert spin.buttonSymbols() != spin.ButtonSymbols.NoButtons
        text_width = spin.fontMetrics().horizontalAdvance("65535")
        assert spin.minimumWidth() >= text_width + 40
        assert spin.sizeHint().width() >= text_width + 40
        # The inner line edit must not inherit the framed QLineEdit padding.
        assert "QAbstractSpinBox QLineEdit" in app.styleSheet()
    finally:
        theme.apply_theme(app, "light")


# --------------------------------------- pairing (now on the Devices page)


def test_pairing_started_shows_qr_and_countdown(service: FakeService) -> None:
    page = DevicesPage(service)
    page.pair_button.click()
    assert "start_pairing" in service.names()
    dialog = page.pairing_dialog
    assert dialog is not None
    service.pairingStarted.emit(QR_URI, MANUAL, int(time.time()) + 102)
    pixmap = dialog.qr_label.pixmap()
    assert not pixmap.isNull()
    assert 200 <= pixmap.width() <= 280
    assert dialog.manual_label.text() == MANUAL
    assert dialog.countdown_label.text().startswith("Code expires in 1:")
    assert dialog.new_code_button.isHidden()
    # Dark modules on white: the quiet zone corner is white, the finder corner dark.
    image = pixmap.toImage()
    assert image.pixelColor(0, 0).name() == "#ffffff"
    assert any(
        image.pixelColor(x, y).name() == "#000000"
        for x in range(0, image.width(), 4)
        for y in range(0, image.height(), 4)
    )


def test_closing_the_dialog_cancels_pairing(service: FakeService) -> None:
    page = DevicesPage(service)
    page.pair_button.click()
    dialog = page.pairing_dialog
    assert dialog is not None
    service.pairingStarted.emit(QR_URI, MANUAL, int(time.time()) + 120)
    dialog.close()
    assert service.names().count("cancel_pairing") == 1
    assert page.pairing_dialog is None


def test_expired_shows_new_code_button(service: FakeService) -> None:
    dialog = PairingDialog(service)
    dialog.show_code(QR_URI, MANUAL, int(time.time()) + 100)
    service.calls.clear()
    dialog.finished_with("expired")
    assert dialog.countdown_label.text() == "Code expired"
    assert not dialog.new_code_button.isHidden() or dialog.new_code_button.isVisibleTo(dialog)
    assert dialog.qr_label.pixmap().isNull()
    assert dialog.manual_label.text() == ""
    dialog.new_code_button.click()
    assert service.names() == ["start_pairing"]


@pytest.mark.parametrize("outcome", ["approved", "denied", "expired", "failed"])
def test_end_states_shrink_the_dialog_to_fit(service: FakeService, outcome: str) -> None:
    dialog = PairingDialog(service)
    dialog.show_code(QR_URI, MANUAL, int(time.time()) + 100)
    dialog.show()
    dialog.resize(dialog.width(), 850)
    tall = dialog.height()
    dialog.finished_with(outcome)
    assert dialog.qr_frame.isHidden()
    assert dialog.height() < tall - 200
    assert dialog.height() <= dialog.sizeHint().height() + 20
    dialog.close()


def test_countdown_reaching_zero_expires(service: FakeService) -> None:
    dialog = PairingDialog(service)
    dialog.show_code(QR_URI, MANUAL, int(time.time()) - 1)
    assert dialog.countdown_label.text() == "Code expired"
    assert dialog.manual_label.text() == ""


def test_locked_warning_banner(service: FakeService) -> None:
    page = DevicesPage(service)
    page.pair_button.click()
    dialog = page.pairing_dialog
    assert dialog is not None
    service.pairingStarted.emit(QR_URI, MANUAL, int(time.time()) + 120)
    service.pairingLockedWarning.emit()
    assert dialog.banner.text() == "Too many wrong codes. Show a new code."
    assert not dialog.banner.isHidden()


def _allow_prompt(page: DevicesPage) -> tuple[QMessageBox, QPushButton, QPushButton]:
    box = page.allow_box
    assert box is not None
    buttons = {b.text(): cast(QPushButton, b) for b in box.buttons()}
    return box, buttons["Allow"], buttons["Deny"]


def test_pending_opens_allow_prompt_and_allow_approves(service: FakeService) -> None:
    page = DevicesPage(service)
    page.pair_button.click()
    service.pairingPending.emit("Pixel 8")
    box, allow, _deny = _allow_prompt(page)
    assert box.windowTitle() == "Pair a phone"
    assert box.text() == 'Allow "Pixel 8" to control GrabLine?'
    allow.click()
    assert "approve_pairing" in service.names()
    assert "deny_pairing" not in service.names()
    assert page.allow_box is None
    # Approval finishes the pairing: success text and a refreshed list.
    service.store.devices.append(device("d3", "Pixel 8"))
    service.pairingFinished.emit("approved")
    dialog = page.pairing_dialog
    assert dialog is not None
    assert "paired" in dialog.status_label.text()
    assert dialog.qr_label.pixmap().isNull()
    assert _names(page) == ["Pixel 8", "Galaxy S", "Pixel 8"]


def test_deny_and_escape_deny(service: FakeService) -> None:
    page = DevicesPage(service)
    page.pair_button.click()
    service.pairingPending.emit("Pixel 8")
    _box, _allow, deny = _allow_prompt(page)
    deny.click()
    assert "deny_pairing" in service.names()
    assert "approve_pairing" not in service.names()
    service.pairingFinished.emit("denied")
    dialog = page.pairing_dialog
    assert dialog is not None
    assert dialog.status_label.text() == "Pairing cancelled"

    service.calls.clear()
    service.pairingPending.emit("Pixel 8")
    box, _allow, _deny = _allow_prompt(page)
    box.reject()
    assert service.names() == ["deny_pairing"]


def test_prompt_closes_when_pairing_ends_without_answer(service: FakeService) -> None:
    page = DevicesPage(service)
    page.pair_button.click()
    service.pairingPending.emit("Pixel 8")
    assert page.allow_box is not None
    service.pairingFinished.emit("failed")
    assert page.allow_box is None
    assert "approve_pairing" not in service.names()
    assert "deny_pairing" not in service.names()


def test_pairing_code_is_never_logged(
    service: FakeService, caplog: pytest.LogCaptureFixture
) -> None:
    page = DevicesPage(service)
    page.pair_button.click()
    with caplog.at_level("DEBUG"):
        service.pairingStarted.emit(QR_URI, MANUAL, int(time.time()) + 120)
    assert "123456" not in caplog.text


def test_qr_pixmap_scales_to_target() -> None:
    ensure_app()
    pixmap = phone_page.qr_pixmap(QR_URI)
    assert 200 <= pixmap.width() <= 280
    assert pixmap.width() == pixmap.height()
