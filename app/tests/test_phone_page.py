"""Settings -> Phone page, the pairing dialog and the Allow prompt, driven with
a fake service that has the real service's API and real Qt signals."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QLabel, QMessageBox, QPushButton

from app.tests.qt_app import ensure_app
from app.ui import phone_page
from app.ui.phone_page import PairingDialog, PhonePage

QR_URI = "grabline://pair?v=1&a=192.168.1.20&p=47821&c=123456"
MANUAL = "192.168.1.20:47821  code 123456"


def _device(device_id: str, name: str, last_seen: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        device_id=device_id,
        name=name,
        public_key="",
        created_at=1_700_000_000,
        last_seen_at=last_seen,
    )


class FakeStore:
    def __init__(self, devices: list[SimpleNamespace]) -> None:
        self.devices = devices

    def list(self) -> list[SimpleNamespace]:
        return list(self.devices)

    def count(self) -> int:
        return len(self.devices)


class FakeService(QObject):
    pairingStarted = Signal(str, str, int)
    pairingPending = Signal(str)
    pairingLockedWarning = Signal()
    pairingFinished = Signal(str)
    devicesChanged = Signal()
    stateChanged = Signal(bool)
    errorOccurred = Signal(str)

    def __init__(self, devices: list[SimpleNamespace] | None = None) -> None:
        super().__init__()
        self.store = FakeStore(devices or [])
        self.listener_created = False
        self.calls: list[tuple[str, Any]] = []
        self._enabled = True
        self._port = 47821

    def enabled_setting(self) -> bool:
        return self._enabled

    def port_setting(self) -> int:
        return self._port

    def set_enabled(self, on: bool) -> None:
        self._enabled = on
        self.calls.append(("set_enabled", on))

    def set_port(self, port: int) -> None:
        self._port = port
        self.calls.append(("set_port", port))

    def start_pairing(self) -> None:
        self.calls.append(("start_pairing", None))

    def cancel_pairing(self) -> None:
        self.calls.append(("cancel_pairing", None))

    def approve_pairing(self) -> None:
        self.calls.append(("approve_pairing", None))

    def deny_pairing(self) -> None:
        self.calls.append(("deny_pairing", None))

    def remove_device(self, device_id: str) -> bool:
        self.calls.append(("remove_device", device_id))
        self.store.devices = [d for d in self.store.devices if d.device_id != device_id]
        return True

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture()
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeService:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    ensure_app()
    return FakeService(
        [_device("d1", "Pixel 8", last_seen=1_700_000_500), _device("d2", "Galaxy S")]
    )


def _texts(page: PhonePage) -> list[str]:
    return [label.text() for label in page.devices_box.findChildren(QLabel)]


def test_page_lists_devices_with_dates(service: FakeService) -> None:
    page = PhonePage(service)
    texts = _texts(page)
    assert "Pixel 8" in texts
    assert "Galaxy S" in texts
    assert any("Paired" in text and "Last seen" in text for text in texts)
    assert any("Not connected yet" in text for text in texts)
    removes = [b for b in page.devices_box.findChildren(QPushButton) if b.text() == "Remove"]
    assert [b.accessibleName() for b in removes] == ["Remove Pixel 8", "Remove Galaxy S"]


def test_empty_list_and_refresh_on_devices_changed(service: FakeService) -> None:
    service.store.devices = []
    page = PhonePage(service)
    assert any("No phones are paired" in text for text in _texts(page))
    service.store.devices = [_device("d9", "Moto")]
    service.devicesChanged.emit()
    assert "Moto" in _texts(page)


def test_remove_asks_first_then_calls_service(
    service: FakeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = PhonePage(service)
    asked: list[str] = []
    answers = iter([False, True])

    def fake_confirm(name: str) -> bool:
        asked.append(name)
        return next(answers)

    monkeypatch.setattr(page, "_confirm_remove", fake_confirm)
    buttons = [b for b in page.devices_box.findChildren(QPushButton) if b.text() == "Remove"]
    buttons[0].click()
    assert asked == ["Pixel 8"]
    assert "remove_device" not in service.names()
    page._remove(service.store.devices[0])
    assert ("remove_device", "d1") in service.calls
    assert "Pixel 8" not in _texts(page)


def test_remove_confirmation_text(service: FakeService, monkeypatch: pytest.MonkeyPatch) -> None:
    page = PhonePage(service)
    seen: dict[str, str] = {}

    def fake_exec(box: QMessageBox) -> int:
        seen["text"] = box.text()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    assert page._confirm_remove("Pixel 8") is False
    assert seen["text"] == "Remove Pixel 8? It will stop working at once."


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


def test_pairing_started_shows_qr_and_countdown(service: FakeService) -> None:
    page = PhonePage(service)
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
    page = PhonePage(service)
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


def test_countdown_reaching_zero_expires(service: FakeService) -> None:
    dialog = PairingDialog(service)
    dialog.show_code(QR_URI, MANUAL, int(time.time()) - 1)
    assert dialog.countdown_label.text() == "Code expired"
    assert dialog.manual_label.text() == ""


def test_locked_warning_banner(service: FakeService) -> None:
    page = PhonePage(service)
    page.pair_button.click()
    dialog = page.pairing_dialog
    assert dialog is not None
    service.pairingStarted.emit(QR_URI, MANUAL, int(time.time()) + 120)
    service.pairingLockedWarning.emit()
    assert dialog.banner.text() == "Too many wrong codes. Show a new code."
    assert not dialog.banner.isHidden()


def _allow_prompt(page: PhonePage) -> tuple[QMessageBox, QPushButton, QPushButton]:
    box = page.allow_box
    assert box is not None
    buttons = {b.text(): cast(QPushButton, b) for b in box.buttons()}
    return box, buttons["Allow"], buttons["Deny"]


def test_pending_opens_allow_prompt_and_allow_approves(service: FakeService) -> None:
    page = PhonePage(service)
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
    service.store.devices.append(_device("d3", "Pixel 8"))
    service.pairingFinished.emit("approved")
    dialog = page.pairing_dialog
    assert dialog is not None
    assert "paired" in dialog.status_label.text()
    assert dialog.qr_label.pixmap().isNull()
    assert _texts(page).count("Pixel 8") == 2


def test_deny_and_escape_deny(service: FakeService) -> None:
    page = PhonePage(service)
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
    page = PhonePage(service)
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
    page = PhonePage(service)
    page.pair_button.click()
    with caplog.at_level("DEBUG"):
        service.pairingStarted.emit(QR_URI, MANUAL, int(time.time()) + 120)
    assert "123456" not in caplog.text


def test_qr_pixmap_scales_to_target() -> None:
    ensure_app()
    pixmap = phone_page.qr_pixmap(QR_URI)
    assert 200 <= pixmap.width() <= 280
    assert pixmap.width() == pixmap.height()


def test_settings_view_hosts_phone_page(
    service: FakeService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.settings import Settings
    from app.db.database import Database
    from app.ui.settings_view import SettingsView

    db = Database(tmp_path / "grabline-test.db")
    try:
        view = SettingsView(Settings(db), lambda: None, remote_service=service)
        titles = [view._list.item(i).text() for i in range(view._list.count())]
        assert "Phone" in titles
        assert titles[-1] == "About"
        without = SettingsView(Settings(db), lambda: None)
        assert without._list.count() == view._list.count() - 1
    finally:
        db.close()
