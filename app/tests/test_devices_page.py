"""The Devices page: its states, the live table, the commands it sends, and how
the main window hosts it. Driven with a fake service that has the real service's
API and real Qt signals (phone_fakes)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractButton,
    QGraphicsOpacityEffect,
    QLabel,
    QLineEdit,
    QPushButton,
)

from app.remote.phonejobs import ACTIONS
from app.tests.phone_fakes import FakeService, device, phone_job, snapshot
from app.tests.qt_app import ensure_app
from app.ui.devices_page import DevicesPage, action_label, result_text

ALL_STATUSES = ["queued", "downloading", "paused", "waiting", "completed", "failed", "cancelled"]


@pytest.fixture(autouse=True)
def _profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.ui.devices_page.RENDER_MIN_INTERVAL_S", 0.0)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    ensure_app()


def make(
    *,
    online: bool = True,
    jobs: list[Any] | None = None,
    shared: bool = True,
    report: bool = True,
    devices: int = 2,
) -> tuple[FakeService, DevicesPage]:
    people = [device("d1", "Pixel 8", last_seen=1_700_000_500), device("d2", "Galaxy S")]
    service = FakeService(people[:devices])
    if online:
        service.online = {"d1"}
    if report:
        service.lists["d1"] = snapshot(jobs if jobs is not None else [], shared=shared)
    page = DevicesPage(service)
    page.resize(1000, 560)
    page.show()
    return service, page


def row_buttons(page: DevicesPage, row: int) -> dict[str, QAbstractButton]:
    holder = page.table.cellWidget(row, 5)
    assert holder is not None
    return {
        action_label(str(b.property("action"))): b
        for b in holder.findChildren(QAbstractButton)  # tooltip is the label
    }


def done_text(page: DevicesPage, row: int) -> str:
    holder = page.table.cellWidget(row, 2)
    assert holder is not None
    label = holder.findChild(QLabel, "DoneText")
    assert label is not None
    return label.text()


def cell(page: DevicesPage, row: int, column: int) -> str:
    item = page.table.item(row, column)
    assert item is not None
    return item.text()


def texts(widget: Any) -> list[str]:
    return [label.text() for label in widget.findChildren(QLabel)]


# ------------------------------------------------------------------ states


def test_no_phones_shows_what_pairing_gives_you() -> None:
    service, page = make(devices=0, report=False, online=False)
    assert page.right_stack.currentIndex() == 0
    assert page.empty_title.text() == "No phones paired yet"
    body = page.empty_body.text()
    assert "see and control its downloads" in body and "start downloads on this PC" in body
    assert page.device_list.isHidden() and not page.no_devices_label.isHidden()
    assert page.pair_button.isEnabled() and page.pair_button.text() == "Pair a phone"
    page.pair_button.click()
    assert "start_pairing" in service.names()
    assert page.pairing_dialog is not None
    page.pairing_dialog.close()


def test_phones_are_listed_with_their_state() -> None:
    _service, page = make(jobs=[])
    assert page.device_list.count() == 2
    first = page.device_list.item(0)
    second = page.device_list.item(1)
    assert first.data(Qt.ItemDataRole.AccessibleTextRole) == "Pixel 8, Online"
    assert second.data(Qt.ItemDataRole.AccessibleTextRole) == "Galaxy S, Not connected yet"
    assert page.device_list.accessibleName() == "Paired phones"
    assert page.device_list.currentRow() == 0
    assert page.name_label.text() == "Pixel 8"
    assert page.state_label.text().startswith("Online  ·  Paired ")


def test_offline_phone_shows_last_seen() -> None:
    _service, page = make(online=False, jobs=[phone_job(1)])
    item = page.device_list.item(0)
    assert str(item.data(Qt.ItemDataRole.AccessibleTextRole)).startswith("Pixel 8, Last seen ")
    assert page.state_label.text().startswith("Last seen ")


def test_phone_online_with_jobs_fills_the_table() -> None:
    jobs = [
        phone_job(12, name="ubuntu.iso", total=6_114_656_256, downloaded=734_003_200),
        phone_job(11, name="zoo.mp4", status="completed", downloaded=549_921, total=549_921),
        phone_job(10, name="report.pdf", status="failed", kind="transfer", url="", error="Boom"),
    ]
    service, page = make(jobs=jobs)
    assert page.jobs_stack.currentIndex() == 1
    assert page.table.rowCount() == 3
    assert [cell(page, r, 0) for r in range(3)] == ["ubuntu.iso", "zoo.mp4", "report.pdf"]
    assert done_text(page, 0) == "700.0 MB of 5.7 GB"  # how much is done, always
    assert done_text(page, 1) == "537.0 KB"  # finished: the total
    assert cell(page, 0, 3) == "1.00 MB/s" and cell(page, 0, 4) == "1m 15s"
    assert cell(page, 1, 3) == "" and cell(page, 1, 4) == ""  # only while downloading
    assert page.banner_label.isHidden()
    assert not page._dim.isEnabled()
    assert page.table.accessibleName() == "Downloads on this phone"
    headers = []
    for column in range(page.table.columnCount()):
        header = page.table.horizontalHeaderItem(column)
        assert header is not None
        headers.append(header.text())
    assert headers == ["Name", "Status", "Progress", "Speed", "Time left", ""]
    # Buttons are named after the action and the download.
    names = {b.accessibleName() for b in page.table.cellWidget(0, 5).findChildren(QPushButton)}
    assert names == {
        "Pause ubuntu.iso",
        "Cancel ubuntu.iso",
        "Download on this PC instead ubuntu.iso",
    }
    assert service.names() == []  # showing the list sends nothing


@pytest.mark.parametrize("status", [*ALL_STATUSES, "unknown"])
def test_row_buttons_are_exactly_the_actions_that_fit(status: str) -> None:
    job = phone_job(7, status=status, downloaded=0 if status != "completed" else 5)
    _service, page = make(jobs=[job])
    shown = set(row_buttons(page, 0))
    assert shown == {action_label(a) for a in job.actions()}
    assert all(b.isEnabled() for b in row_buttons(page, 0).values())
    if status == "unknown":
        assert shown == set()


def test_a_copy_from_a_pc_cannot_be_moved_to_this_pc() -> None:
    job = phone_job(3, status="failed", kind="transfer", url="")
    _service, page = make(jobs=[job])
    assert set(row_buttons(page, 0)) == {"Retry", "Remove"}


def test_not_shared_message_and_no_table() -> None:
    service, page = make(shared=False, jobs=[])
    expected = (
        "This phone doesn't share its downloads. "
        "Turn it on in GrabLine on the phone: Settings, PCs."
    )
    assert page.jobs_stack.currentIndex() == 0
    assert page.message_label.text() == expected
    assert page.table.rowCount() == 0
    assert page.message_label.accessibleName() == expected
    # Sending a link still works for such a phone.
    page.link_edit.setText("https://example.com/a.zip")
    page.send_button.click()
    assert ("queue_for_phone", ("d1", "https://example.com/a.zip")) in service.calls


def test_a_phone_that_never_sent_a_list_is_an_older_app() -> None:
    _service, page = make(report=False)
    # Just connected: give it a moment before concluding anything.
    assert page.message_label.text() == "Checking the phone's downloads…"
    page._online_since["d1"] = time.monotonic() - 60
    page._render_right()
    assert page.message_label.text() == "Update GrabLine on the phone to see its downloads here."
    assert page.jobs_stack.currentIndex() == 0


def test_the_grace_timer_turns_checking_into_the_older_app_message() -> None:
    _service, page = make(report=False)
    assert page._grace_timer.isActive()
    page._online_since["d1"] = time.monotonic() - 60
    page._grace_timer.timeout.emit()
    assert page.message_label.text().startswith("Update GrabLine on the phone")


def test_an_offline_phone_that_never_reported() -> None:
    _service, page = make(online=False, report=False)
    assert page.message_label.text() == "Its downloads show up here once the phone connects."


def test_empty_list_message() -> None:
    _service, page = make(jobs=[])
    assert page.message_label.text() == "No downloads on this phone."
    assert page.jobs_stack.currentIndex() == 0


def test_offline_phone_shows_the_last_list_greyed_with_disabled_actions() -> None:
    service, page = make(online=False, jobs=[phone_job(4), phone_job(5, status="paused")])
    assert page.table.rowCount() == 2
    assert not page.banner_label.isHidden()
    assert page.banner_label.text() == "Offline. Showing the last list this phone sent."
    effect = page.table.graphicsEffect()
    assert isinstance(effect, QGraphicsOpacityEffect) and effect is page._dim
    assert effect.isEnabled() and effect.opacity() < 1.0
    for row in range(2):
        buttons = row_buttons(page, row)
        assert buttons and not any(b.isEnabled() for b in buttons.values())
        for button in buttons.values():
            button.click()  # a disabled button does nothing
    assert "command" not in service.names()
    # It comes back to life when the phone connects.
    service.online.add("d1")
    service.deviceOnlineChanged.emit("d1")
    assert page.banner_label.isHidden() and not page._dim.isEnabled()
    assert all(b.isEnabled() for b in row_buttons(page, 0).values())


def test_a_phone_sharing_html_is_shown_as_text() -> None:
    evil = "<b>Evil</b><img src=x>"
    service = FakeService([device("d1", evil)])
    service.online = {"d1"}
    service.lists["d1"] = snapshot(
        [phone_job(1, name="<i>x</i>.zip", error="<u>no</u>", status="failed")]
    )
    page = DevicesPage(service)
    page.show()
    labels = [*page.device_list.findChildren(QLabel), page.name_label, page.state_label]
    assert labels
    assert all(label.textFormat() == Qt.TextFormat.PlainText for label in labels)
    assert page.name_label.full_text() == evil
    assert cell(page, 0, 0) == "<i>x</i>.zip"
    assert page.message_label.textFormat() == Qt.TextFormat.PlainText


# ------------------------------------------------------------- live update


def test_the_table_follows_phone_jobs_changed() -> None:
    service, page = make(jobs=[phone_job(1, downloaded=100), phone_job(2, status="queued")])
    assert cell(page, 0, 0) == "file-1.iso"
    service.lists["d1"] = snapshot(
        [
            phone_job(1, downloaded=900, status="paused", speed=0, eta=None),
            phone_job(2, status="downloading"),
        ]
    )
    service.phoneJobsChanged.emit("d1")
    assert cell(page, 0, 3) == ""  # paused: no speed
    assert set(row_buttons(page, 0)) == {"Resume", "Cancel", "Download on this PC instead"}
    assert cell(page, 1, 3) == "1.00 MB/s"
    # A different list replaces the rows.
    service.lists["d1"] = snapshot([phone_job(9, name="new.bin")])
    service.phoneJobsChanged.emit("d1")
    assert page.table.rowCount() == 1 and cell(page, 0, 0) == "new.bin"
    # Another phone's update does not touch this view.
    service.lists["d2"] = snapshot([phone_job(3), phone_job(4)])
    service.phoneJobsChanged.emit("d2")
    assert page.table.rowCount() == 1


def test_the_first_list_replaces_the_older_app_message() -> None:
    service, page = make(report=False)
    assert page.jobs_stack.currentIndex() == 0
    service.lists["d1"] = snapshot([phone_job(1)])
    service.phoneJobsChanged.emit("d1")
    assert page.jobs_stack.currentIndex() == 1 and page.table.rowCount() == 1


def test_a_list_that_turns_not_shared_replaces_the_table() -> None:
    service, page = make(jobs=[phone_job(1)])
    assert page.table.rowCount() == 1
    service.lists["d1"] = snapshot([], shared=False)
    service.phoneJobsChanged.emit("d1")
    assert page.table.rowCount() == 0 and "doesn't share" in page.message_label.text()


def test_progress_bar_reflects_the_download() -> None:
    _service, page = make(jobs=[phone_job(1, downloaded=500, total=1000)])
    bar = page._bars[0]
    bar.set_value(0.5, immediate=True)
    assert bar._fill.value == pytest.approx(0.5)


def test_selecting_another_phone_shows_its_own_list() -> None:
    service, page = make(jobs=[phone_job(1)])
    service.lists["d2"] = snapshot([phone_job(5, name="other.zip"), phone_job(6)])
    service.online.add("d2")
    page.device_list.setCurrentRow(1)
    assert page.name_label.text() == "Galaxy S"
    assert page.table.rowCount() == 2 and cell(page, 0, 0) == "other.zip"
    page.device_list.setCurrentRow(0)
    assert page.table.rowCount() == 1


def test_presence_change_updates_the_list_and_header() -> None:
    service, page = make(online=False, jobs=[phone_job(1)])
    assert page.state_label.text().startswith("Last seen")
    service.online.add("d1")
    service.deviceOnlineChanged.emit("d1")
    assert page.state_label.text().startswith("Online")
    assert str(page.device_list.item(0).data(Qt.ItemDataRole.AccessibleTextRole)).endswith("Online")


def test_narrow_windows_drop_size_and_time_left() -> None:
    _service, page = make(jobs=[phone_job(1)])
    page.resize(1300, 560)
    ensure_app().processEvents()
    assert not page.table.isColumnHidden(3) and not page.table.isColumnHidden(4)
    page.resize(900, 560)
    ensure_app().processEvents()
    assert page.table.isColumnHidden(4) and not page.table.isColumnHidden(3)  # time left first
    page.resize(760, 560)
    ensure_app().processEvents()
    assert page.table.isColumnHidden(3) and page.table.isColumnHidden(4)
    # How much is done always stays, with the name and the actions.
    for column in (0, 1, 2, 5):
        assert not page.table.isColumnHidden(column)


# ---------------------------------------------------------------- commands


@pytest.mark.parametrize("action", ACTIONS)
def test_each_button_sends_its_command(action: str) -> None:
    status = {
        "pause": "downloading",
        "resume": "paused",
        "cancel": "downloading",
        "retry": "failed",
        "remove": "completed",
        "moveToPc": "downloading",
    }[action]
    service, page = make(jobs=[phone_job(41, status=status)])
    buttons = row_buttons(page, 0)
    buttons[action_label(action)].click()
    assert service.calls == [("command", ("d1", 41, action))]


def test_a_command_disables_that_row_until_the_phone_answers() -> None:
    service, page = make(jobs=[phone_job(1), phone_job(2, status="paused")])
    row_buttons(page, 0)["Pause"].click()
    assert not any(b.isEnabled() for b in row_buttons(page, 0).values())
    assert all(b.isEnabled() for b in row_buttons(page, 1).values())  # other rows keep working
    assert service.names() == ["command"]
    service.commandFinished.emit(1, "ok", "")
    assert all(b.isEnabled() for b in row_buttons(page, 0).values())


@pytest.mark.parametrize(
    ("result", "message", "shown"),
    [
        ("ok", "", "Paused on the phone"),
        ("ok", "Paused it", "Pause: Paused it"),
        ("ok", "", "Paused on the phone"),
        ("timeout", "", "Pause: The phone did not answer"),
        ("offline", "", "Pause: The phone is not connected"),
        ("busy", "", "Pause: Too many commands at once. Try again in a moment."),
        ("notFound", "", "Pause: The phone no longer has that download"),
        ("refused", "", "Pause: The phone did not allow that"),
        ("refused", "Not while charging", "Pause: Not while charging"),
        ("failed", "", "Pause: The phone could not do that"),
        ("failed", "No space left", "Pause: No space left"),
    ],
)
def test_the_result_of_each_command_is_shown(result: str, message: str, shown: str) -> None:
    service, page = make(jobs=[phone_job(1)])
    row_buttons(page, 0)["Pause"].click()
    service.commandFinished.emit(1, result, message)
    assert page.result_label.text() == shown
    # Failures are coloured so they are seen; success is not.
    assert page.result_label.property("role") == ("ok" if result == "ok" else "warn")
    assert page.result_label.textFormat() == Qt.TextFormat.PlainText


def test_a_timeout_says_the_phone_did_not_answer_and_frees_the_row() -> None:
    service, page = make(jobs=[phone_job(1)])
    row_buttons(page, 0)["Cancel"].click()
    service.commandFinished.emit(1, "timeout", "")
    assert page.result_label.text() == "Cancel: The phone did not answer"
    assert all(b.isEnabled() for b in row_buttons(page, 0).values())


def test_results_of_commands_this_page_did_not_send_are_ignored() -> None:
    service, page = make(jobs=[phone_job(1)])
    service.commandFinished.emit(77, "timeout", "")
    assert page.result_label.text() == ""


def test_a_command_the_service_refuses_to_send_says_so() -> None:
    service, page = make(jobs=[phone_job(1)])
    service.send_phone_command = lambda *a: 0  # type: ignore[method-assign]
    row_buttons(page, 0)["Pause"].click()
    assert page.result_label.text() == "Pause: The phone could not do that"
    assert all(b.isEnabled() for b in row_buttons(page, 0).values())


def test_the_result_clears_itself() -> None:
    service, page = make(jobs=[phone_job(1)])
    row_buttons(page, 0)["Pause"].click()
    service.commandFinished.emit(1, "ok", "")
    assert page._result_timer.isActive()
    page._result_timer.timeout.emit()
    assert page.result_label.text() == ""


def test_the_context_menu_offers_the_same_enabled_actions() -> None:
    service, page = make(jobs=[phone_job(5, status="paused")])
    menu = page._row_menu(0)
    assert menu is not None
    assert [(a.text(), a.isEnabled()) for a in menu.actions()] == [
        ("Resume", True),
        ("Cancel", True),
        ("Download on this PC instead", True),
    ]
    menu.actions()[0].trigger()
    assert service.calls == [("command", ("d1", 5, "resume"))]
    assert page._row_menu(3) is None
    # An offline phone's menu is greyed, like its buttons.
    service.online.clear()
    greyed = page._row_menu(0)
    assert greyed is not None and not any(a.isEnabled() for a in greyed.actions())


def test_result_text_prefers_the_phones_words_only_for_its_own_answers() -> None:
    assert result_text("ok", "") == "Done"
    assert result_text("refused", "No") == "No"
    # The PC's own conclusions keep their fixed sentences.
    assert result_text("timeout", "ignored") == "The phone did not answer"
    assert result_text("mystery", "") == "The phone could not do that"
    assert action_label("moveToPc") == "Download on this PC instead"


# ---------------------------------------------------------------- sending


def test_send_a_link_to_the_selected_phone() -> None:
    service, page = make(jobs=[])
    assert not page.send_button.isEnabled()
    page.link_edit.setText("  https://example.com/file.zip  ")
    assert page.send_button.isEnabled()
    page.send_button.click()
    assert service.calls == [("queue_for_phone", ("d1", "https://example.com/file.zip"))]
    assert page.link_edit.text() == ""
    assert page.send_note.text() == "Sent to your phone" and not page.send_note.isHidden()
    assert not page.send_button.isEnabled()


def test_enter_sends_and_a_bad_link_is_explained() -> None:
    service, page = make(jobs=[])
    service.accept_links = False
    heard: list[str] = []
    page.status.connect(heard.append)
    page.link_edit.setText("not a link")
    page.link_edit.returnPressed.emit()
    assert page.send_note.text() == "This link cannot be sent to a phone"
    assert page.link_edit.text() == "not a link"  # kept so it can be fixed
    assert heard == []  # shown once, inline: not on the status bar too


def test_a_link_goes_to_the_phone_that_is_selected() -> None:
    service, page = make(jobs=[])
    page.device_list.setCurrentRow(1)
    page.link_edit.setText("https://example.com/b.zip")
    page.send_button.click()
    assert service.calls == [("queue_for_phone", ("d2", "https://example.com/b.zip"))]


def test_an_offline_phone_can_still_be_sent_a_link() -> None:
    service, page = make(online=False, jobs=[])
    page.link_edit.setText("https://example.com/c.zip")
    assert page.send_button.isEnabled()
    page.send_button.click()
    assert service.queued == [("d1", "https://example.com/c.zip")]


# --------------------------------------------------------------- removing


def test_remove_asks_first_then_unpairs_through_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, page = make(jobs=[])
    asked: list[str] = []
    answers = iter([False, True])

    def fake_confirm(name: str) -> bool:
        asked.append(name)
        return next(answers)

    monkeypatch.setattr(page, "_confirm_remove", fake_confirm)
    assert page.remove_button.accessibleName() == "Remove Pixel 8"
    page.remove_button.click()
    assert asked == ["Pixel 8"] and "remove_device" not in service.names()
    page.remove_button.click()
    assert ("remove_device", "d1") in service.calls
    # The next phone takes its place.
    assert page.device_list.count() == 1 and page.name_label.text() == "Galaxy S"


def test_removing_the_last_phone_shows_the_empty_state(monkeypatch: pytest.MonkeyPatch) -> None:
    service, page = make(devices=1, jobs=[])
    monkeypatch.setattr(page, "_confirm_remove", lambda _name: True)
    page.remove_button.click()
    assert page.right_stack.currentIndex() == 0 and service.store.count() == 0


def test_remove_confirmation_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    _service, page = make(jobs=[])
    seen: dict[str, str] = {}

    def fake_exec(box: QMessageBox) -> int:
        seen["text"] = box.text()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    assert page._confirm_remove("Pixel 8") is False
    assert seen["text"] == "Remove Pixel 8? It will stop working at once."


def test_a_phone_removed_elsewhere_leaves_the_list() -> None:
    service, page = make(jobs=[phone_job(1)])
    service.store.devices = [d for d in service.store.devices if d.device_id != "d1"]
    service.lists.pop("d1")
    service.devicesChanged.emit()
    assert page.device_list.count() == 1 and page.name_label.text() == "Galaxy S"


# ------------------------------------------------------------ accessibility


def test_every_control_has_an_accessible_name_and_can_be_tabbed_to() -> None:
    _service, page = make(jobs=[phone_job(1), phone_job(2, status="paused")])
    controls = page.findChildren(QPushButton) + page.findChildren(QLineEdit)
    assert controls
    for control in controls:
        name = control.accessibleName() or (
            control.text() if isinstance(control, QPushButton) else ""
        )
        assert name, f"{control!r} has no accessible name"
        assert control.focusPolicy() != Qt.FocusPolicy.NoFocus
    assert page.link_edit.accessibleName() == "Send a link to this phone"
    assert page.send_button.accessibleName() == "Send"
    assert page.pair_button.accessibleName() == "Pair a phone"


def test_retint_redraws_without_losing_the_selection() -> None:
    from app.ui import theme

    app = ensure_app()
    _service, page = make(jobs=[phone_job(1)])
    page.device_list.setCurrentRow(0)
    theme.apply_theme(app, "dark")
    try:
        page.retint()
        assert page.table.rowCount() == 1 and page.name_label.text() == "Pixel 8"
    finally:
        theme.apply_theme(app, "light")


# ------------------------------------------------------------- main window


def test_main_window_has_devices_after_queue_and_settings_opens_it(db: Any, tmp_path: Path) -> None:
    from app.core.manager import DownloadManager
    from app.core.settings import Settings
    from app.ui.main_window import MainWindow
    from app.ui.shortcuts import BY_ID

    ensure_app()
    settings = Settings(db)
    settings.download_dir = tmp_path
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    window = MainWindow(manager, settings)
    try:
        assert list(window._nav) == ["downloads", "dashboard", "queue", "devices", "settings"]
        button = window._nav["devices"]
        assert button.accessibleName() == "Devices" and button.toolTip() == "Devices"
        assert "nav.devices" in BY_ID
        assert window._devices_page is None  # built on first use
        button.click()
        page = window._devices_page
        assert isinstance(page, DevicesPage)
        assert window._pages.currentWidget() is page
        assert window._current_page == "devices" and button.isChecked()
        # The status bar carries the outcome of a command.
        page.status.emit("Pause: The phone did not answer")
        assert window.statusBar().currentMessage() == "Pause: The phone did not answer"
        # Ctrl+Tab style cycling includes it, in the right place.
        window._switch_view("queue")
        window._cycle_page(1)
        assert window._current_page == "devices"
        window._cycle_page(1)
        assert window._current_page == "settings"
        # Settings -> Phone has the button that opens Devices.
        from app.ui.phone_page import PhonePage

        window._switch_view("settings")
        settings_page = window._pages.currentWidget()
        settings_page._list.setCurrentRow(1)  # Phone: built when first opened
        phone = settings_page.findChild(PhonePage)
        assert phone is not None
        window._switch_view("downloads")
        phone.devices_button.click()
        assert window._current_page == "devices"
    finally:
        window.shutdown()
        manager.shutdown()


# ------------------------------------------------ review: tooltips, throttle


EVIL = '<img src="//evil-host/s/x.png"><b>x</b>'
ESCAPED = "&lt;img src=&quot;//evil-host/s/x.png&quot;&gt;&lt;b&gt;x&lt;/b&gt;"


def pill_label(page: DevicesPage) -> QLabel:
    holder = page.table.cellWidget(0, 1)
    assert holder is not None
    label = holder.findChild(QLabel)
    assert label is not None
    return label


def test_plain_tooltip_escapes_markup_and_keeps_line_breaks() -> None:
    from app.ui.format import plain_tooltip

    tip = plain_tooltip(EVIL + "\nline two")
    assert "<img" not in tip and "<b>" not in tip and ESCAPED in tip
    assert tip.startswith("<p style='white-space:pre-wrap'>") and "\nline two" in tip
    assert plain_tooltip("") == "" and plain_tooltip(None) == ""


def test_phone_text_never_reaches_a_tooltip_as_markup() -> None:
    service = FakeService([device("d1", EVIL)])
    service.online = {"d1"}
    service.lists["d1"] = snapshot([phone_job(1, name=EVIL, status="failed", error=EVIL)])
    page = DevicesPage(service)
    page.show()
    item = page.table.item(0, 0)
    pill = pill_label(page)
    assert item is not None
    carrying = [page.name_label.toolTip(), item.toolTip(), pill.toolTip()]
    titles = [lbl.toolTip() for lbl in page.device_list.findChildren(QLabel) if lbl.toolTip()]
    assert all(carrying) and titles
    for tip in [*carrying, *titles]:
        assert "<img" not in tip and "<b>" not in tip, tip
    for tip in [*carrying, titles[0]]:
        assert ESCAPED in tip


def test_an_error_tooltip_goes_away_with_the_error() -> None:
    service, page = make(jobs=[phone_job(1, status="failed", error="Boom")])
    pill = pill_label(page)
    assert "Boom" in pill.toolTip()
    service.lists["d1"] = snapshot([phone_job(1, status="failed", error=None)])
    service.phoneJobsChanged.emit("d1")
    assert "Boom" not in pill.toolTip()


def test_phone_list_updates_redraw_at_most_once_per_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.ui.devices_page.RENDER_MIN_INTERVAL_S", 0.25)
    service, page = make(jobs=[phone_job(1)])
    renders: list[int] = []
    real = page._render_right

    def counted() -> None:
        renders.append(1)
        real()

    page._render_right = counted  # type: ignore[method-assign]
    page._last_render = time.monotonic()
    for n in range(50):  # a flood of reports
        service.lists["d1"] = snapshot([phone_job(1, name=f"v{n}.bin")])
        service.phoneJobsChanged.emit("d1")
    assert renders == []  # inside the interval: nothing drawn yet
    assert page._render_timer.isActive()
    page._render_timer.stop()
    page._render_timer.timeout.emit()
    assert len(renders) == 1
    assert cell(page, 0, 0) == "v49.bin"  # the newest list won
    # Past the interval the next report draws at once.
    page._last_render = time.monotonic() - 1.0
    service.phoneJobsChanged.emit("d1")
    assert len(renders) == 2


def test_bidi_in_a_device_name_cannot_reorder_it() -> None:
    from app.remote.phonejobs import neutralise

    assert neutralise("photo\u202egpj.exe") == "photo\ufffdgpj.exe"


# ---------------------------------------------- polish: size, names, notes


def test_unknown_total_and_early_states_still_say_how_much() -> None:
    jobs = [
        phone_job(1, total=None, downloaded=34_000_000),
        phone_job(2, status="queued", downloaded=0, total=None),
        phone_job(3, status="paused", downloaded=34_000_000, total=793_000_000),
    ]
    _service, page = make(jobs=jobs)
    assert done_text(page, 0) == "32.4 MB"
    assert done_text(page, 1) == ""
    assert done_text(page, 2) == "32.4 MB of 756.3 MB"


def test_the_name_takes_the_remaining_width() -> None:
    from PySide6.QtWidgets import QHeaderView

    _service, page = make(jobs=[phone_job(1)])
    header = page.table.horizontalHeader()
    assert header.sectionResizeMode(0) == QHeaderView.ResizeMode.Stretch


def test_action_buttons_are_named_with_the_job_and_tabbable() -> None:
    _service, page = make(jobs=[phone_job(1, name="debian-13.7.0-amd64-netinst.iso")])
    row = page.table.cellWidget(0, 5)
    assert row is not None
    buttons = row.findChildren(QPushButton)
    assert buttons
    for button in buttons:
        assert button.accessibleName().endswith(" debian-13.7.0-amd64-netinst.iso")
        assert button.toolTip().startswith("<p style='white-space:pre-wrap'>")
        assert button.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert "Pause debian-13.7.0-amd64-netinst.iso" in {b.accessibleName() for b in buttons}


def test_the_selected_download_has_real_buttons_outside_the_table() -> None:
    """UI Automation cannot see widgets inside table cells; this bar is the
    accessible path and does the same commands."""
    service, page = make(
        jobs=[phone_job(7, name="a.iso"), phone_job(8, name="b.iso", status="paused")]
    )
    assert not page.action_bar.isHidden()
    bar = {b.accessibleName(): b for b in page.action_bar.findChildren(QPushButton)}
    assert set(bar) == {"Pause a.iso", "Cancel a.iso", "Download on this PC instead a.iso"}
    assert all(b.focusPolicy() == Qt.FocusPolicy.StrongFocus for b in bar.values())
    bar["Pause a.iso"].click()
    assert service.calls == [("command", ("d1", 7, "pause"))]
    page.table.setCurrentCell(1, 0)
    names = {b.accessibleName() for b in page.action_bar.findChildren(QPushButton)}
    assert "Resume b.iso" in names and "Pause a.iso" not in names


def test_the_action_bar_is_hidden_when_there_is_no_list() -> None:
    _service, page = make(shared=False, jobs=[])
    assert page.action_bar.isHidden()


def test_sent_note_shows_inline_only_and_clears_itself_or_on_typing() -> None:
    service, page = make(jobs=[])
    heard: list[str] = []
    page.status.connect(heard.append)
    page.link_edit.setText("https://example.com/a.zip")
    page.send_button.click()
    assert page.send_note.text() == "Sent to your phone" and heard == []
    assert page._note_timer.isActive()
    page._note_timer.stop()
    page._note_timer.timeout.emit()
    assert page.send_note.isHidden() and page.send_note.text() == ""
    page.link_edit.setText("https://example.com/b.zip")
    page.send_button.click()
    assert not page.send_note.isHidden()
    page.link_edit.setText("h")  # typing again
    assert page.send_note.isHidden()
    assert service.queued
