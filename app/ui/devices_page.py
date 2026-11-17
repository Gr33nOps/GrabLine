"""The Devices page: paired Android phones, links sent to them, and the
downloads on each phone (PROTOCOL.md section 20).

Left: the paired phones and the "Pair a phone" button. Right, for the phone
picked on the left: its name and state with "Remove", a field to send it a
link, and "Downloads on this phone", a live table with the actions that fit
each download. Commands go out only from a click here; the outcome of each one
is shown under the table and on the status bar.

The page talks to ``app.remote.service.RemoteService`` only through its public
API and Qt signals. Every string that came from a phone is shown as plain text.
The pairing code lives in the pairing dialog and nowhere else.
"""

from __future__ import annotations

import time
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
from app.remote.phonejobs import PhoneJob, PhoneSnapshot
from app.ui import components, design, motion, theme
from app.ui.format import human_bytes, plain_tooltip
from app.ui.phone_page import PairingDialog

#: How long a phone may be connected without sending a list before the PC
#: concludes its app is too old to share downloads.
LIST_GRACE_S = 4.0
#: How long a command result stays under the table.
RESULT_SHOWN_MS = 8000
#: Phone list updates redraw the page at most this often (seconds).
RENDER_MIN_INTERVAL_S = 0.25
#: Below this table width the size and time-left columns are hidden.
_NARROW_PX = 700

_COLUMNS = (N_("Name"), N_("Status"), N_("Progress"), N_("Size"), N_("Speed"), N_("Time left"), "")
_C_NAME, _C_STATUS, _C_PROGRESS, _C_SIZE, _C_SPEED, _C_ETA, _C_ACTIONS = range(7)

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
_RESULT_TEXT = {
    "ok": N_("Done"),
    "notFound": N_("The phone no longer has that download"),
    "refused": N_("The phone did not allow that"),
    "failed": N_("The phone could not do that"),
    "timeout": N_("The phone did not answer"),
    "offline": N_("The phone is not connected"),
    "busy": N_("Too many commands at once. Try again in a moment."),
}


def action_label(action: str) -> str:
    return t(_ACTIONS[action][0]) if action in _ACTIONS else action


def result_text(result: str, message: str) -> str:
    """The words for a command outcome. The phone's own note wins for the
    answers the phone gave; the PC's conclusions use fixed sentences."""
    if message and result in ("ok", "notFound", "refused", "failed"):
        return message
    return t(_RESULT_TEXT.get(result, "The phone could not do that"))


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


