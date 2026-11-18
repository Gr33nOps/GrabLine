"""The Devices page: phones and PCs, links to and from them, and their downloads
(PROTOCOL.md sections 20 and 21).

Left: "Devices" in two groups. Phones are the paired Android phones. PCs are the
PCs that linked to this one (devices of kind ``pc``, shown like a phone: the
downloads they report and the same controls) and the PCs this one linked to
(the other PC's downloads, live, with Pause/Resume/Cancel/Retry/Remove and a
field that adds a link on that PC). A PC that appears both ways is shown once.
Buttons: "Pair a phone or PC" (this PC's code) and "Link to another PC".

Right, for what is picked on the left: its name and state with "Remove", a
field to send or add a link, and the downloads on it, a live table with the
actions that fit each download. Commands go out only from a click here; the
outcome of each one is shown under the table.

The page talks to ``app.remote.service.RemoteService`` and
``app.remote.client.service.LinkService`` only through their public API and Qt
signals. Every string that came from another device is shown as plain text.
The pairing code lives in the pairing dialog and nowhere else.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QDateTime, QEvent, QLocale, QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import N_, t
from app.remote.phonejobs import PhoneJob, PhoneSnapshot, neutralise
from app.ui import components, design, motion, theme
from app.ui.format import human_bytes, plain_tooltip
from app.ui.link_dialog import LinkDialog
from app.ui.phone_page import PairingDialog

#: How long a phone may be connected without sending a list before the PC
#: concludes its app is too old to share downloads.
LIST_GRACE_S = 4.0
#: How long a command result stays under the table.
RESULT_SHOWN_MS = 8000
#: How long "Sent to your phone" stays under the send box.
NOTE_SHOWN_MS = 4000
#: Phone list updates redraw the page at most this often (seconds).
RENDER_MIN_INTERVAL_S = 0.25
#: Below this table width the size and time-left columns are hidden.
_NARROW_PX = 700
#: Below this the speed goes too (the time left goes first, then the speed).
_VERY_NARROW_PX = 600

_COLUMNS = (N_("Name"), N_("Status"), N_("Progress"), N_("Speed"), N_("Time left"), "")
_C_NAME, _C_STATUS, _C_PROGRESS, _C_SPEED, _C_ETA, _C_ACTIONS = range(6)

#: action -> (label, icon, danger)
_ACTIONS: dict[str, tuple[str, str, bool]] = {
    "pause": (N_("Pause"), "pause", False),
    "resume": (N_("Resume"), "resume", False),
    "cancel": (N_("Cancel"), "cancel", True),
    "retry": (N_("Retry"), "retry", False),
    "remove": (N_("Remove"), "trash", True),
    "moveToPc": (N_("Download on this PC instead"), "download", False),
}

#: What a command's outcome says when the phone gave no words of its own.
_OK_TEXT = {
    "pause": N_("Paused on the phone"),
    "resume": N_("Resumed on the phone"),
    "cancel": N_("Cancelled on the phone"),
    "retry": N_("Retrying on the phone"),
    "remove": N_("Removed from the phone"),
    "moveToPc": N_("Sent to this PC to download"),
}
_RESULT_TEXT = {
    "ok": N_("Done"),
    "notFound": N_("The phone no longer has that download"),
    "refused": N_("The phone did not allow that"),
    "failed": N_("The phone could not do that"),
    "timeout": N_("The phone did not answer"),
    "offline": N_("The phone is not connected"),
    "busy": N_("Too many commands at once. Try again in a moment."),
}
#: The same outcomes for a PC (the words say "that PC", not "the phone").
_PC_RESULT_TEXT = {
    "ok": N_("Done"),
    "notFound": N_("That PC no longer has that download"),
    "refused": N_("That PC did not allow that"),
    "failed": N_("That PC could not do that"),
    "timeout": N_("That PC did not answer"),
    "offline": N_("That PC is not connected"),
    "busy": N_("Too many commands at once. Try again in a moment."),
    "conflict": N_("That can't be done right now"),
    "unsupported": N_("Update GrabLine on that PC to use this"),
}

#: What a link's state says when it is not simply online.
_LINK_STATE_TEXT = {
    "connecting": N_("Connecting…"),
    "offline": N_("Offline"),
    "identityChanged": N_("This PC's identity changed. Link it again"),
    "removed": N_("This PC no longer accepts this one. Link it again"),
    "hostTooOld": N_("Update GrabLine on the other PC"),
    "clientTooOld": N_("Update GrabLine on this PC"),
}
_LINK_STOPPED = ("identityChanged", "removed", "hostTooOld", "clientTooOld")


def action_label(action: str) -> str:
    return t(_ACTIONS[action][0]) if action in _ACTIONS else action


def result_text(result: str, message: str, *, pc: bool = False) -> str:
    """The words for a command outcome. The phone's own note wins for the
    answers the phone gave; the PC's conclusions use fixed sentences."""
    if message and result in ("ok", "notFound", "refused", "failed"):
        return neutralise(message)
    table = _PC_RESULT_TEXT if pc else _RESULT_TEXT
    return t(
        table.get(result, "That PC could not do that" if pc else "The phone could not do that")
    )


def _elided(
    text: str, role: str, *, size: int | None = None, bold: bool = False
) -> components.ElidingLabel:
    """A one-line label for text a phone supplied: never rich text, no links."""
    label = components.ElidingLabel("", role, size=size, bold=bold)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setText(text)
    return label


