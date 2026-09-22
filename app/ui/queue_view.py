"""The embedded Queue Manager page.

A header with 'New queue', then one card per queue - numbered badge, name, the
settings it runs under, and, live, what it is actually doing: how many
downloads are running, how many are waiting, a progress bar across the queue's
unfinished work, and the names of the jobs in flight. The default queue (the
downloads that belong to no named queue) gets a card too, so every download in
the app is accounted for somewhere on this page.

Each card carries the controls you reach for most - pause/resume the whole
queue, move it up or down the running order - with the full settings in an
inline editor that drops open underneath. Wraps the real queue backend: every
button here is a DownloadManager call, and the scheduler is what enforces it.
"""

from __future__ import annotations

from dataclasses import replace
from typing import NamedTuple

from PySide6.QtCore import Qt, QTime, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import N_, t
from app.core.manager import DownloadManager, QueueStats
from app.core.models import Queue
from app.ui import components, design
from app.ui.format import human_bytes

#: How often the live figures refresh. Only ticks while the page is on screen,
#: and reads the manager's cached snapshot, so it costs nothing when idle.
_LIVE_MS = 1000

_CATEGORIES = (
    "",
    N_("Video"),
    N_("Music"),
    N_("Images"),
    N_("Documents"),
    N_("Archives"),
    N_("Programs"),
    N_("Games"),
    N_("Torrents"),
)


class _LiveWidgets(NamedTuple):
    """The widgets on one card that the one-second tick rewrites in place."""

    summary: components.ElidingLabel  # "2 downloading · 3 waiting"
    active: components.ElidingLabel  # the names of the jobs in flight
    bar: QProgressBar
    readout: components.ElidingLabel  # "1.2 GB of 10.8 GB · 11%"


def _live_text(stats: QueueStats) -> str:
    """One plain sentence about what this queue is doing right now.

    Deliberately not a row of numbers: "2 downloading · 3 waiting" answers the
    question people open this page to ask, and naming what is running answers
    the follow-up without making them go back to the list.
    """
    if stats.total == 0:
        return t("Empty")
    parts = []
    if stats.downloading:
        parts.append(t("{count} downloading", count=stats.downloading))
    if stats.queued:
        parts.append(t("{count} waiting", count=stats.queued))
    if stats.paused:
        parts.append(t("{count} paused", count=stats.paused))
    if stats.completed:
        parts.append(t("{count} done", count=stats.completed))
    if stats.failed:
        parts.append(t("{count} failed", count=stats.failed))
    return "  ·  ".join(parts) if parts else t("Nothing to do")


def _active_text(stats: QueueStats) -> str:
    """The names of the jobs in flight, as a line of its own.

    They used to be appended to the counts with an em dash. A single film
    release name is longer than the whole rest of the sentence, so on a narrow
    window that one line decided the card's width and the page grew a
    horizontal scrollbar. Its own eliding line keeps the counts readable at
    any size.
    """
    if not stats.active:
        return ""
    shown = ", ".join(stats.active[:2])
    if len(stats.active) > 2:
        shown += t(" and {count} more", count=len(stats.active) - 2)
    return shown


def _would_cycle(queues: dict[int, Queue], queue_id: int, depends_on: int | None) -> bool:
    """Following the depends_on chain from ``depends_on``, do we reach
    ``queue_id`` again? (A cycle would deadlock both queues.)"""
    seen: set[int] = set()
    current = depends_on
    while current is not None and current not in seen:
        if current == queue_id:
            return True
        seen.add(current)
        parent = queues.get(current)
        current = parent.depends_on if parent else None
    return False


