"""The embedded Queue Manager page: a header with 'New queue', a list of queue
cards (numbered badge, name, traits, edit/delete), and an inline editor that
drops open under the selected card. Wraps the real queue backend.
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, QTime
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import t
from app.core.manager import DownloadManager
from app.core.models import Queue
from app.ui import components, design
from app.ui.queue_dialog import _CATEGORIES, _would_cycle


class QueueView(QWidget):
    def __init__(self, manager: DownloadManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self._editing: int | None = None

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
        self._body_holder = QWidget()
        self._body = QVBoxLayout(self._body_holder)
        self._body.setContentsMargins(16, 16, 16, 16)
        self._body.setSpacing(8)
        scroll.setWidget(self._body_holder)
        root.addWidget(scroll, 1)

    def showEvent(self, event: object) -> None:
        super().showEvent(event)  # type: ignore[arg-type]
        self.reload()

    def reload(self) -> None:
        while self._body.count():
            item = self._body.takeAt(0)
            if item is None:
                break
            w = item.widget()
            if w is not None:
                w.deleteLater()
        queues = {q.id: q for q in self.manager.list_queues()}
        if not queues:
            empty = components.role_label(
                t("No queues yet. Press New queue to create one."), "muted"
            )
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._body.addSpacing(48)
            self._body.addWidget(empty)
        for index, queue in enumerate(queues.values(), start=1):
            self._body.addWidget(self._card(index, queue, queues))
            if self._editing == queue.id:
                self._body.addWidget(self._editor(queue, list(queues.values())))
        self._body.addStretch(1)

    def _card(self, index: int, queue: Queue, queues: dict[int, Queue]) -> QWidget:
        selected = self._editing == queue.id
        card = QFrame()
        card.setProperty("card", "true")
        if selected:
            card.setProperty("selected", "true")
        lay = QHBoxLayout(card)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(12)

        badge = QLabel(str(index))
        badge.setObjectName("QueueBadge")
        badge.setFixedSize(30, 30)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(badge)

        text = QWidget()
        # Layout only - without this it paints the page background over the card.
        text.setObjectName("BareContainer")
        tl = QVBoxLayout(text)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(1)
        name = components.role_label(queue.name, "strong", size=design.FONT["h2"], bold=True)
        traits = components.role_label(
            self._traits(queue, queues), "muted", size=design.FONT["small"]
        )
        tl.addWidget(name)
        tl.addWidget(traits)
        lay.addWidget(text, 1)

        edit = components.IconButton("settings", "")
        edit.clicked.connect(lambda: self._toggle_edit(queue.id))
        delete = components.IconButton("trash", "", danger=True)
        delete.clicked.connect(lambda: self._delete(queue))
        lay.addWidget(edit)
        lay.addWidget(delete)
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