def _wrapping(label: QLabel) -> QLabel:
    label.setWordWrap(True)
    label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    label.setMinimumWidth(0)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def _repolish(widget: QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


def _format_time(unix: int) -> str:
    return QLocale().toString(
        QDateTime.fromSecsSinceEpoch(int(unix)), QLocale.FormatType.ShortFormat
    )


@dataclass(frozen=True)
class _Entry:
    """One row on the left. ``device`` is a phone or a PC that linked to this
    one; ``link`` is a PC this one linked to. A PC both ways has both."""

    group: str  # "phone" or "pc"
    device: Any = None  # a paired device (``RemoteService`` store row)
    link: Any = None  # a ``LinkInfo``

    @property
    def key(self) -> str:
        if self.link is not None:
            return f"l:{self.link.pc_id}"
        return f"d:{self.device.device_id}"

    @property
    def name(self) -> str:
        """The device's own name when there is one (a merged row shows what the
        PC called itself when it linked here, never only the name a host gave)."""
        return str(self.device.name if self.device is not None else self.link.name)


def _kind_of(device: Any) -> str:
    return "pc" if getattr(device, "kind", "phone") == "pc" else "phone"


class DevicesPage(QWidget):
    """Sidebar page "Devices"."""

    #: Kept for callers that want a status-bar line; the page itself shows
    #: everything inline and does not emit it.
    status = Signal(str)

    def __init__(self, service: Any, parent: QWidget | None = None, *, links: Any = None) -> None:
        super().__init__(parent)
        self._service = service
        self._links = links
        #: key of the selected row: "d:<deviceId>" or "l:<pcId>"
        self._sel: str | None = None
        self._entries: list[_Entry] = []
        self._row_ids: list[int] = []
        #: the job the user picked, by id, so a rebuild keeps it
        self._selected_job: int | None = None
        #: the snapshot the table was last drawn from; the action row resolves
        #: its job from this, never from a newer one
        self._rendered: PhoneSnapshot | None = None
        self._bar_sig: tuple[Any, ...] | None = None
        self._row_sigs: dict[int, tuple[Any, ...]] = {}
        self._bars: list[motion.SmoothProgressBar] = []
        self._online_since: dict[str, float] = {}
        #: command id -> (device id, job id, action), for commands to a device
        self._commands: dict[int, tuple[str, int, str]] = {}
        #: request token -> (pc id, job id, action), for controls sent over a link
        self._link_commands: dict[int, tuple[str, int, str]] = {}
        #: token -> pc id, for links added with ``download``
        self._link_sends: dict[int, str] = {}
        #: (row key, job id) with a command waiting for its answer
        self._busy: set[tuple[str, int]] = set()
        self._last_render = 0.0
        self.pairing_dialog: PairingDialog | None = None
        self.link_dialog: LinkDialog | None = None
        self.allow_box: QMessageBox | None = None
        self._allow_button: QPushButton | None = None

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_left())
        outer.addWidget(self._build_right(), 1)

        self._grace_timer = QTimer(self)
        self._grace_timer.setSingleShot(True)
        self._grace_timer.timeout.connect(self._render_right)
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._render_throttled)
        self._result_timer = QTimer(self)
        self._result_timer.setSingleShot(True)
        self._result_timer.timeout.connect(lambda: self.result_label.setText(""))
        self._note_timer = QTimer(self)
        self._note_timer.setSingleShot(True)
        self._note_timer.timeout.connect(self._clear_note)

        service.devicesChanged.connect(self.refresh)
        service.deviceOnlineChanged.connect(self._on_presence)
        service.phoneJobsChanged.connect(self._on_jobs_changed)
        service.commandFinished.connect(self._on_command_finished)
        service.pairingStarted.connect(self._on_pairing_started)
        service.pairingPending.connect(self._on_pairing_pending)
        service.pairingLockedWarning.connect(self._on_locked_warning)
        service.pairingFinished.connect(self._on_pairing_finished)
        if links is not None:
            links.linksChanged.connect(self.refresh)
            links.linkStateChanged.connect(self._on_link_state)
            links.hostJobsChanged.connect(self._on_jobs_changed)
            links.requestFinished.connect(self._on_request_finished)

        now = time.monotonic()
        for device in service.store.list():
            if service.is_online(device.device_id):
                self._online_since[device.device_id] = now
        self.refresh()

    # ---------------------------------------------------------------- layout

    def _build_left(self) -> QWidget:
        left = QFrame()
        left.setObjectName("DevicesNav")
        left.setMinimumWidth(200)
        left.setMaximumWidth(230)
        left.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        col = QVBoxLayout(left)
        col.setContentsMargins(12, 14, 12, 12)
        col.setSpacing(8)

        self.phones_label = components.SectionLabel(t("Phones"))
        col.addWidget(self.phones_label)
        self.device_list = self._make_list(t("Phones"))
        col.addWidget(self.device_list)

        self.pcs_label = components.SectionLabel(t("PCs"))
        col.addWidget(self.pcs_label)
        self.pc_list = self._make_list(t("PCs"))
        col.addWidget(self.pc_list)

        self.no_devices_label = _wrapping(
            components.role_label(t("Nothing is paired or linked yet."), "muted")
        )
        self.no_devices_label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
        )
        self.no_devices_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        col.addWidget(self.no_devices_label, 1)
        col.addStretch(1)

        self.pair_button = components.accent_button(t("Pair a phone or PC"))
        self.pair_button.setAccessibleName(t("Pair a phone or PC"))
        self.pair_button.clicked.connect(self._pair)
        col.addWidget(self.pair_button)
        self.link_button = QPushButton(t("Link to another PC"))
        self.link_button.setAccessibleName(t("Link to another PC"))
        self.link_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.link_button.clicked.connect(self._open_link_dialog)
        self.link_button.setVisible(self._links is not None)
        col.addWidget(self.link_button)
        return left

    def _make_list(self, name: str) -> QListWidget:
        widget = QListWidget()
        widget.setObjectName("DevicesList")
        widget.setAccessibleName(name)
        widget.setFrameShape(QFrame.Shape.NoFrame)
        widget.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        # Selection (not the current item) drives the page: the list that is not
        # active keeps a current item but no selection, so a focus change in it
        # can never switch the page by itself.
        widget.itemSelectionChanged.connect(lambda w=widget: self._on_selection_changed(w))
        return widget

    def _build_right(self) -> QWidget:
        self.right_stack = QStackedWidget()
        self.right_stack.setObjectName("BareContainer")
        self.right_stack.addWidget(self._build_empty_state())  # 0
        self.right_stack.addWidget(self._build_detail())  # 1
        return self.right_stack

    def _build_empty_state(self) -> QWidget:
        page = QWidget()
        page.setObjectName("BareContainer")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(32, 32, 32, 32)
        lay.addStretch(1)
        card = components.card_frame()
        card.setMaximumWidth(440)
        card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(24, 20, 24, 22)
        inner.setSpacing(8)
        self.empty_title = components.role_label(
            t("No phones or PCs yet"), "strong", size=design.FONT["h1"], bold=True
        )
        inner.addWidget(self.empty_title)
        self.empty_body = _wrapping(
            components.role_label(
                t(
                    "Pair your Android phone or link another PC to send it links, see and "
                    "control its downloads here, and start downloads on this PC from there."
                ),
                "dim",
            )
        )
        inner.addWidget(self.empty_body)
        centered = QHBoxLayout()
        centered.addStretch(1)
        centered.addWidget(card, 4)
        centered.addStretch(1)
        lay.addLayout(centered)
        lay.addStretch(2)
        return page

    def _build_detail(self) -> QWidget:
        page = QWidget()
        page.setObjectName("BareContainer")
        col = QVBoxLayout(page)
        col.setContentsMargins(24, 18, 24, 14)
        col.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(12)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.name_label = _elided("", "strong", size=design.FONT["h1"], bold=True)
        self.state_label = _elided("", "muted", size=design.FONT["small"])
        titles.addWidget(self.name_label)
        titles.addWidget(self.state_label)
        head.addLayout(titles, 1)
        self.remove_button = QPushButton(t("Remove"))
        self.remove_button.setProperty("danger", "true")
        self.remove_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remove_button.clicked.connect(self._remove_selected)
        head.addWidget(self.remove_button, 0, Qt.AlignmentFlag.AlignTop)
        col.addLayout(head)

        self.merged_note = _wrapping(components.role_label("", "muted", size=design.FONT["small"]))
        self.merged_note.hide()
        col.addWidget(self.merged_note)
        self.share_note = _wrapping(components.role_label("", "muted", size=design.FONT["small"]))
        self.share_note.hide()
        col.addWidget(self.share_note)

        self.send_caption = components.SectionLabel(t("Send a link to this phone"))
        col.addWidget(self.send_caption)
        send_row = QHBoxLayout()
        send_row.setSpacing(8)
        self.link_edit = QLineEdit()
        self.link_edit.setPlaceholderText(t("Paste a link to send"))
        self.link_edit.setAccessibleName(t("Send a link to this phone"))
        self.link_edit.setClearButtonEnabled(True)
        self.link_edit.returnPressed.connect(self._send_link)
        self.link_edit.textChanged.connect(self._update_send_enabled)
        send_row.addWidget(self.link_edit, 1)
        self.send_button = components.accent_button(t("Send"))
        self.send_button.setAccessibleName(t("Send"))
        self.send_button.clicked.connect(self._send_link)
        send_row.addWidget(self.send_button)
        self.send_row = QWidget()
        self.send_row.setObjectName("BareContainer")
        send_row_box = QVBoxLayout(self.send_row)
        send_row_box.setContentsMargins(0, 0, 0, 0)
        send_row_box.setSpacing(0)
        send_row_box.addLayout(send_row)
        col.addWidget(self.send_row)
        self.send_note = _wrapping(components.role_label("", "muted", size=design.FONT["small"]))
        self.send_note.hide()
        col.addWidget(self.send_note)

        col.addSpacing(4)
        self.jobs_caption = components.SectionLabel(t("Downloads on this phone"))
        col.addWidget(self.jobs_caption)
        self.banner_label = _wrapping(components.role_label("", "muted", size=design.FONT["small"]))
        self.banner_label.hide()
        col.addWidget(self.banner_label)

        self.jobs_stack = QStackedWidget()
        self.jobs_stack.setObjectName("BareContainer")
        self.message_label = _wrapping(components.role_label("", "muted"))
        self.message_label.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.message_label.setContentsMargins(0, 28, 0, 0)
        self.message_label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.jobs_stack.addWidget(self.message_label)  # 0
        self.jobs_stack.addWidget(self._build_table())  # 1
        col.addWidget(self.jobs_stack, 1)

        self.action_bar = QWidget()
        self.action_bar.setObjectName("BareContainer")
        self.action_bar.setAccessibleName(t("Downloads on this phone"))
        self.action_layout = QHBoxLayout(self.action_bar)
        self.action_layout.setContentsMargins(4, 0, 4, 0)
        self.action_layout.setSpacing(4)
        self.action_bar.hide()
        col.addWidget(self.action_bar)

        self.result_label = _wrapping(components.role_label("", "dim"))
        self.result_label.setAccessibleName(t("Downloads on this phone"))
        col.addWidget(self.result_label)
        return page

    def _build_table(self) -> QTableWidget:
        table = QTableWidget(0, len(_COLUMNS))
        table.setObjectName("JobsTable")
        table.setHorizontalHeaderLabels([t(c) if c else "" for c in _COLUMNS])
        table.setAccessibleName(t("Downloads on this phone"))
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(40)
        table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(self._show_row_menu)
        table.currentCellChanged.connect(self._on_current_cell)
        header = table.horizontalHeader()
        header.setHighlightSections(False)
        header.setStretchLastSection(False)
        header.setSectionResizeMode(_C_NAME, QHeaderView.ResizeMode.Stretch)
        for column, width in (
            (_C_STATUS, 118),
            (_C_PROGRESS, 172),
            (_C_SPEED, 96),
            (_C_ETA, 76),
            (_C_ACTIONS, 108),
        ):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            table.setColumnWidth(column, width)
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        for column in (_C_SPEED, _C_ETA):
            item = table.horizontalHeaderItem(column)
            if item is not None:
                item.setTextAlignment(right)
        self.table = table
        # The last list of a phone that is offline is shown greyed.
        self._dim = QGraphicsOpacityEffect(table)
        self._dim.setOpacity(0.5)
        self._dim.setEnabled(False)
        table.setGraphicsEffect(self._dim)
        table.viewport().installEventFilter(self)
        return table

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.table.viewport() and event.type() == QEvent.Type.Resize:
            self._fit_columns()
        return super().eventFilter(watched, event)

    def _fit_columns(self) -> None:
        """Narrow windows drop the figures that can wait, the time left first and
        then the speed, so the name has room. How much is done always stays,
        under the progress bar."""
        width = self.table.viewport().width()
        self.table.setColumnHidden(_C_ETA, width < _NARROW_PX)
        self.table.setColumnHidden(_C_SPEED, width < _VERY_NARROW_PX)

    # ------------------------------------------------------------- the list

    def _build_entries(self) -> list[_Entry]:
        """Phones, then PCs: the PCs that linked to this one and the PCs this
        one linked to, a PC that did both once."""
        devices = list(self._service.store.list())
        links = list(self._links.links()) if self._links is not None else []
        entries = [_Entry("phone", device=d) for d in devices if _kind_of(d) == "phone"]
        unmatched = list(links)
        pcs = [d for d in devices if _kind_of(d) == "pc"]
        claimed = [getattr(d, "client_pc_id", None) for d in pcs]
        for device in pcs:
            # The same PC both ways: the device says which PC it is (clientPcId)
            # and that is this link's host. Merged only when exactly one device
            # and one link share the id; names are never compared.
            own = getattr(device, "client_pc_id", None)
            candidates = [link for link in unmatched if own and link.pc_id == own]
            match = candidates[0] if len(candidates) == 1 and claimed.count(own) == 1 else None
            if match is not None:
                unmatched.remove(match)
            entries.append(_Entry("pc", device=device, link=match))
        entries.extend(_Entry("pc", link=link) for link in unmatched)
        return entries

    def refresh(self) -> None:
        """Rebuild the two lists, keeping the selection."""
        self._entries = self._build_entries()
        keys = [entry.key for entry in self._entries]
        if self._sel not in keys:
            self._sel = keys[0] if keys else None
            self._selected_job = None
        for widget, group in ((self.device_list, "phone"), (self.pc_list, "pc")):
            widget.blockSignals(True)
            widget.clear()
            shown = [e for e in self._entries if e.group == group]
            for entry in shown:
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, entry.key)
                detail, online = self._row_state(entry)
                item.setData(Qt.ItemDataRole.AccessibleTextRole, f"{entry.name}, {detail}")
                row = self._device_row(entry.name, detail, online)
                item.setSizeHint(row.sizeHint())
                widget.addItem(item)
                widget.setItemWidget(item, row)
                if entry.key == self._sel:
                    widget.setCurrentItem(item)
            if widget.count() and not widget.selectedItems():
                widget.setCurrentRow(0)
                widget.clearSelection()
            widget.blockSignals(False)
            widget.setVisible(bool(shown))
            rows = sum(widget.sizeHintForRow(r) for r in range(widget.count()))
            widget.setMaximumHeight(rows + 8 if rows else 0)
        self.phones_label.setVisible(self.device_list.count() > 0)
        self.pcs_label.setVisible(self.pc_list.count() > 0)
        self.no_devices_label.setVisible(not self._entries)
        self._render_right()

    def _entry(self, key: str | None) -> _Entry | None:
        for entry in self._entries:
            if entry.key == key:
                return entry
        return None

    def _row_state(self, entry: _Entry) -> tuple[str, bool]:
        """The line under a name on the left, and whether it is online."""
        if entry.link is not None and (entry.device is None or self._driver(entry) == "link"):
            return self._link_text(entry.link), bool(entry.link.online)
        assert entry.device is not None
        online = bool(self._service.is_online(entry.device.device_id))
        return self._seen_text(entry.device, online), online

    def _link_text(self, link: Any) -> str:
        if link.state == "online":
            return t("Online")
        if link.state == "offline":
            if link.last_seen_at:
                return t("Last seen {when}", when=_format_time(link.last_seen_at))
            return t("Offline")
        return t(_LINK_STATE_TEXT.get(link.state, "Offline"))

    def _seen_text(self, device: Any, online: bool) -> str:
        if online:
            return t("Online")
        if device.last_seen_at:
            return t("Last seen {when}", when=_format_time(device.last_seen_at))
        return t("Not connected yet")

    def _device_row(self, name: str, detail: str, online: bool) -> QWidget:
        holder = QWidget()
        holder.setObjectName("BareContainer")
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(1)
        title = _elided(name, "strong", bold=True)
        sub = _elided(detail, "ok" if online else "muted", size=design.FONT["small"])
        lay.addWidget(title)
        lay.addWidget(sub)
        return holder

    def _on_selection_changed(self, widget: QListWidget) -> None:
        chosen = widget.selectedItems()
        if not chosen:
            return
        key = chosen[0].data(Qt.ItemDataRole.UserRole)
        other = self.pc_list if widget is self.device_list else self.device_list
        other.blockSignals(True)
        other.clearSelection()
        other.blockSignals(False)
        if key != self._sel:
            self._sel = key
            self._selected_job = None
            self.link_edit.clear()
            self.send_note.setText("")
            self.send_note.hide()
            self.result_label.setText("")
            self._render_right()

    def selected_device(self) -> Any | None:
        """The paired device (phone, or PC that linked to this one) picked on
        the left, if the row has one."""
        entry = self._entry(self._sel)
        return entry.device if entry is not None else None

    def selected_link(self) -> Any | None:
        """The link to another PC picked on the left, if the row has one."""
        entry = self._entry(self._sel)
        return entry.link if entry is not None else None

    def _on_presence(self, device_id: str) -> None:
        if self._service.is_online(device_id):
            self._online_since[device_id] = time.monotonic()
        else:
            self._online_since.pop(device_id, None)
        self.refresh()

    def _on_link_state(self, _pc_id: str) -> None:
        self.refresh()

    # ------------------------------------------------------- the right side

    def _driver(self, entry: _Entry) -> str:
        """Which side supplies what the right pane shows: ``device`` (the
        downloads the device reported) or ``link`` (the other PC's downloads,
        live). A PC both ways uses the link while it is online."""
        if entry.link is None:
            return "device"
        if entry.device is None:
            return "link"
        if entry.link.online:
            return "link"
        if self._service.is_online(entry.device.device_id):
            return "device"
        return "link"

    def _render_right(self) -> None:
        self._last_render = time.monotonic()
        entry = self._entry(self._sel)
        if entry is None:
            self.right_stack.setCurrentIndex(0)
            return
        self.right_stack.setCurrentIndex(1)
        driver = self._driver(entry)
        if driver == "link":
            assert entry.link is not None
            self._render_link(entry, entry.link)
        else:
            assert entry.device is not None
            self._render_device(entry, entry.device)

    def _set_captions(self, *, pc: bool, link: bool) -> None:
        """The words that change with what is picked: a phone, a PC that linked
        to this one, or a PC this one linked to."""
        if link:
            send, jobs = t("Add a link to this PC"), t("Downloads on this PC")
            self.link_edit.setPlaceholderText(t("Paste a link to download on that PC"))
            self.send_button.setText(t("Add"))
            self.send_button.setAccessibleName(t("Add"))
        else:
            send = t("Send a link to this phone")
            jobs = t("Downloads on this PC") if pc else t("Downloads on this phone")
            self.link_edit.setPlaceholderText(t("Paste a link to send"))
            self.send_button.setText(t("Send"))
            self.send_button.setAccessibleName(t("Send"))
        self.send_caption.setText(send.upper())
        self.link_edit.setAccessibleName(send)
        self.jobs_caption.setText(jobs.upper())
        for widget in (self.action_bar, self.result_label, self.table):
            widget.setAccessibleName(jobs)
        # A PC that linked to this one never pulls links; only phones do.
        self.send_row.setVisible(link or not pc)
        self.send_caption.setVisible(link or not pc)

    def _merge_note(self, entry: _Entry) -> None:
        both = entry.device is not None and entry.link is not None
        self.merged_note.setVisible(both)
        if both:
            self.merged_note.setText(
                t("This PC is linked both ways, so it is shown once.")
                + " "
                + t("Also linked as {name}", name=entry.link.name)
            )

    def _render_device(self, entry: _Entry, device: Any) -> None:
        pc = entry.group == "pc"
        online = bool(self._service.is_online(device.device_id))
        self._set_captions(pc=pc, link=False)
        self._merge_note(entry)
        self.share_note.hide()
        self.name_label.setText(str(device.name))
        paired = t("Paired {date}", date=_format_time(device.created_at))
        seen = self._seen_text(device, online)
        self.state_label.setText(f"{seen}  ·  {paired}")
        self.state_label.setProperty("role", "ok" if online else "muted")
        _repolish(self.state_label)
        self.remove_button.setAccessibleName(f"{t('Remove')} {device.name}")
        self._update_send_enabled()
        self._render_jobs(entry, online, pc=pc)

    def _render_link(self, entry: _Entry, link: Any) -> None:
        online = bool(link.online)
        self._set_captions(pc=True, link=True)
        self._merge_note(entry)
        self.name_label.setText(entry.name)
        stopped = link.state in _LINK_STOPPED
        if online:
            state = t("Online")
        elif link.state == "offline" and link.last_seen_at:
            state = t("Last seen {when}", when=_format_time(link.last_seen_at))
        else:
            state = t(_LINK_STATE_TEXT.get(link.state, "Offline"))
        linked = t("Linked {date}", date=_format_time(link.paired_at))
        self.state_label.setText(state if stopped else f"{state}  ·  {linked}")
        self.state_label.setProperty("role", "ok" if online else ("warn" if stopped else "muted"))
        _repolish(self.state_label)
        self.remove_button.setAccessibleName(f"{t('Remove')} {link.name}")
        self._render_share_note(link)
        self._update_send_enabled()
        self._render_jobs(entry, online, pc=True)

    def _render_share_note(self, link: Any) -> None:
        """Tell the person when the other PC cannot see this PC's downloads."""
        text = ""
        if link.state == "online":
            if not link.shares_downloads:
                text = t("That PC can't see downloads on this PC. Update GrabLine on it.")
            elif self._links is not None and not self._links.shared_setting():
                text = t(
                    "Sharing is off, so that PC can't see or control downloads on this PC. "
                    "Turn it on in Settings, Phones and PCs."
                )
        self.share_note.setText(text)
        self.share_note.setVisible(bool(text))

    def _jobs_state(self, entry: _Entry, online: bool) -> tuple[str, PhoneSnapshot | None]:
        if self._driver(entry) == "link":
            link = entry.link
            assert link is not None
            if link.state in _LINK_STOPPED:
                return f"link_{link.state}", None
            snapshot = self._links.host_jobs(link.pc_id)
            if snapshot is None:
                return ("checking" if online else "offline_never"), None
            return ("jobs" if snapshot.jobs else "empty"), snapshot
        assert entry.device is not None
        device_id = entry.device.device_id
        snapshot = self._service.phone_jobs(device_id)
        if snapshot is None:
            if not online:
                return "offline_never", None
            waited = time.monotonic() - self._online_since.get(device_id, 0.0)
            return ("checking" if waited < LIST_GRACE_S else "older"), None
        if not snapshot.shared:
            return "not_shared", snapshot
        if not snapshot.jobs:
            return "empty", snapshot
        return "jobs", snapshot

    def _render_jobs(self, entry: _Entry, online: bool, *, pc: bool) -> None:
        state, snapshot = self._jobs_state(entry, online)
        driver_is_link = self._driver(entry) == "link"
        text = {
            "offline_never": (
                t("Its downloads show up here once that PC connects.")
                if pc
                else t("Its downloads show up here once the phone connects.")
            ),
            "checking": (
                t("Checking that PC's downloads…") if pc else t("Checking the phone's downloads…")
            ),
            "older": (
                t("Update GrabLine on that PC to see its downloads here.")
                if pc
                else t("Update GrabLine on the phone to see its downloads here.")
            ),
            "not_shared": (
                t(
                    "This PC doesn't share its downloads. "
                    "Turn it on in GrabLine on that PC: Settings, Phones and PCs."
                )
                if pc
                else t(
                    "This phone doesn't share its downloads. "
                    "Turn it on in GrabLine on the phone: Settings, PCs."
                )
            ),
            "empty": t("No downloads on that PC.") if pc else t("No downloads on this phone."),
        }.get(state)
        if text is None and state.startswith("link_"):
            text = t(_LINK_STATE_TEXT.get(state[5:], "Offline"))
        stale = not online and snapshot is not None and state == "jobs"
        self.banner_label.setVisible(stale)
        if stale:
            self.banner_label.setText(
                t("Offline. Showing the last list from that PC.")
                if pc
                else t("Offline. Showing the last list this phone sent.")
            )
        if state == "checking" and not driver_is_link:
            assert entry.device is not None
            remaining = LIST_GRACE_S - (
                time.monotonic() - self._online_since.get(entry.device.device_id, 0.0)
            )
            self._grace_timer.start(max(50, int(remaining * 1000) + 50))
        if text is not None:
            self.message_label.setText(text)
            self.message_label.setAccessibleName(text)
            self.jobs_stack.setCurrentIndex(0)
            self._clear_rows()
            self._refresh_action_bar()
            return
        assert snapshot is not None
        self.jobs_stack.setCurrentIndex(1)
        self._fill_rows(self._busy_key(), snapshot, online)
        self._dim.setEnabled(not online)

    def _on_jobs_changed(self, source_id: str) -> None:
        """A device reports at most once a second by its own rule, and a host's
        events come as they happen, but the page does not rely on that:
        re-drawing is limited to one per interval and the newest list wins."""
        entry = self._entry(self._sel)
        if entry is None:
            return
        ids = {
            getattr(entry.device, "device_id", None),
            getattr(entry.link, "pc_id", None),
        }
        if source_id not in ids:
            return
        wait = RENDER_MIN_INTERVAL_S - (time.monotonic() - self._last_render)
        if wait <= 0 and not self._render_timer.isActive():
            self._render_right()
        elif not self._render_timer.isActive():
            self._render_timer.start(max(1, int(wait * 1000) + 1))

    def _render_throttled(self) -> None:
        if self._sel is not None:
            self._render_right()

    # ------------------------------------------------------------ the table

    def _clear_rows(self) -> None:
        for bar in self._bars:
            bar.release()
        self._bars = []
        blocked = self.table.blockSignals(True)
        # Take the cell widgets out and hide them now: Qt deletes them later,
        # and until then they would still paint (a stray icon under the rows).
        for row in range(self.table.rowCount()):
            for column in range(self.table.columnCount()):
                holder = self.table.cellWidget(row, column)
                if holder is not None:
                    self.table.removeCellWidget(row, column)
                    holder.hide()
                    holder.deleteLater()
        self.table.setRowCount(0)
        self.table.blockSignals(blocked)
        self.table.viewport().update()
        self._row_ids = []
        self._row_sigs = {}

    def _fill_rows(self, key: str, snapshot: PhoneSnapshot, online: bool) -> None:
        ids = [job.id for job in snapshot.jobs]
        if ids != self._row_ids:
            self._rebuild_rows(snapshot)
        for row, job in enumerate(snapshot.jobs):
            self._update_row(row, key, job, online)
        self._rendered = snapshot
        # Keep the job the user picked (by id) across rebuilds; fall back to the
        # first row only when it is gone.
        wanted = (
            self._row_ids.index(self._selected_job) if self._selected_job in self._row_ids else 0
        )
        if snapshot.jobs and self.table.currentRow() != wanted:
            blocked = self.table.blockSignals(True)
            self.table.setCurrentCell(wanted, _C_NAME)
            self.table.blockSignals(blocked)
        if snapshot.jobs:
            self._selected_job = self._row_ids[wanted]
        self._refresh_action_bar()

    def _on_current_cell(self, row: int, _column: int, _prev_row: int, _prev_col: int) -> None:
        if 0 <= row < len(self._row_ids):
            self._selected_job = self._row_ids[row]
        self._refresh_action_bar()

    def _rebuild_rows(self, snapshot: PhoneSnapshot) -> None:
        self._clear_rows()
        self.table.setRowCount(len(snapshot.jobs))
        num_font = design.numeric_font(self.table.font())
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        for row, job in enumerate(snapshot.jobs):
            for column in (_C_NAME, _C_SPEED, _C_ETA):
                item = QTableWidgetItem("")
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if column != _C_NAME:
                    item.setTextAlignment(right)
                    item.setFont(num_font)
                self.table.setItem(row, column, item)
            pill = components.StatusPill(job.status)
            self.table.setCellWidget(row, _C_STATUS, self._pad(pill))
            bar = motion.SmoothProgressBar()
            self._bars.append(bar)
            done = components.role_label("", "dim", size=design.FONT["small"])
            done.setObjectName("DoneText")
            done.setTextFormat(Qt.TextFormat.PlainText)
            cell = QWidget()
            cell.setObjectName("BareContainer")
            box = QVBoxLayout(cell)
            box.setContentsMargins(6, 0, 6, 0)
            box.setSpacing(3)
            box.addStretch(1)
            box.addWidget(bar)
            box.addWidget(done)
            box.addStretch(1)
            self.table.setCellWidget(row, _C_PROGRESS, cell)
        self._row_ids = [job.id for job in snapshot.jobs]
        self._row_sigs = {}

    @staticmethod
    def _pad(widget: QWidget) -> QWidget:
        holder = QWidget()
        holder.setObjectName("BareContainer")
        lay = QHBoxLayout(holder)
        lay.setContentsMargins(6, 0, 6, 0)
        lay.addWidget(widget)
        return holder

    def _cell(self, row: int, column: int) -> QTableWidgetItem:
        item = self.table.item(row, column)
        assert item is not None
        return item

    def _busy_key(self) -> str:
        """Who a command in flight belongs to: the link while the page shows
        the other PC's own downloads, else the device."""
        entry = self._entry(self._sel)
        if entry is None:
            return ""
        if self._driver(entry) == "link":
            return f"l:{entry.link.pc_id}"
        return f"d:{entry.device.device_id}"

    def _movable(self) -> bool:
        """Only a download on a phone or on a PC that linked to this one can be
        moved here; the downloads of a PC this one linked to are already on a PC."""
        entry = self._entry(self._sel)
        return entry is not None and self._driver(entry) == "device"

    def _update_row(self, row: int, key: str, job: PhoneJob, online: bool) -> None:
        p = theme.current()
        name_item = self._cell(row, _C_NAME)
        name_item.setText(job.name)
        name_item.setToolTip(plain_tooltip(job.name))
        downloading = job.status == "downloading"
        holder_done = self.table.cellWidget(row, _C_PROGRESS)
        done = holder_done.findChild(QLabel, "DoneText") if holder_done is not None else None
        if done is not None:
            done.setText(self._done_text(job))
        self._cell(row, _C_SPEED).setText(motion.fmt_speed(job.speed) if downloading else "")
        self._cell(row, _C_ETA).setText(motion.fmt_eta(job.eta) if downloading else "")
        holder = self.table.cellWidget(row, _C_STATUS)
        pill = holder.findChild(components.StatusPill) if holder is not None else None
        if pill is not None:
            pill.set_status(job.status)
            # Always reset: an error that went away must not linger.
            tip = f"{pill.full_text()}\n{job.error}" if job.error else pill.full_text()
            pill.setToolTip(plain_tooltip(tip))
        bar = self._bars[row]
        fraction = job.fraction
        if job.status == "completed":
            fraction = 1.0
        bar.set_value(fraction or 0.0)
        bar.set_color(None if downloading else design.status_color(p, job.status))
        busy = (key, job.id) in self._busy
        actions = job.actions(movable=self._movable())
        sig = (job.id, actions, online, busy, p.text2)
        if self._row_sigs.get(job.id) != sig:
            self._row_sigs[job.id] = sig
            previous = self.table.cellWidget(row, _C_ACTIONS)
            if previous is not None:  # hide it now: its deletion is deferred
                self.table.removeCellWidget(row, _C_ACTIONS)
                previous.hide()
                previous.deleteLater()
            self.table.setCellWidget(
                row, _C_ACTIONS, self._actions_cell(job, actions, online and not busy)
            )

    @staticmethod
    def _done_text(job: PhoneJob) -> str:
        """How much is done: "34 MB of 793 MB", the total for a finished
        download, or what has arrived when the size is not known."""
        if job.status == "completed":
            return human_bytes(job.total or job.downloaded) if (job.total or job.downloaded) else ""
        if job.total:
            return t(
                "{done} of {total}", done=human_bytes(job.downloaded), total=human_bytes(job.total)
            )
        return human_bytes(job.downloaded) if job.downloaded else ""

    def _action_buttons(
        self, job: PhoneJob, actions: tuple[str, ...], enabled: bool
    ) -> list[QPushButton]:
        """One button per action that fits, each with the download's name in
        its accessible name, a plain-text tooltip and keyboard focus."""
        buttons: list[QPushButton] = []
        for action in actions:
            label, icon, danger = _ACTIONS[action]
            button = components.IconButton(icon, danger=danger)
            button.setToolTip(plain_tooltip(t(label)))
            button.setAccessibleName(f"{t(label)} {job.name}")
            button.setProperty("action", action)
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            button.setFixedSize(30, 28)
            button.setStyleSheet("QPushButton { padding: 0px; }")
            button.setEnabled(enabled)
            button.clicked.connect(lambda _checked=False, a=action, j=job.id: self._command(j, a))
            buttons.append(button)
        return buttons

    def _selected_online(self) -> bool:
        entry = self._entry(self._sel)
        if entry is None:
            return False
        if self._driver(entry) == "link":
            return bool(entry.link.online)
        return bool(self._service.is_online(entry.device.device_id))

    def _refresh_action_bar(self) -> None:
        """The actions for the selected download as a real row of buttons under
        the table. Windows UI Automation does not expose widgets placed inside
        table cells, so this is the path screen readers and keyboards use; the
        buttons in the row are the mouse shortcut for the same commands."""
        key = self._busy_key() if self._sel is not None else None
        snapshot = self._rendered
        row = self.table.currentRow()
        if (
            key is None
            or snapshot is None
            or self.jobs_stack.currentIndex() != 1
            or not 0 <= row < len(self._row_ids)
            or self._row_ids[row] not in {j.id for j in snapshot.jobs}
        ):
            self._clear_action_bar()
            self.action_bar.hide()
            return
        # The job comes from the rows on screen (by id), against the snapshot
        # they were drawn from: never from whatever list arrived since.
        job = snapshot.job(self._row_ids[row])
        assert job is not None
        actions = job.actions(movable=self._movable())
        enabled = self._selected_online() and (key, job.id) not in self._busy
        sig = (key, job.id, job.name, actions, enabled, theme.current().text2)
        if sig == self._bar_sig and not self.action_bar.isHidden():
            return  # unchanged: keep the buttons, and the focus on them
        self._clear_action_bar()
        self._bar_sig = sig
        caption = _elided(job.name, "dim")
        caption.setAccessibleName(job.name)
        self.action_layout.addWidget(caption, 1)
        for button in self._action_buttons(job, actions, enabled):
            self.action_layout.addWidget(button)
        self.action_bar.show()

    def _clear_action_bar(self) -> None:
        self._bar_sig = None
        while self.action_layout.count():
            item = self.action_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()

    def _actions_cell(self, job: PhoneJob, actions: tuple[str, ...], enabled: bool) -> QWidget:
        holder = QWidget()
        holder.setObjectName("BareContainer")
        lay = QHBoxLayout(holder)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(2)
        lay.addStretch(1)
        for button in self._action_buttons(job, actions, enabled):
            lay.addWidget(button)
        return holder

    def _row_menu(self, row: int) -> QMenu | None:
        """A menu with the actions that fit one download, or None."""
        key = self._busy_key() if self._sel is not None else None
        snapshot = self._rendered
        if key is None or snapshot is None or not 0 <= row < len(snapshot.jobs):
            return None
        job = snapshot.jobs[row]
        online = self._selected_online()
        menu = QMenu(self)
        for action in job.actions(movable=self._movable()):
            entry = menu.addAction(action_label(action))
            entry.setEnabled(online and (key, job.id) not in self._busy)
            entry.triggered.connect(lambda _checked=False, a=action, j=job.id: self._command(j, a))
        if not menu.actions():
            return None
        return menu

    def _show_row_menu(self, pos: QPoint) -> None:
        """The same actions from the keyboard (Menu key) or a right click."""
        row = self.table.rowAt(pos.y())
        menu = self._row_menu(row if row >= 0 else self.table.currentRow())
        if menu is not None:
            menu.exec(self.table.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------- commands

    def _command(self, job_id: int, action: str) -> None:
        entry = self._entry(self._sel)
        if entry is None or (self._busy_key(), job_id) in self._busy:
            return
        if self._driver(entry) == "link":
            assert entry.link is not None and self._links is not None
            token = int(self._links.send_control(entry.link.pc_id, job_id, action))
            if token <= 0:
                self._show_result(action, "failed", "", pc=True)
                return
            self._link_commands[token] = (entry.link.pc_id, job_id, action)
        else:
            assert entry.device is not None
            device_id = entry.device.device_id
            command_id = int(self._service.send_phone_command(device_id, job_id, action))
            if command_id <= 0:
                self._show_result(action, "failed", "", pc=entry.group == "pc")
                return
            self._commands[command_id] = (device_id, job_id, action)
        self._busy.add((self._busy_key(), job_id))
        self._render_right()

    def _on_command_finished(self, command_id: int, result: str, message: str) -> None:
        entry = self._commands.pop(command_id, None)
        if entry is None:
            return
        device_id, job_id, action = entry
        self._busy.discard((f"d:{device_id}", job_id))
        current = self._entry(self._sel)
        pc = current is not None and current.group == "pc"
        self._show_result(action, result, message, pc=pc)
        if current is not None and getattr(current.device, "device_id", None) == device_id:
            self._render_right()

    def _on_request_finished(self, token: int, result: str, message: str) -> None:
        """A control or an added link sent over a link has its outcome."""
        control = self._link_commands.pop(token, None)
        if control is not None:
            pc_id, job_id, action = control
            self._busy.discard((f"l:{pc_id}", job_id))
            self._show_result(action, result, message, pc=True)
            current = self._entry(self._sel)
            if current is not None and getattr(current.link, "pc_id", None) == pc_id:
                self._render_right()
            return
        pc_id_sent = self._link_sends.pop(token, None)
        if pc_id_sent is not None:
            self._show_add_outcome(pc_id_sent, result, message)

    def _show_result(self, action: str, result: str, message: str, *, pc: bool = False) -> None:
        """Inline, under the table: what happened, in words. A failure is
        coloured so it cannot be missed."""
        if result == "ok" and not message and action in _OK_TEXT and not pc:
            text = t(_OK_TEXT[action])
        else:
            text = t(
                "{action}: {result}",
                action=action_label(action),
                result=result_text(result, message, pc=pc),
            )
        self.result_label.setText(text)
        self.result_label.setProperty("role", "ok" if result == "ok" else "warn")
        _repolish(self.result_label)
        self._result_timer.start(RESULT_SHOWN_MS)

    # -------------------------------------------------------------- sending

    def _update_send_enabled(self) -> None:
        if self.link_edit.text():
            self._note_timer.stop()
            self._clear_note()  # typing again: the last answer is old news
        entry = self._entry(self._sel)
        usable = True
        if entry is not None and self._driver(entry) == "link":
            usable = bool(entry.link.online)
        self.send_button.setEnabled(bool(self.link_edit.text().strip()) and usable)

    def _send_link(self) -> None:
        entry = self._entry(self._sel)
        text = self.link_edit.text().strip()
        if not text or entry is None:
            return
        if self._driver(entry) == "link":
            self._add_link_on_pc(entry, text)
            return
        if entry.device is None or entry.group == "pc":
            return
        queued = self._service.queue_for_phone(entry.device.device_id, text)
        if queued:
            self.link_edit.clear()
            note = t("Sent to your phone")
        else:
            note = t("This link cannot be sent to a phone")
        self._show_note(note)

    def _add_link_on_pc(self, entry: _Entry, text: str) -> None:
        assert entry.link is not None and self._links is not None
        urls = text.split()
        pc_id = entry.link.pc_id
        if len(urls) == 1:
            token = int(self._links.send_download(pc_id, urls[0]))
        else:
            token = int(self._links.send_links(pc_id, urls))
        if token <= 0:
            self._show_note(t("This link cannot be sent to a PC"))
            return
        self._link_sends[token] = pc_id

    def _show_add_outcome(self, pc_id: str, result: str, message: str) -> None:
        entry = self._entry(self._sel)
        if entry is None or getattr(entry.link, "pc_id", None) != pc_id:
            return
        if result == "ok":
            self.link_edit.clear()
            self._show_note(
                t("That PC already has this link")
                if message == "duplicate"
                else t("Added on that PC")
            )
        else:
            self._show_note(result_text(result, "", pc=True))

    def _show_note(self, text: str) -> None:
        self.send_note.setText(text)
        self.send_note.show()
        self._note_timer.start(NOTE_SHOWN_MS)

    def _clear_note(self) -> None:
        self.send_note.setText("")
        self.send_note.hide()

    # ------------------------------------------------------------- removing

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

    def _remove_selected(self) -> None:
        entry = self._entry(self._sel)
        if entry is None or not self._confirm_remove(entry.name):
            return
        # A PC linked both ways is one row: removing it ends both directions.
        if entry.device is not None:
            self._service.remove_device(entry.device.device_id)
        if entry.link is not None and self._links is not None:
            self._links.remove_link(entry.link.pc_id)
        self.refresh()

    # -------------------------------------------------------------- theming

    def retint(self) -> None:
        """The theme changed: redraw the rows and icons in the new colors."""
        self._row_sigs = {}
        self._render_right()

    # -------------------------------------------------------------- pairing

    def _pair(self) -> None:
        dialog = self.pairing_dialog
        if dialog is None:
            dialog = PairingDialog(self._service, self.window())
            dialog.finished.connect(self._on_dialog_closed)
            self.pairing_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        self._service.start_pairing()

    def _open_link_dialog(self) -> None:
        if self._links is None:
            return
        dialog = self.link_dialog
        if dialog is None:
            dialog = LinkDialog(self._links, self.window())
            dialog.finished.connect(lambda _result: setattr(self, "link_dialog", None))
            self.link_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

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

    def _on_pairing_pending(self, device_name: str, kind: str = "phone") -> None:
        self._dismiss_allow_box()
        box = QMessageBox(self.window())
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(t("Pair a phone or PC"))
        box.setTextFormat(Qt.TextFormat.PlainText)
        if kind == "pc":
            box.setText(t('Allow "{name}" (a PC) to control GrabLine?', name=device_name))
        else:
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
        if self.link_dialog is not None:
            self.link_dialog.close()
        for bar in self._bars:
            bar.release()
        self._bars = []
        super().closeEvent(event)


__all__ = ["DevicesPage", "action_label", "result_text"]