class QueueView(QWidget):
    def __init__(self, manager: DownloadManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self._editing: int | None = None
        #: queue id (None = default queue) -> the widgets the live tick writes
        #: into. Updating these in place rather than rebuilding the page keeps
        #: the open editor, the scroll position and the focus where they were.
        self._live: dict[int | None, _LiveWidgets] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(_LIVE_MS)
        self._timer.timeout.connect(self._tick)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QFrame()
        header.setObjectName("Toolbar")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 10, 12, 10)
        title = components.role_label(
            t("Queue manager"), "strong", size=design.FONT["h1"], bold=True
        )
        hl.addWidget(title)
        hl.addStretch(1)
        new_btn = components.IconButton("add", t("New queue"))
        new_btn.clicked.connect(self._new_queue)
        hl.addWidget(new_btn)
        root.addWidget(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        # Cards elide rather than overflow, so a sideways scrollbar would only
        # ever be the symptom of a layout bug - refuse it outright.
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._body_holder = QWidget()
        self._body = QVBoxLayout(self._body_holder)
        self._body.setContentsMargins(16, 16, 16, 16)
        self._body.setSpacing(8)
        scroll.setWidget(self._body_holder)
        root.addWidget(scroll, 1)

    def showEvent(self, event: object) -> None:
        super().showEvent(event)  # type: ignore[arg-type]
        self.reload()
        self._timer.start()

    def hideEvent(self, event: object) -> None:
        super().hideEvent(event)  # type: ignore[arg-type]
        self._timer.stop()  # nothing to update behind another page

    # ------------------------------------------------------------ live view

    def _tick(self) -> None:
        """Refresh the live line and bar on every card, in place."""
        if not self._live:
            return
        stats = self.manager.queue_stats()
        for queue_id, widgets in self._live.items():
            self._apply_stats(widgets, stats.get(queue_id) or QueueStats(queue_id=queue_id))

    @staticmethod
    def _apply_stats(widgets: _LiveWidgets, stats: QueueStats) -> None:
        widgets.summary.setText(_live_text(stats))
        active = _active_text(stats)
        widgets.active.setText(active)
        widgets.active.setVisible(bool(active))
        showing = stats.downloading > 0 and stats.total_bytes > 0
        widgets.bar.setVisible(showing)
        widgets.readout.setVisible(showing)
        if showing:
            widgets.bar.setValue(stats.percent)
            # The bar is a 6px hairline: its own text renders clipped and
            # overlapping the line beneath it, so the figures live in a label.
            widgets.readout.setText(
                t(
                    "{done} of {total}  ·  {percent}%",
                    done=human_bytes(stats.downloaded_bytes),
                    total=human_bytes(stats.total_bytes),
                    percent=stats.percent,
                )
            )

    def reload(self) -> None:
        while self._body.count():
            item = self._body.takeAt(0)
            if item is None:
                break
            w = item.widget()
            if w is not None:
                # Unparent before deleting. deleteLater() only schedules the
                # destruction for the next pass of the event loop, and until
                # then the old card is still a visible child painting over the
                # new layout - two reloads in one slot left ghost text lying
                # across the cards. setParent(None) takes it off screen now.
                w.setParent(None)
                w.deleteLater()
        self._live.clear()
        queues = {q.id: q for q in self.manager.list_queues()}
        stats = self.manager.queue_stats()

        ordered = list(queues.values())
        for index, queue in enumerate(ordered, start=1):
            self._body.addWidget(
                self._card(
                    index,
                    queue,
                    queues,
                    stats.get(queue.id) or QueueStats(queue_id=queue.id),
                    first=index == 1,
                    last=index == len(ordered),
                )
            )
            if self._editing == queue.id:
                self._body.addWidget(self._editor(queue, ordered))

        # The default queue always gets a card, last: without it the downloads
        # that belong to no named queue are invisible here, which is exactly
        # the state people describe as "the queue manager shows nothing".
        self._body.addWidget(
            self._default_card(stats.get(None) or QueueStats(queue_id=None), len(ordered) + 1)
        )
        if not queues:
            hint = components.role_label(
                t("Press New queue to group downloads and limit how many run at once."),
                "muted",
            )
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._body.addSpacing(12)
            self._body.addWidget(hint)
        self._body.addStretch(1)

    def _default_card(self, stats: QueueStats, index: int) -> QWidget:
        """A read-only card for downloads with no named queue. It has no
        settings of its own - it runs under the global 'Downloads at once' -
        so it offers no edit or delete, only the same live view."""
        card, body = self._card_shell(str(index), t("Default"))
        traits = components.ElidingLabel(
            t(
                "Every download not put in a queue  ·  global limit: {count} at once",
                count=self.manager.max_concurrent,
            ),
            "muted",
            size=design.FONT["small"],
        )
        body.addWidget(traits)
        self._attach_live(None, body, stats)
        return card

    def _card_shell(self, badge_text: str, title: str) -> tuple[QFrame, QVBoxLayout]:
        """A card with its numbered badge and title, and the column the caller
        fills with the trait line, the live line and the progress bar."""
        card = QFrame()
        card.setProperty("card", "true")
        outer = QVBoxLayout(card)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(12)
        badge = QLabel(badge_text)
        badge.setObjectName("QueueBadge")
        badge.setFixedSize(30, 30)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)

        text = QWidget()
        # Layout only - without this it paints the page background over the card.
        text.setObjectName("BareContainer")
        # Free to shrink: the column's own minimum used to be whatever its
        # longest label wanted, which is what pushed the card off the page.
        text.setMinimumWidth(0)
        text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        body = QVBoxLayout(text)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(2)
        body.addWidget(components.ElidingLabel(title, "strong", size=design.FONT["h2"], bold=True))
        top.addWidget(text, 1)
        card._top_row = top  # type: ignore[attr-defined]  # buttons go here
        outer.addLayout(top)
        return card, body

    def _attach_live(self, queue_id: int | None, body: QVBoxLayout, stats: QueueStats) -> None:
        """Add the live status lines and progress bar, and register them so the
        one-second tick can rewrite them without rebuilding the page.

        Every text line elides, so a card is as wide as the page gives it and
        never a character more.
        """
        summary = components.ElidingLabel("", "dim", size=design.FONT["small"])
        active = components.ElidingLabel("", "muted", size=design.FONT["small"])
        active.setVisible(False)
        bar = QProgressBar()
        bar.setTextVisible(False)
        bar.setFixedHeight(6)
        bar.setVisible(False)
        readout = components.ElidingLabel("", "dim", size=design.FONT["caption"])
        readout.setVisible(False)
        body.addWidget(summary)
        body.addWidget(active)
        body.addWidget(bar)
        body.addWidget(readout)
        widgets = _LiveWidgets(summary, active, bar, readout)
        self._live[queue_id] = widgets
        self._apply_stats(widgets, stats)

    def _card(
        self,
        index: int,
        queue: Queue,
        queues: dict[int, Queue],
        stats: QueueStats,
        *,
        first: bool = False,
        last: bool = False,
    ) -> QWidget:
        card, body = self._card_shell(str(index), queue.name)
        if self._editing == queue.id:
            card.setProperty("selected", "true")
        body.addWidget(
            components.ElidingLabel(self._traits(queue, queues), "muted", size=design.FONT["small"])
        )
        self._attach_live(queue.id, body, stats)

        top = card._top_row  # type: ignore[attr-defined]
        # Pause/resume the whole queue in one click. It was previously only
        # reachable by opening the editor and saving, which is three clicks and
        # a form for the control people use most.
        toggle = components.IconButton(
            "resume" if queue.paused else "pause",
            "",
            tooltip=t("Resume queue") if queue.paused else t("Pause queue"),
        )
        toggle.clicked.connect(lambda: self._toggle_paused(queue))
        up = components.IconButton("export", "", tooltip=t("Move up the running order"))
        up.setEnabled(not first)
        up.clicked.connect(lambda: self._move(queue.id, -1))
        down = components.IconButton("import", "", tooltip=t("Move down the running order"))
        down.setEnabled(not last)
        down.clicked.connect(lambda: self._move(queue.id, 1))
        edit = components.IconButton("settings", "", tooltip=t("Queue settings"))
        edit.clicked.connect(lambda: self._toggle_edit(queue.id))
        delete = components.IconButton("trash", "", danger=True, tooltip=t("Delete queue"))
        delete.clicked.connect(lambda: self._delete(queue))
        for button in (toggle, up, down, edit, delete):
            top.addWidget(button)
        return card

    @staticmethod
    def _traits(queue: Queue, queues: dict[int, Queue]) -> str:
        parts = []
        if queue.max_concurrent == 1:
            parts.append(t("Sequential"))
        elif queue.max_concurrent > 1:
            parts.append(t("{count} parallel", count=queue.max_concurrent))
        else:
            parts.append(t("Global limit"))
        if queue.schedule_enabled:
            parts.append(f"{queue.start_time}-{queue.stop_time}")
        if queue.paused:
            parts.append(t("Paused"))
        if queue.category:
            parts.append(t(queue.category))
        if queue.depends_on and queue.depends_on in queues:
            parts.append(t("after '{name}'", name=queues[queue.depends_on].name))
        return "  ·  ".join(parts)

    def _editor(self, queue: Queue, all_queues: list[Queue]) -> QWidget:
        box = QFrame()
        box.setProperty("panel", "true")
        form = QVBoxLayout(box)
        form.setContentsMargins(16, 14, 16, 14)
        form.setSpacing(10)

        name_edit = QLineEdit(queue.name)
        concurrent = QSpinBox()
        concurrent.setRange(0, 10)
        concurrent.setValue(queue.max_concurrent)
        concurrent.setSpecialValueText(t("Global"))
        sched_check = QCheckBox(t("Only between"))
        sched_check.setChecked(queue.schedule_enabled)
        start = QTimeEdit(QTime.fromString(queue.start_time, "HH:mm"))
        start.setDisplayFormat("HH:mm")
        stop = QTimeEdit(QTime.fromString(queue.stop_time, "HH:mm"))
        stop.setDisplayFormat("HH:mm")
        paused = QCheckBox(t("Paused"))
        paused.setChecked(queue.paused)
        category = QComboBox()
        for cat in _CATEGORIES:
            category.addItem(t(cat) if cat else t("(none)"), cat)
        category.setCurrentIndex(max(0, category.findData(queue.category)))
        depends = QComboBox()
        depends.addItem(t("(nothing)"), None)
        for other in all_queues:
            if other.id != queue.id:
                depends.addItem(other.name, other.id)
        if queue.depends_on is not None:
            depends.setCurrentIndex(max(0, depends.findData(queue.depends_on)))

        form.addLayout(self._field(t("Name"), name_edit))
        form.addLayout(self._field(t("Downloads at once"), concurrent))
        sched_row = QHBoxLayout()
        sched_row.addWidget(sched_check)
        sched_row.addWidget(start)
        sched_row.addWidget(QLabel(t("and")))
        sched_row.addWidget(stop)
        sched_row.addStretch(1)
        form.addLayout(self._field(t("Schedule"), sched_row))
        form.addLayout(self._field(t("Category"), category))
        form.addLayout(self._field(t("Wait for queue"), depends))
        form.addLayout(self._field("", paused))

        from PySide6.QtWidgets import QPushButton

        buttons = QHBoxLayout()
        save = components.accent_button(t("Save"))
        cancel_btn = QPushButton(t("Cancel"))

        def do_save() -> None:
            dep = depends.currentData()
            queues = {q.id: q for q in all_queues}
            if dep is not None and _would_cycle(queues, queue.id, dep):
                QMessageBox.warning(self, "GrabLine", t("That would make the queues wait forever."))
                return
            self.manager.update_queue(
                replace(
                    queue,
                    name=name_edit.text().strip() or queue.name,
                    max_concurrent=concurrent.value(),
                    paused=paused.isChecked(),
                    schedule_enabled=sched_check.isChecked(),
                    start_time=start.time().toString("HH:mm"),
                    stop_time=stop.time().toString("HH:mm"),
                    category=str(category.currentData() or ""),
                    depends_on=dep,
                )
            )
            self._editing = None
            self.reload()

        save.clicked.connect(do_save)
        cancel_btn.clicked.connect(self._cancel_edit)
        buttons.addWidget(save)
        buttons.addWidget(cancel_btn)
        buttons.addStretch(1)
        form.addLayout(buttons)
        return box

    def _cancel_edit(self) -> None:
        self._editing = None
        self.reload()

    def _field(self, label: str, widget: object) -> QHBoxLayout:
        from PySide6.QtWidgets import QLayout
        from PySide6.QtWidgets import QWidget as _QW

        row = QHBoxLayout()
        cap = components.role_label(label, "dim")
        cap.setFixedWidth(150)
        row.addWidget(cap)
        if isinstance(widget, QLayout):
            row.addLayout(widget, 1)
        elif isinstance(widget, _QW):
            row.addWidget(widget, 1)
        return row

    # ------------------------------------------------------------- actions

    def _new_queue(self) -> None:
        name, ok = QInputDialog.getText(self, t("New queue"), t("Queue name:"))
        if ok and name.strip():
            q = self.manager.create_queue(name.strip())
            self._editing = q.id
            self.reload()

    def _toggle_paused(self, queue: Queue) -> None:
        self.manager.set_queue_paused(queue.id, not queue.paused)
        self.reload()

    def _move(self, queue_id: int, delta: int) -> None:
        self.manager.move_queue(queue_id, delta)
        self.reload()

    def _toggle_edit(self, queue_id: int) -> None:
        self._editing = None if self._editing == queue_id else queue_id
        self.reload()

    def _delete(self, queue: Queue) -> None:
        answer = QMessageBox.question(
            self,
            "GrabLine",
            t(
                "Delete queue '{name}'? Its downloads move back to the default queue.",
                name=queue.name,
            ),
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.manager.delete_queue(queue.id)
            if self._editing == queue.id:
                self._editing = None
            self.reload()