class DevicesPage(QWidget):
    """Sidebar page "Devices"."""

    #: A line for the main window's status bar.
    status = Signal(str)

    def __init__(self, service: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service = service
        self._selected: str | None = None
        self._row_ids: list[int] = []
        self._row_sigs: dict[int, tuple[Any, ...]] = {}
        self._bars: list[motion.SmoothProgressBar] = []
        self._online_since: dict[str, float] = {}
        #: command id -> (device id, job id, action)
        self._commands: dict[int, tuple[str, int, str]] = {}
        #: (device id, job id) with a command waiting for its answer
        self._busy: set[tuple[str, int]] = set()
        self._last_render = 0.0
        self.pairing_dialog: PairingDialog | None = None
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

        service.devicesChanged.connect(self.refresh)
        service.deviceOnlineChanged.connect(self._on_presence)
        service.phoneJobsChanged.connect(self._on_jobs_changed)
        service.commandFinished.connect(self._on_command_finished)
        service.pairingStarted.connect(self._on_pairing_started)
        service.pairingPending.connect(self._on_pairing_pending)
        service.pairingLockedWarning.connect(self._on_locked_warning)
        service.pairingFinished.connect(self._on_pairing_finished)

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
        col.setSpacing(10)
        col.addWidget(components.SectionLabel(t("Paired phones")))

        self.device_list = QListWidget()
        self.device_list.setObjectName("DevicesList")
        self.device_list.setAccessibleName(t("Paired phones"))
        self.device_list.setFrameShape(QFrame.Shape.NoFrame)
        self.device_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.device_list.currentItemChanged.connect(self._on_current_changed)
        col.addWidget(self.device_list, 1)

        self.no_devices_label = _wrapping(
            components.role_label(t("No phones are paired."), "muted")
        )
        self.no_devices_label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
        )
        self.no_devices_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        col.addWidget(self.no_devices_label, 1)

        self.pair_button = components.accent_button(t("Pair a phone"))
        self.pair_button.setAccessibleName(t("Pair a phone"))
        self.pair_button.clicked.connect(self._pair)
        col.addWidget(self.pair_button)
        return left

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
            t("No phones paired yet"), "strong", size=design.FONT["h1"], bold=True
        )
        inner.addWidget(self.empty_title)
        self.empty_body = _wrapping(
            components.role_label(
                t(
                    "Pair your Android phone to send it links from this PC, see and control "
                    "its downloads here, and start downloads on this PC from your phone."
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

        col.addWidget(components.SectionLabel(t("Send a link to this phone")))
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
        col.addLayout(send_row)
        self.send_note = _wrapping(components.role_label("", "muted", size=design.FONT["small"]))
        self.send_note.hide()
        col.addWidget(self.send_note)

        col.addSpacing(4)
        col.addWidget(components.SectionLabel(t("Downloads on this phone")))
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
        header = table.horizontalHeader()
        header.setHighlightSections(False)
        header.setStretchLastSection(False)
        header.setSectionResizeMode(_C_NAME, QHeaderView.ResizeMode.Stretch)
        for column, width in (
            (_C_STATUS, 118),
            (_C_PROGRESS, 72),
            (_C_SIZE, 80),
            (_C_SPEED, 96),
            (_C_ETA, 76),
            (_C_ACTIONS, 108),
        ):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            table.setColumnWidth(column, width)
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        for column in (_C_SIZE, _C_SPEED, _C_ETA):
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
        """Narrow windows drop the figures that can wait (size, time left) so
        the name always has room; the same rule as the downloads table."""
        narrow = self.table.viewport().width() < _NARROW_PX
        self.table.setColumnHidden(_C_SIZE, narrow)
        self.table.setColumnHidden(_C_ETA, narrow)

    # ------------------------------------------------------------- the list

    def refresh(self) -> None:
        """Rebuild the list of paired phones, keeping the selection."""
        devices = list(self._service.store.list())
        ids = [d.device_id for d in devices]
        keep = self._selected if self._selected in ids else (ids[0] if ids else None)
        self.device_list.blockSignals(True)
        self.device_list.clear()
        for device in devices:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, device.device_id)
            online = self._service.is_online(device.device_id)
            detail = self._seen_text(device, online)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, f"{device.name}, {detail}")
            widget = self._device_row(str(device.name), detail, online)
            item.setSizeHint(widget.sizeHint())
            self.device_list.addItem(item)
            self.device_list.setItemWidget(item, widget)
            if device.device_id == keep:
                self.device_list.setCurrentItem(item)
        self.device_list.blockSignals(False)
        self.device_list.setVisible(bool(devices))
        self.no_devices_label.setVisible(not devices)
        self._selected = keep
        self._render_right()

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

    def _on_current_changed(self, current: QListWidgetItem | None, _previous: Any) -> None:
        device_id = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        if device_id != self._selected:
            self._selected = device_id
            self.link_edit.clear()
            self.send_note.setText("")
            self.send_note.hide()
            self.result_label.setText("")
            self._render_right()

    def selected_device(self) -> Any | None:
        for device in self._service.store.list():
            if device.device_id == self._selected:
                return device
        return None

    def _on_presence(self, device_id: str) -> None:
        if self._service.is_online(device_id):
            self._online_since[device_id] = time.monotonic()
        else:
            self._online_since.pop(device_id, None)
        self.refresh()

    # ------------------------------------------------------- the right side

    def _render_right(self) -> None:
        self._last_render = time.monotonic()
        device = self.selected_device()
        if device is None:
            self.right_stack.setCurrentIndex(0)
            return
        self.right_stack.setCurrentIndex(1)
        online = bool(self._service.is_online(device.device_id))
        self.name_label.setText(str(device.name))
        paired = t("Paired {date}", date=_format_time(device.created_at))
        seen = self._seen_text(device, online)
        self.state_label.setText(f"{seen}  ·  {paired}")
        self.state_label.setProperty("role", "ok" if online else "muted")
        _repolish(self.state_label)
        self.remove_button.setAccessibleName(f"{t('Remove')} {device.name}")
        self._update_send_enabled()
        self._render_jobs(device, online)

    def _jobs_state(self, device_id: str, online: bool) -> tuple[str, PhoneSnapshot | None]:
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

    def _render_jobs(self, device: Any, online: bool) -> None:
        state, snapshot = self._jobs_state(device.device_id, online)
        text = {
            "offline_never": t("Its downloads show up here once the phone connects."),
            "checking": t("Checking the phone's downloads…"),
            "older": t("Update GrabLine on the phone to see its downloads here."),
            "not_shared": t(
                "This phone doesn't share its downloads. "
                "Turn it on in GrabLine on the phone: Settings, PCs."
            ),
            "empty": t("No downloads on this phone."),
        }.get(state)
        stale = not online and snapshot is not None and state == "jobs"
        self.banner_label.setVisible(stale)
        if stale:
            self.banner_label.setText(t("Offline. Showing the last list this phone sent."))
        if state == "checking":
            remaining = LIST_GRACE_S - (
                time.monotonic() - self._online_since.get(device.device_id, 0.0)
            )
            self._grace_timer.start(max(50, int(remaining * 1000) + 50))
        if text is not None:
            self.message_label.setText(text)
            self.message_label.setAccessibleName(text)
            self.jobs_stack.setCurrentIndex(0)
            self._clear_rows()
            return
        assert snapshot is not None
        self.jobs_stack.setCurrentIndex(1)
        self._fill_rows(device.device_id, snapshot, online)
        self._dim.setEnabled(not online)

    def _on_jobs_changed(self, device_id: str) -> None:
        """A phone reports at most once a second by its own rule, but the PC
        does not rely on that: re-drawing is limited to one per interval and
        the newest list wins."""
        if device_id != self._selected:
            return
        wait = RENDER_MIN_INTERVAL_S - (time.monotonic() - self._last_render)
        if wait <= 0 and not self._render_timer.isActive():
            self._render_right()
        elif not self._render_timer.isActive():
            self._render_timer.start(max(1, int(wait * 1000) + 1))

    def _render_throttled(self) -> None:
        if self._selected is not None:
            self._render_right()

    # ------------------------------------------------------------ the table

    def _clear_rows(self) -> None:
        for bar in self._bars:
            bar.release()
        self._bars = []
        self.table.setRowCount(0)
        self._row_ids = []
        self._row_sigs = {}

    def _fill_rows(self, device_id: str, snapshot: PhoneSnapshot, online: bool) -> None:
        ids = [job.id for job in snapshot.jobs]
        if ids != self._row_ids:
            self._rebuild_rows(snapshot)
        for row, job in enumerate(snapshot.jobs):
            self._update_row(row, device_id, job, online)

    def _rebuild_rows(self, snapshot: PhoneSnapshot) -> None:
        self._clear_rows()
        self.table.setRowCount(len(snapshot.jobs))
        num_font = design.numeric_font(self.table.font())
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        for row, job in enumerate(snapshot.jobs):
            for column in (_C_NAME, _C_SIZE, _C_SPEED, _C_ETA):
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
            self.table.setCellWidget(row, _C_PROGRESS, self._pad(bar))
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

    def _update_row(self, row: int, device_id: str, job: PhoneJob, online: bool) -> None:
        p = theme.current()
        name_item = self._cell(row, _C_NAME)
        name_item.setText(job.name)
        name_item.setToolTip(plain_tooltip(job.name))
        downloading = job.status == "downloading"
        size = job.total if job.total else job.downloaded
        self._cell(row, _C_SIZE).setText(human_bytes(size) if size else "")
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
        busy = (device_id, job.id) in self._busy
        actions = job.actions()
        sig = (job.id, actions, online, busy, p.text2)
        if self._row_sigs.get(job.id) != sig:
            self._row_sigs[job.id] = sig
            self.table.setCellWidget(
                row, _C_ACTIONS, self._actions_cell(device_id, job, actions, online and not busy)
            )

    def _actions_cell(
        self, device_id: str, job: PhoneJob, actions: tuple[str, ...], enabled: bool
    ) -> QWidget:
        holder = QWidget()
        holder.setObjectName("BareContainer")
        lay = QHBoxLayout(holder)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(2)
        lay.addStretch(1)
        for action in actions:
            label, icon, danger = _ACTIONS[action]
            button = components.IconButton(icon, danger=danger, tooltip=t(label))
            button.setAccessibleName(f"{t(label)}: {job.name}")
            button.setFixedSize(30, 28)
            button.setStyleSheet("QPushButton { padding: 0px; }")
            button.setEnabled(enabled)
            button.clicked.connect(
                lambda _checked=False, a=action, j=job.id: self._command(device_id, j, a)
            )
            lay.addWidget(button)
        return holder

    def _row_menu(self, row: int) -> QMenu | None:
        """A menu with the actions that fit one download, or None."""
        device_id = self._selected
        snapshot = self._service.phone_jobs(device_id) if device_id else None
        if device_id is None or snapshot is None or not 0 <= row < len(snapshot.jobs):
            return None
        job = snapshot.jobs[row]
        online = bool(self._service.is_online(device_id))
        menu = QMenu(self)
        for action in job.actions():
            entry = menu.addAction(action_label(action))
            entry.setEnabled(online and (device_id, job.id) not in self._busy)
            entry.triggered.connect(
                lambda _checked=False, a=action, j=job.id: self._command(device_id, j, a)
            )
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

    def _command(self, device_id: str, job_id: int, action: str) -> None:
        if (device_id, job_id) in self._busy:
            return
        command_id = int(self._service.send_phone_command(device_id, job_id, action))
        if command_id <= 0:
            self._show_result(action, "failed", "")
            return
        self._commands[command_id] = (device_id, job_id, action)
        self._busy.add((device_id, job_id))
        self._render_right()

    def _on_command_finished(self, command_id: int, result: str, message: str) -> None:
        entry = self._commands.pop(command_id, None)
        if entry is None:
            return
        device_id, job_id, action = entry
        self._busy.discard((device_id, job_id))
        self._show_result(action, result, message)
        if device_id == self._selected:
            self._render_right()

    def _show_result(self, action: str, result: str, message: str) -> None:
        text = t(
            "{action}: {result}", action=action_label(action), result=result_text(result, message)
        )
        self.result_label.setText(text)
        self._result_timer.start(RESULT_SHOWN_MS)
        self.status.emit(text)

    # -------------------------------------------------------------- sending

    def _update_send_enabled(self) -> None:
        self.send_button.setEnabled(bool(self.link_edit.text().strip()))

    def _send_link(self) -> None:
        url = self.link_edit.text().strip()
        device_id = self._selected
        if not url or device_id is None:
            return
        queued = self._service.queue_for_phone(device_id, url)
        if queued:
            self.link_edit.clear()
            text = t("Sent to your phone")
        else:
            text = t("This link cannot be sent to a phone")
        self.send_note.setText(text)
        self.send_note.show()
        self.status.emit(text)

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
        device = self.selected_device()
        if device is None or not self._confirm_remove(str(device.name)):
            return
        self._service.remove_device(device.device_id)
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
        for bar in self._bars:
            bar.release()
        self._bars = []
        super().closeEvent(event)


__all__ = ["DevicesPage", "action_label", "result_text"]
