"""The Queue Manager: create/edit/reorder named download queues (groups) with
their own concurrency (sequential/parallel), schedule, category auto-assign,
and queue-to-queue dependencies.
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, QTime
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import N_, t
from app.core.manager import DownloadManager
from app.core.models import Queue
from app.ui import chrome

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


class QueueManagerDialog(chrome.Dialog):
    def __init__(self, manager: DownloadManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.setWindowTitle(t("Queue manager"))
        self.setMinimumSize(460, 360)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                t(
                    "Queues run in this order. Each can be sequential (1 at a "
                    "time) or parallel, paused, scheduled, tied to a category, or "
                    "made to wait for another queue."
                )
            )
        )
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _item: self._edit())
        layout.addWidget(self.list)

        buttons = QHBoxLayout()
        for label, handler in (
            (t("Add…"), self._add),
            (t("Edit…"), self._edit),
            (t("Up"), lambda: self._nudge(-1)),
            (t("Down"), lambda: self._nudge(1)),
            (t("Delete"), self._delete),
        ):
            button = QPushButton(label)
            button.clicked.connect(handler)
            buttons.addWidget(button)
        buttons.addStretch(1)
        close = QPushButton(t("Close"))
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self._reload()

    # -------------------------------------------------------------- helpers

    def _reload(self) -> None:
        self.list.clear()
        queues = {queue.id: queue for queue in self.manager.list_queues()}
        for queue in queues.values():
            traits = []
            if queue.max_concurrent == 1:
                traits.append(t("sequential"))
            elif queue.max_concurrent > 1:
                traits.append(t("parallel x{count}", count=queue.max_concurrent))
            if queue.paused:
                traits.append(t("paused"))
            if queue.schedule_enabled:
                traits.append(f"{queue.start_time}-{queue.stop_time}")
            if queue.category:
                traits.append(t(queue.category).lower())
            if queue.depends_on and queue.depends_on in queues:
                traits.append(t("after '{name}'", name=queues[queue.depends_on].name))
            suffix = f"   ({', '.join(traits)})" if traits else ""
            item = QListWidgetItem(f"{queue.name}{suffix}")
            item.setData(Qt.ItemDataRole.UserRole, queue.id)
            self.list.addItem(item)

    def _selected(self) -> Queue | None:
        row = self.list.currentRow()
        if row < 0:
            return None
        queue_id = self.list.item(row).data(Qt.ItemDataRole.UserRole)
        for queue in self.manager.list_queues():
            if queue.id == queue_id:
                return queue
        return None

    # -------------------------------------------------------------- actions

    def _add(self) -> None:
        name, accepted = QInputDialog.getText(self, t("New queue"), t("Queue name:"))
        if accepted and name.strip():
            queue = self.manager.create_queue(name.strip())
            self._reload()
            self._open_editor(queue)
            self._reload()

    def _edit(self) -> None:
        queue = self._selected()
        if queue is not None:
            self._open_editor(queue)
            self._reload()

    def _open_editor(self, queue: Queue) -> None:
        editor = _QueueEditor(queue, self.manager.list_queues(), self)
        if editor.exec() == QDialog.DialogCode.Accepted:
            self.manager.update_queue(editor.result_queue())

    def _nudge(self, delta: int) -> None:
        """Swap positions with the neighbor (queue priorities)."""
        queue = self._selected()
        if queue is None:
            return
        queues = self.manager.list_queues()
        index = next(i for i, q in enumerate(queues) if q.id == queue.id)
        other_index = index + delta
        if not 0 <= other_index < len(queues):
            return
        other = queues[other_index]
        self.manager.update_queue(replace(queue, position=other.position))
        self.manager.update_queue(replace(other, position=queue.position))
        self._reload()
        self.list.setCurrentRow(other_index)

    def _delete(self) -> None:
        queue = self._selected()
        if queue is None:
            return
        answer = QMessageBox.question(
            self,
            "GrabLine",
            t(
                "Delete queue '{name}'? Its downloads go back to the "
                "default queue (nothing is removed).",
                name=queue.name,
            ),
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.manager.delete_queue(queue.id)
            self._reload()


class _QueueEditor(chrome.Dialog):
    def __init__(self, queue: Queue, queues: list[Queue], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.queue = queue
        self._queues = {q.id: q for q in queues}
        self.setWindowTitle(t("Queue: {name}", name=queue.name))
        self.setMinimumWidth(400)
        form = QFormLayout(self)

        self.name_edit = QLineEdit(queue.name)
        form.addRow(t("Name:"), self.name_edit)

        self.concurrent_spin = QSpinBox()
        self.concurrent_spin.setRange(0, 10)
        self.concurrent_spin.setValue(queue.max_concurrent)
        self.concurrent_spin.setSpecialValueText(t("Global setting"))
        self.concurrent_spin.setToolTip(t("1 = sequential (one at a time, in order)"))
        form.addRow(t("Downloads at once:"), self.concurrent_spin)

        self.paused_check = QCheckBox(t("Paused (holds every download in this queue)"))
        self.paused_check.setChecked(queue.paused)
        form.addRow("", self.paused_check)

        schedule_row = QHBoxLayout()
        self.schedule_check = QCheckBox(t("Only between"))
        self.schedule_check.setChecked(queue.schedule_enabled)
        self.start_edit = QTimeEdit(QTime.fromString(queue.start_time, "HH:mm"))
        self.start_edit.setDisplayFormat("HH:mm")
        self.stop_edit = QTimeEdit(QTime.fromString(queue.stop_time, "HH:mm"))
        self.stop_edit.setDisplayFormat("HH:mm")
        schedule_row.addWidget(self.schedule_check)
        schedule_row.addWidget(self.start_edit)
        schedule_row.addWidget(QLabel(t("and")))
        schedule_row.addWidget(self.stop_edit)
        schedule_row.addStretch(1)
        form.addRow(t("Schedule:"), schedule_row)

        self.category_combo = QComboBox()
        for category in _CATEGORIES:
            self.category_combo.addItem(t(category) if category else t("(none)"), category)
        self.category_combo.setCurrentIndex(max(0, self.category_combo.findData(queue.category)))
        self.category_combo.setToolTip(
            t("New downloads of this type join this queue automatically")
        )
        form.addRow(t("Category:"), self.category_combo)

        self.depends_combo = QComboBox()
        self.depends_combo.addItem(t("(nothing)"), None)
        for other in queues:
            if other.id != queue.id:
                self.depends_combo.addItem(other.name, other.id)
        if queue.depends_on is not None:
            self.depends_combo.setCurrentIndex(
                max(0, self.depends_combo.findData(queue.depends_on))
            )
        self.depends_combo.setToolTip(t("This queue starts only after that queue has finished"))
        form.addRow(t("Wait for queue:"), self.depends_combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _validate_and_accept(self) -> None:
        depends_on = self.depends_combo.currentData()
        if depends_on is not None and _would_cycle(self._queues, self.queue.id, depends_on):
            QMessageBox.warning(
                self,
                "GrabLine",
                t("That would make the queues wait on each other forever."),
            )
            return
        self.accept()

    def result_queue(self) -> Queue:
        return replace(
            self.queue,
            name=self.name_edit.text().strip() or self.queue.name,
            max_concurrent=self.concurrent_spin.value(),
            paused=self.paused_check.isChecked(),
            schedule_enabled=self.schedule_check.isChecked(),
            start_time=self.start_edit.time().toString("HH:mm"),
            stop_time=self.stop_edit.time().toString("HH:mm"),
            category=str(self.category_combo.currentData() or ""),
            depends_on=self.depends_combo.currentData(),
        )
