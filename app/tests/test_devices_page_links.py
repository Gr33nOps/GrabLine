"""The Devices page with PCs: phones and PCs in two groups, PCs that linked to
this one (like a phone), PCs this one linked to (their downloads, live), a PC
both ways shown once, the "Link to another PC" dialog, and the Settings switch
for sharing. Driven with fakes that have the real services' API and signals."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QAbstractButton, QLabel, QLineEdit, QMessageBox, QPushButton

from app.tests.phone_fakes import FakeLinks, FakeService, device, link, phone_job, snapshot
from app.tests.qt_app import ensure_app
from app.ui.devices_page import DevicesPage, action_label
from app.ui.link_dialog import LinkDialog, failure_text


@pytest.fixture(autouse=True)
def _profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.ui.devices_page.RENDER_MIN_INTERVAL_S", 0.0)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    ensure_app()


def make(
    devices: list[Any] | None = None,
    links: list[Any] | None = None,
    *,
    online: set[str] | None = None,
) -> tuple[FakeService, FakeLinks, DevicesPage]:
    service = FakeService(devices if devices is not None else [device("p1", "Pixel 8")])
    service.online = online if online is not None else {"p1"}
    service.lists["p1"] = snapshot([phone_job(1, name="on-phone.iso")])
    fake_links = FakeLinks(links if links is not None else [link("L1", "DESKTOP")])
    fake_links.jobs["L1"] = snapshot(
        [
            phone_job(7, name="on-desktop.iso", status="downloading"),
            phone_job(8, name="paused.iso", status="paused"),
            phone_job(9, name="done.iso", status="completed"),
        ]
    )
    page = DevicesPage(service, links=fake_links)
    page.resize(1000, 600)
    page.show()
    return service, fake_links, page


def texts_of(holder: Any) -> list[str]:
    """The full text of every label (the rows elide what does not fit)."""
    return [getattr(lbl, "full_text", lbl.text)() for lbl in holder.findChildren(QLabel)]


def select(page: DevicesPage, name: str) -> None:
    for widget in (page.device_list, page.pc_list):
        for row in range(widget.count()):
            item = widget.item(row)
            holder = widget.itemWidget(item)
            if holder is not None and name in texts_of(holder):
                widget.setCurrentRow(row)
                return
    raise AssertionError(f"{name} is not on the page")


def first_name(page: DevicesPage) -> str:
    item = page.table.item(0, 0)
    assert item is not None
    return item.text()


def row_names(widget: Any) -> list[str]:
    names = []
    for row in range(widget.count()):
        holder = widget.itemWidget(widget.item(row))
        names.append(texts_of(holder)[0] if holder is not None else "")
    return names


def action_buttons(page: DevicesPage, row: int) -> dict[str, QAbstractButton]:
    holder = page.table.cellWidget(row, 5)
    assert holder is not None
    return {str(b.property("action")): b for b in holder.findChildren(QAbstractButton)}


# ---------------------------------------------------------------- the lists


def test_phones_and_pcs_are_two_groups() -> None:
    _service, _links, page = make(
        devices=[device("p1", "Pixel 8"), device("c1", "LAPTOP", kind="pc")],
        links=[link("L1", "DESKTOP")],
    )
    assert row_names(page.device_list) == ["Pixel 8"]
    assert row_names(page.pc_list) == ["LAPTOP", "DESKTOP"]  # linked to us, then we linked to it
    assert not page.phones_label.isHidden() and not page.pcs_label.isHidden()
    assert page.phones_label.text() == "PHONES" and page.pcs_label.text() == "PCS"
    assert page.device_list.accessibleName() == "Phones"
    assert page.pc_list.accessibleName() == "PCs"
    assert page.no_devices_label.isHidden()


def test_an_empty_group_is_hidden() -> None:
    _service, _links, page = make(devices=[device("p1", "Pixel 8")], links=[])
    assert page.pc_list.isHidden() and page.pcs_label.isHidden()
    assert not page.device_list.isHidden()
    _service, _links, page = make(devices=[], links=[link("L1", "DESKTOP")])
    assert page.device_list.isHidden() and page.phones_label.isHidden()
    assert row_names(page.pc_list) == ["DESKTOP"]


def test_nothing_paired_or_linked_shows_the_empty_state() -> None:
    _service, _links, page = make(devices=[], links=[])
    assert page.right_stack.currentIndex() == 0
    assert not page.no_devices_label.isHidden()
    assert page.pair_button.text() == "Pair a phone or PC"
    assert page.link_button.text() == "Link to another PC" and not page.link_button.isHidden()


def test_selecting_in_one_group_clears_the_other() -> None:
    _service, _links, page = make()
    select(page, "Pixel 8")
    assert page.device_list.currentRow() == 0 and page.pc_list.selectedItems() == []
    select(page, "DESKTOP")
    assert page.pc_list.currentRow() == 0 and page.device_list.selectedItems() == []
    assert page.name_label.text() == "DESKTOP"


def test_without_the_link_service_the_page_has_no_link_button() -> None:
    service = FakeService([device("p1", "Pixel 8")])
    page = DevicesPage(service)
    page.show()
    assert page.link_button.isHidden() and page.pc_list.isHidden()


# ------------------------------------------------------ a PC this one linked to


def test_a_linked_pc_shows_its_downloads_live_with_the_right_actions() -> None:
    _service, links, page = make()
    select(page, "DESKTOP")
    assert page.state_label.text().startswith("Online  ·  Linked ")
    assert page.send_caption.text() == "ADD A LINK TO THIS PC"
    assert page.jobs_caption.text() == "DOWNLOADS ON THIS PC"
    assert page.send_button.text() == "Add"
    assert [
        getattr(page.table.item(r, 0), "text", str)() for r in range(page.table.rowCount())
    ] == [
        "on-desktop.iso",
        "paused.iso",
        "done.iso",
    ]
    assert sorted(action_buttons(page, 0)) == ["cancel", "pause"]
    assert sorted(action_buttons(page, 1)) == ["cancel", "resume"]
    assert sorted(action_buttons(page, 2)) == ["remove"]
    for row in range(3):
        assert "moveToPc" not in action_buttons(page, row), "its downloads are already on a PC"
    # A host job changed: the table follows.
    links.jobs["L1"] = snapshot([phone_job(7, name="on-desktop.iso", status="paused")])
    links.hostJobsChanged.emit("L1")
    assert page.table.rowCount() == 1
    assert sorted(action_buttons(page, 0)) == ["cancel", "resume"]


def test_controls_go_over_the_link_and_the_outcome_is_shown() -> None:
    _service, links, page = make()
    select(page, "DESKTOP")
    page.table.setCurrentCell(0, 0)
    action_buttons(page, 0)["pause"].click()
    assert links.calls[-1] == ("control", ("L1", 7, "pause"))
    assert ("l:L1", 7) in page._busy
    assert not action_buttons(page, 0)["pause"].isEnabled(), "busy: no second click"
    links.requestFinished.emit(1, "ok", "")
    assert page._busy == set()
    assert page.result_label.text() == "Pause: Done"
    action_buttons(page, 1)["cancel"].click()
    links.requestFinished.emit(2, "notFound", "")
    assert page.result_label.text() == "Cancel: That PC no longer has that download"
    action_buttons(page, 1)["resume"].click()
    links.requestFinished.emit(3, "conflict", "")
    assert page.result_label.text() == "Resume: That can't be done right now"
    action_buttons(page, 1)["resume"].click()
    links.requestFinished.emit(4, "timeout", "")
    assert page.result_label.text() == "Resume: That PC did not answer"


def test_a_control_that_cannot_be_sent_says_so() -> None:
    _service, links, page = make()
    select(page, "DESKTOP")
    links.accept = False
    action_buttons(page, 0)["pause"].click()
    assert page._busy == set()
    assert page.result_label.text() == "Pause: That PC could not do that"


def test_the_row_menu_and_action_bar_offer_the_same_actions() -> None:
    _service, links, page = make()
    select(page, "DESKTOP")
    page.table.setCurrentCell(1, 0)
    menu = page._row_menu(1)
    assert menu is not None
    assert [a.text() for a in menu.actions()] == [action_label("resume"), action_label("cancel")]
    bar_buttons = [b for b in page.action_bar.findChildren(QAbstractButton)]
    assert sorted(str(b.property("action")) for b in bar_buttons) == ["cancel", "resume"]
    menu.actions()[0].trigger()
    assert links.calls[-1] == ("control", ("L1", 8, "resume"))


def test_adding_a_link_sends_download_over_the_link() -> None:
    _service, links, page = make()
    select(page, "DESKTOP")
    assert not page.send_button.isEnabled()
    page.link_edit.setText("  https://example.com/file.zip  ")
    assert page.send_button.isEnabled()
    page.send_button.click()
    assert links.calls[-1] == ("download", ("L1", "https://example.com/file.zip"))
    links.requestFinished.emit(1, "ok", "")
    assert page.link_edit.text() == "" and page.send_note.text() == "Added on that PC"
    page.link_edit.setText("https://example.com/again.zip")
    page.send_button.click()
    links.requestFinished.emit(2, "ok", "duplicate")
    assert page.send_note.text() == "That PC already has this link"
    page.link_edit.setText("https://example.com/c.zip")
    page.send_button.click()
    links.requestFinished.emit(3, "offline", "")
    assert page.send_note.text() == "That PC is not connected"
    assert page.link_edit.text() == "https://example.com/c.zip", "kept so it can be sent again"


def test_several_links_go_as_links_and_a_bad_one_is_refused() -> None:
    _service, links, page = make()
    select(page, "DESKTOP")
    page.link_edit.setText("https://a.example/1 https://a.example/2")
    page.send_button.click()
    assert links.calls[-1] == ("links", ("L1", ("https://a.example/1", "https://a.example/2")))
    links.accept = False
    page.link_edit.setText("nope")
    page.send_button.click()
    assert page.send_note.text() == "This link cannot be sent to a PC"


def test_nothing_can_be_added_while_the_link_is_offline() -> None:
    _service, _links, page = make(links=[link("L1", "DESKTOP", "offline")])
    select(page, "DESKTOP")
    page.link_edit.setText("https://example.com/file.zip")
    assert not page.send_button.isEnabled()
    assert page.banner_label.isVisible()
    assert page.banner_label.text() == "Offline. Showing the last list from that PC."
    assert not action_buttons(page, 0)["pause"].isEnabled()


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("connecting", "Connecting…"),
        ("identityChanged", "This PC's identity changed. Link it again"),
        ("removed", "This PC no longer accepts this one. Link it again"),
        ("hostTooOld", "Update GrabLine on the other PC"),
        ("clientTooOld", "Update GrabLine on this PC"),
    ],
)
def test_link_states_are_worded(state: str, expected: str) -> None:
    _service, links, page = make(links=[link("L1", "DESKTOP", state)])
    links.jobs.clear()
    page.refresh()
    select(page, "DESKTOP")
    assert expected in page.state_label.text() + " " + page.message_label.text()
    holder = page.pc_list.itemWidget(page.pc_list.item(0))
    assert holder is not None and expected in texts_of(holder)


def test_a_stopped_link_can_be_removed_and_offers_no_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _service, links, page = make(links=[link("L1", "DESKTOP", "identityChanged")])
    monkeypatch.setattr(page, "_confirm_remove", lambda _name: True)
    select(page, "DESKTOP")
    assert not any("Retry" in b.text() for b in page.findChildren(QPushButton))
    page.remove_button.click()
    assert links.calls == [("remove_link", "L1")]
    assert page.pc_list.count() == 0


def test_loading_and_empty_and_never_connected_messages() -> None:
    _service, links, page = make()
    links.jobs.clear()
    page.refresh()
    select(page, "DESKTOP")
    assert page.message_label.text() == "Checking that PC's downloads…"
    links.jobs["L1"] = snapshot([])
    links.hostJobsChanged.emit("L1")
    assert page.message_label.text() == "No downloads on that PC."
    links.infos = [link("L1", "DESKTOP", "offline")]
    links.jobs.clear()
    links.linksChanged.emit()
    assert page.message_label.text() == "Its downloads show up here once that PC connects."


def test_the_page_says_when_the_other_pc_cannot_see_this_ones_downloads() -> None:
    _service, links, page = make(links=[link("L1", "DESKTOP", features=("jobs", "control"))])
    select(page, "DESKTOP")
    assert (
        page.share_note.text() == "That PC can't see downloads on this PC. Update GrabLine on it."
    )
    links.infos = [link("L1", "DESKTOP")]
    links.shared = False
    links.linksChanged.emit()
    assert page.share_note.text().startswith("Sharing is off, so that PC can't see or control")
    links.shared = True
    links.linksChanged.emit()
    assert page.share_note.isHidden()


# --------------------------------------------------- a PC that linked to this one


def test_a_pc_that_linked_to_this_one_is_shown_like_a_phone() -> None:
    service, _links, page = make(
        devices=[device("c1", "LAPTOP", kind="pc")], links=[], online={"c1"}
    )
    service.lists["c1"] = snapshot(
        [phone_job(1, name="laptop.iso"), phone_job(2, name="old.iso", status="failed")]
    )
    page.refresh()
    select(page, "LAPTOP")
    assert page.name_label.text() == "LAPTOP"
    assert page.jobs_caption.text() == "DOWNLOADS ON THIS PC"
    assert page.send_row.isHidden() and page.send_caption.isHidden(), "a PC never pulls links"
    assert sorted(action_buttons(page, 0)) == ["cancel", "moveToPc", "pause"]
    action_buttons(page, 0)["moveToPc"].click()
    assert service.calls[-1] == ("command", ("c1", 1, "moveToPc"))
    service.commandFinished.emit(1, "ok", "")
    assert page.result_label.text() == "Download on this PC instead: Done"
    action_buttons(page, 1)["retry"].click()
    service.commandFinished.emit(2, "refused", "")
    assert page.result_label.text() == "Retry: That PC did not allow that"


def test_pc_wording_for_a_pc_that_does_not_share_or_is_old() -> None:
    service, _links, page = make(
        devices=[device("c1", "LAPTOP", kind="pc")], links=[], online={"c1"}
    )
    service.lists["c1"] = snapshot([], shared=False)
    page.refresh()
    select(page, "LAPTOP")
    assert page.message_label.text().startswith("This PC doesn't share its downloads.")
    service.lists.clear()
    service.online = set()
    service.deviceOnlineChanged.emit("c1")
    assert page.message_label.text() == "Its downloads show up here once that PC connects."


def test_the_allow_prompt_says_when_the_device_is_a_pc() -> None:
    service, _links, page = make()
    page.pair_button.click()
    service.pairingPending.emit("LAPTOP", "pc")
    assert page.allow_box is not None
    assert page.allow_box.text() == 'Allow "LAPTOP" (a PC) to control GrabLine?'
    page.allow_box.done(0)
    service.pairingPending.emit("Pixel 9", "phone")
    assert (
        page.allow_box is not None
        and page.allow_box.text() == 'Allow "Pixel 9" to control GrabLine?'
    )
    page.allow_box.done(0)


# ---------------------------------------------------------- the same PC both ways


def both_ways() -> tuple[FakeService, FakeLinks, DevicesPage]:
    service, links, page = make(
        devices=[device("c1", "DESKTOP", kind="pc", client_pc_id="L1")],
        links=[link("L1", "desktop")],
        online={"c1"},
    )
    service.lists["c1"] = snapshot([phone_job(1, name="its-own.iso")])
    page.refresh()
    return service, links, page


def test_pcs_with_the_same_name_stay_separate_without_a_matching_id() -> None:
    """Names are never compared: two PCs may share a hostname."""
    _service, _links, page = make(
        devices=[
            device("c1", "DESKTOP", kind="pc"),  # an older build: no clientPcId
            device("c2", "DESKTOP", kind="pc", client_pc_id="SOMEONE-ELSE-0000000"),
        ],
        links=[link("L1", "DESKTOP")],
    )
    assert row_names(page.pc_list) == ["DESKTOP", "DESKTOP", "DESKTOP"]
    assert page.pc_list.count() == 3
    assert [e.link is not None and e.device is not None for e in page._entries] == [False] * 3


def test_a_pc_is_merged_only_by_its_client_pc_id() -> None:
    _service, _links, page = make(
        devices=[
            device("c1", "LAPTOP", kind="pc", client_pc_id="L2"),
            device("c2", "OTHER", kind="pc", client_pc_id="L1"),
        ],
        links=[link("L1", "LAPTOP"), link("L2", "somebody")],
    )
    pairs = {(e.device.device_id, e.link.pc_id) for e in page._entries if e.device and e.link}
    assert pairs == {("c1", "L2"), ("c2", "L1")}, "matched by id even when the names differ"
    assert page.pc_list.count() == 2


def test_a_phone_is_never_merged_with_a_link() -> None:
    _service, _links, page = make(
        devices=[device("p1", "DESKTOP", client_pc_id="L1")], links=[link("L1", "DESKTOP")]
    )
    assert page.device_list.count() == 1 and page.pc_list.count() == 1


def test_a_pc_linked_both_ways_is_shown_once_with_a_note() -> None:
    _service, _links, page = both_ways()
    assert row_names(page.pc_list) == ["DESKTOP"]  # the device's own name, not the link's
    assert page.pc_list.count() == 1 and page.device_list.count() == 0
    select(page, row_names(page.pc_list)[0])
    assert not page.merged_note.isHidden()
    assert page.merged_note.text() == (
        "This PC is linked both ways, so it is shown once. Also linked as desktop"
    )
    assert page.name_label.text() == "DESKTOP"


def test_both_ways_prefers_the_live_link_and_falls_back_to_the_reported_list() -> None:
    service, links, page = both_ways()
    select(page, row_names(page.pc_list)[0])
    assert first_name(page) == "on-desktop.iso"
    links.infos = [link("L1", "desktop", "offline")]
    links.linksChanged.emit()
    assert first_name(page) == "its-own.iso", "the device is online, the link is not"
    assert "moveToPc" in action_buttons(page, 0)
    action_buttons(page, 0)["pause"].click()
    assert service.calls[-1] == ("command", ("c1", 1, "pause"))
    service.commandFinished.emit(1, "ok", "")
    assert ("d:c1", 1) not in page._busy


@pytest.mark.parametrize(
    ("side", "device_gone", "link_gone"),
    [("device", True, False), ("link", False, True), (None, False, False)],
)
def test_removing_a_pc_linked_both_ways_removes_only_the_chosen_side(
    monkeypatch: pytest.MonkeyPatch, side: str | None, device_gone: bool, link_gone: bool
) -> None:
    service, links, page = both_ways()
    select(page, row_names(page.pc_list)[0])
    asked: list[Any] = []

    def choose(entry: Any) -> str | None:
        asked.append((entry.device.name, entry.link.name))
        return side

    monkeypatch.setattr(page, "_choose_removal", choose)
    monkeypatch.setattr(page, "_confirm_remove", lambda _name: pytest.fail("one question only"))
    page.remove_button.click()
    assert asked == [("DESKTOP", "desktop")], "both names are shown"
    assert (("remove_device", "c1") in service.calls) is device_gone
    assert (("remove_link", "L1") in links.calls) is link_gone


def test_the_removal_question_names_both_and_offers_each_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _service, _links, page = both_ways()
    select(page, row_names(page.pc_list)[0])
    seen: dict[str, Any] = {}

    def fake_exec(box: QMessageBox) -> int:
        seen["text"] = box.text()
        seen["buttons"] = [b.text() for b in box.buttons()]
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    page.remove_button.click()
    assert "DESKTOP" in seen["text"] and "desktop" in seen["text"]
    assert sorted(seen["buttons"]) == sorted(
        ["Remove the device that linked to this PC", "Remove the link to that PC", "Cancel"]
    )
    assert page.pc_list.count() == 1, "cancelled: nothing was removed"


def test_remove_confirmation_names_the_pc(monkeypatch: pytest.MonkeyPatch) -> None:
    _service, _links, page = make()
    select(page, "DESKTOP")
    seen: dict[str, str] = {}

    def fake_exec(box: QMessageBox) -> int:
        seen["text"] = box.text()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    page.remove_button.click()
    assert seen["text"] == "Remove DESKTOP? It will stop working at once."
    assert page.pc_list.count() == 1


# ------------------------------------------------------------ strings and a11y


def test_hostile_names_are_shown_as_plain_text() -> None:
    from PySide6.QtCore import Qt

    evil = '<a href="http://evil.example">click</a><img src=x>'
    _service, links, page = make(links=[link("L1", evil)])
    links.jobs["L1"] = snapshot([phone_job(1, name=evil)])
    select(page, evil)
    marked = [lbl for lbl in page.findChildren(QLabel) if "<a href" in lbl.text()]
    assert marked, "the name is on the page as text"
    for label in marked:
        assert label.textFormat() == Qt.TextFormat.PlainText
        assert not label.openExternalLinks()


def test_every_control_has_an_accessible_name_with_links() -> None:
    _service, _links, page = make(
        devices=[device("p1", "Pixel 8"), device("c1", "LAPTOP", kind="pc")]
    )
    for name in ("Pixel 8", "LAPTOP", "DESKTOP"):
        select(page, name)
        for control in page.findChildren(QPushButton) + page.findChildren(QLineEdit):
            label = control.accessibleName() or (
                control.text() if isinstance(control, QPushButton) else ""
            )
            assert label, f"{control!r} has no accessible name"
    assert page.link_button.accessibleName() == "Link to another PC"
    assert page.pair_button.accessibleName() == "Pair a phone or PC"


def test_tooltips_are_plain_text() -> None:
    evil = "<img src=x onerror=alert(1)><b>bold</b>"
    _service, links, page = make(links=[link("L1", evil)])
    links.jobs["L1"] = snapshot([phone_job(1, name=evil, status="failed", error=evil)])
    select(page, evil)
    item = page.table.item(0, 0)
    assert item is not None
    tips = [item.toolTip(), page.name_label.toolTip()]
    holder = page.table.cellWidget(0, 1)
    assert holder is not None
    tips += [lbl.toolTip() for lbl in holder.findChildren(QLabel) if lbl.toolTip()]
    assert all(tips)
    for tip in tips:
        assert "<img" not in tip and "<b>" not in tip, tip


# --------------------------------------------------------------- the dialog


def dialog(links: FakeLinks) -> LinkDialog:
    dlg = LinkDialog(links)
    dlg.show()
    return dlg


def test_the_link_button_opens_the_dialog() -> None:
    _service, links, page = make()
    page.link_button.click()
    assert page.link_dialog is not None and page.link_dialog.isVisible()
    first = page.link_dialog
    page.link_button.click()
    assert page.link_dialog is first
    first.close()
    assert page.link_dialog is None
    assert links.calls == []


def test_the_dialog_starts_empty_with_named_controls() -> None:
    links = FakeLinks()
    dlg = dialog(links)
    assert dlg.windowTitle() == "Link to another PC"
    assert not dlg.link_button.isEnabled()
    assert (
        dlg.address_edit.accessibleName() == "Address" and dlg.code_edit.accessibleName() == "Code"
    )
    assert (
        dlg.link_button.accessibleName() == "Link" and dlg.close_button.accessibleName() == "Cancel"
    )
    assert dlg.status_label.isHidden()
    dlg.address_edit.setText("192.168.1.20:47821")
    assert not dlg.link_button.isEnabled()
    dlg.code_edit.setText("7K3M-9QX2-RTBW")
    assert dlg.link_button.isEnabled()


def test_a_link_attempt_shows_connecting_then_confirm_then_success() -> None:
    links = FakeLinks([link("L9", "STUDY PC")])
    dlg = dialog(links)
    dlg.address_edit.setText("192.168.1.20")
    dlg.code_edit.setText("7K3M-9QX2-RTBW")
    dlg.link_button.click()
    assert links.calls == [("add_link", "192.168.1.20")]
    assert dlg.status_label.text() == "Connecting…"
    assert not dlg.address_edit.isEnabled() and not dlg.code_edit.isEnabled()
    assert not dlg.link_button.isEnabled(), "no second attempt while one is running"
    links.linkProgress.emit("confirm")
    assert dlg.status_label.text() == "Confirm on the other PC"
    links.linkFinished.emit("ok", "L9")
    assert dlg.status_label.text() == "Linked to STUDY PC."
    assert dlg.code_edit.text() == "", "the code is cleared once it has been used"
    assert dlg.link_button.isHidden() and dlg.close_button.text() == "Close"


@pytest.mark.parametrize(
    ("outcome", "text"),
    [
        ("invalidCode", "That code doesn't look right"),
        ("badAddress", "That address doesn't look right"),
        ("badCode", "That code didn't match"),
        ("conflict", "You're already linked to a PC with this ID. Remove that link first."),
        ("proofMismatch", "That code didn't match"),
        ("locked", "Too many wrong codes. Show a new code on the other PC"),
        ("expired", "Code expired. Show a new code on the other PC"),
        ("declined", "The other PC declined"),
        ("notFound", "PC not found"),
        ("timeout", "The other PC didn't answer"),
        ("connectionLost", "The connection was lost. Try again"),
        ("full", "That PC already has 8 devices linked"),
        ("hostTooOld", "Update GrabLine on the other PC"),
        ("clientTooOld", "Update GrabLine on this PC"),
        ("failed", "Pairing failed. Try again."),
        ("something-new", "Pairing failed. Try again."),
    ],
)
def test_failures_are_worded_and_the_form_can_be_corrected(outcome: str, text: str) -> None:
    links = FakeLinks()
    dlg = dialog(links)
    dlg.address_edit.setText("10.0.0.2")
    dlg.code_edit.setText("7K3M-9QX2-RTBW")
    dlg.link_button.click()
    links.linkFinished.emit(outcome, "")
    assert dlg.status_label.text() == text == failure_text(outcome)
    assert dlg.status_label.property("role") == "warn"
    assert dlg.address_edit.isEnabled() and dlg.code_edit.isEnabled()
    assert dlg.address_edit.text() == "10.0.0.2", "the address is kept for another try"
    assert dlg.link_button.isEnabled() and not dlg.link_button.isHidden()


def test_a_start_that_fails_at_once_is_reported_through_the_signal() -> None:
    links = FakeLinks()
    links.add_starts = False
    dlg = dialog(links)
    dlg.address_edit.setText("x")
    dlg.code_edit.setText("123")
    dlg.link_button.click()
    assert not dlg.link_button.isEnabled()  # waiting for the service's answer
    links.linkFinished.emit("invalidCode", "")
    assert dlg.status_label.text() == "That code doesn't look right"


def test_closing_the_dialog_during_an_attempt_cancels_it() -> None:
    links = FakeLinks()
    dlg = dialog(links)
    dlg.address_edit.setText("10.0.0.2")
    dlg.code_edit.setText("7K3M-9QX2-RTBW")
    dlg.link_button.click()
    dlg.close()
    assert ("cancel_add", None) in links.calls
    assert dlg.code_edit.text() == ""


def test_finishing_signals_for_other_dialogs_are_ignored() -> None:
    links = FakeLinks()
    dlg = dialog(links)
    links.linkFinished.emit("badCode", "")  # nothing was started here
    assert dlg.status_label.isHidden()


def test_the_code_never_reaches_an_accessible_text_or_the_calls() -> None:
    links = FakeLinks()
    dlg = dialog(links)
    dlg.address_edit.setText("10.0.0.2")
    dlg.code_edit.setText("7K3M-9QX2-RTBW")
    dlg.link_button.click()
    texts = [dlg.status_label.text(), dlg.status_label.accessibleName(), dlg.instruction.text()]
    assert not any("7K3M" in text for text in texts)
    assert not any("7K3M" in str(call) for call in links.calls)


# --------------------------------------------------------------- the settings


def test_the_settings_page_has_the_sharing_switch() -> None:
    from app.ui.phone_page import PhonePage

    service = FakeService()
    links = FakeLinks()
    page = PhonePage(service, links=links)
    page.show()
    assert page.share_check.text() == "Let linked PCs see and control downloads on this PC"
    assert page.share_check.isChecked() and not page.share_check.isHidden()
    assert page.share_check.accessibleName() == page.share_check.text()
    page.share_check.click()
    assert links.shared is False and links.calls[-1] == ("set_shared", False)
    page.share_check.click()
    assert links.shared is True
    assert page.enabled_check.text() == "Allow phones and PCs to connect"
    without = PhonePage(service)
    without.show()
    assert without.share_check.isHidden()


def test_the_sharing_switch_starts_from_the_stored_setting() -> None:
    from app.ui.phone_page import PhonePage

    links = FakeLinks()
    links.shared = False
    page = PhonePage(FakeService(), links=links)
    assert page.share_check.isChecked() is False


def test_main_window_creates_the_link_service_and_passes_it_on(db: Any, tmp_path: Path) -> None:
    from app.core.manager import DownloadManager
    from app.core.settings import Settings
    from app.remote.client.service import LinkService
    from app.ui.main_window import MainWindow

    ensure_app()
    settings = Settings(db)
    settings.download_dir = tmp_path
    manager = DownloadManager(db, settings=settings, max_concurrent=0)
    window = MainWindow(manager, settings)
    try:
        assert isinstance(window._link_service, LinkService)
        page = window._make_devices_page()
        assert isinstance(page, DevicesPage)
        assert page.link_button.isHidden() is False
        assert page._links is window._link_service
    finally:
        window.shutdown()
        manager.shutdown()


def test_redrawing_leaves_no_stray_action_buttons() -> None:
    """Replaced or removed cell widgets must be hidden at once (their deletion is
    deferred), or they keep painting as stray icons under the rows."""
    _service, links, page = make()
    select(page, "DESKTOP")
    page.retint()
    page.retint()
    links.jobs["L1"] = snapshot([phone_job(7, name="only.iso", status="downloading")])
    links.hostJobsChanged.emit("L1")
    page.retint()
    in_rows = [
        b
        for b in page.table.viewport().findChildren(QAbstractButton)
        if b.isVisibleTo(page.table.viewport())
    ]
    assert sorted(str(b.property("action")) for b in in_rows) == ["cancel", "pause"]


def test_waiting_link_is_disabled_and_cancel_is_never_the_primary_button() -> None:
    links = FakeLinks()
    dlg = dialog(links)
    dlg.address_edit.setText("10.0.0.2")
    dlg.code_edit.setText("7K3M-9QX2-RTBW")
    assert dlg.link_button.property("accent") == "true"
    dlg.link_button.click()
    for step in ("connecting", "confirm"):
        links.linkProgress.emit(step)
        assert not dlg.link_button.isEnabled()
        assert dlg.link_button.property("accent") == "false"
    assert not dlg.close_button.isDefault() and not dlg.close_button.autoDefault()
    assert dlg.close_button.property("accent") is None
    links.linkFinished.emit("badCode", "")
    assert dlg.link_button.isEnabled() and dlg.link_button.property("accent") == "true"


def test_m1_a_pc_id_claimed_twice_is_never_merged() -> None:
    """Two devices claiming the same PC id: neither is merged with the link."""
    _service, _links, page = make(
        devices=[
            device("c1", "LAPTOP", kind="pc", client_pc_id="L1"),
            device("c2", "IMPOSTOR", kind="pc", client_pc_id="L1"),
        ],
        links=[link("L1", "DESKTOP")],
    )
    assert page.pc_list.count() == 3
    assert not any(e.device is not None and e.link is not None for e in page._entries)


def test_m1_a_merged_row_shows_the_devices_own_name_and_the_links_name() -> None:
    _service, _links, page = make(
        devices=[device("c1", "LAPTOP", kind="pc", client_pc_id="L1")],
        links=[link("L1", "SOMETHING-ELSE")],
    )
    assert row_names(page.pc_list) == ["LAPTOP"]
    select(page, "LAPTOP")
    assert page.name_label.text() == "LAPTOP"
    assert "Also linked as SOMETHING-ELSE" in page.merged_note.text()
