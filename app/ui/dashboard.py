"""The live dashboard: current/average/peak speed and ETA, downloaded totals
(today / week / month / lifetime), per-server and per-category breakdowns, and
scrolling graphs for download, upload, network, CPU, and disk.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import t
from app.core.manager import DownloadManager
from app.core.models import JobStatus
from app.core.stats import SpeedTracker, SystemSampler
from app.ui import chrome
from app.ui.format import duration_text, human_bytes
from app.ui.graph import Series, TimeGraph


def _speed(value: float) -> str:
    return f"{human_bytes(value)}/s" if value else "0"


def _percent(value: float) -> str:
    return f"{value:.0f}%"


class _Stat(QWidget):
    """A big-number + label tile."""

    def __init__(self, label: str) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(0)
        self.value = QLabel("")
        self.value.setStyleSheet("font-size: 18px; font-weight: 600;")
        caption = QLabel(label)
        caption.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(self.value)
        layout.addWidget(caption)

    def set(self, text: str) -> None:
        self.value.setText(text)


class DashboardDialog(chrome.Dialog):
    def __init__(self, manager: DownloadManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self._speed = SpeedTracker()
        self._system = SystemSampler()
        self.setWindowTitle(t("Dashboard"))
        self.setMinimumSize(720, 560)
        layout = QVBoxLayout(self)

        # -- live speed tiles -------------------------------------------------
        live = QGridLayout()
        self._tiles: dict[str, _Stat] = {}
        for col, (key, label) in enumerate(
            (
                ("current", t("Current speed")),
                ("average", t("Average speed")),
                ("peak", t("Peak speed")),
                ("eta", t("ETA")),
                ("active", t("Active")),
            )
        ):
            tile = _Stat(label)
            self._tiles[key] = tile
            live.addWidget(tile, 0, col)
        layout.addLayout(live)

        # -- downloaded totals ------------------------------------------------
        totals = QGridLayout()
        for col, (key, label) in enumerate(
            (
                ("today", t("Downloaded today")),
                ("week", t("This week")),
                ("month", t("This month")),
                ("lifetime", t("Lifetime")),
                ("files", t("Files")),
            )
        ):
            tile = _Stat(label)
            self._tiles[key] = tile
            totals.addWidget(tile, 0, col)
        layout.addLayout(totals)

        # -- graphs -----------------------------------------------------------
        graphs = QGridLayout()
        self.download_graph = TimeGraph(t("Download"), [Series("dl", QColor(46, 160, 67))], _speed)
        self.upload_graph = TimeGraph(t("Upload"), [Series("ul", QColor(219, 109, 40))], _speed)
        self.network_graph = TimeGraph(
            t("Network (system)"),
            [Series("down", QColor(56, 139, 253)), Series("up", QColor(163, 113, 247))],
            _speed,
        )
        self.cpu_graph = TimeGraph(
            t("CPU"), [Series("cpu", QColor(219, 60, 60))], _percent, fixed_max=100.0
        )
        self.disk_graph = TimeGraph(
            t("Disk (system)"), [Series("disk", QColor(158, 106, 3))], _speed
        )
        for index, graph in enumerate(
            (
                self.download_graph,
                self.upload_graph,
                self.network_graph,
                self.cpu_graph,
                self.disk_graph,
            )
        ):
            graphs.addWidget(graph, index // 2, index % 2)
        layout.addLayout(graphs)

        self._vpn_label = QLabel("")
        self._vpn_label.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(self._vpn_label)

        # -- per-server / per-category ---------------------------------------
        tables = QHBoxLayout()
        self.server_tree = self._make_tree(t("Server"))
        self.category_tree = self._make_tree(t("Category"))
        tables.addWidget(self._wrap(self.server_tree, t("Per-server")))
        tables.addWidget(self._wrap(self.category_tree, t("Per-category")))
        layout.addLayout(tables)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(500)
        self._tick()

    @staticmethod
    def _make_tree(first_column: str) -> QTreeWidget:
        tree = QTreeWidget()
        tree.setHeaderLabels([first_column, t("Downloaded"), t("Files")])
        tree.setRootIsDecorated(False)
        tree.setColumnWidth(0, 180)
        return tree

    @staticmethod
    def _wrap(tree: QTreeWidget, title: str) -> QGroupBox:
        box = QGroupBox(title)
        inner = QVBoxLayout(box)
        inner.setContentsMargins(4, 4, 4, 4)
        inner.addWidget(tree)
        return box

    # ---------------------------------------------------------------- update

    def _tick(self) -> None:
        views = self.manager.snapshot()
        downloaded = sum(v.downloaded for v in views if v.status is JobStatus.DOWNLOADING)
        remaining = 0
        active = 0
        for view in views:
            if view.status is JobStatus.DOWNLOADING:
                active += 1
                if view.total_size:
                    remaining += max(0, view.total_size - view.downloaded)
        reading = self._speed.update(downloaded, remaining if active else None)

        self._tiles["current"].set(_speed(reading.current))
        self._tiles["average"].set(_speed(reading.average))
        self._tiles["peak"].set(_speed(reading.peak))
        self._tiles["eta"].set(duration_text(reading.eta_seconds) or "")
        self._tiles["active"].set(str(active))

        totals = self.manager.stat_totals()
        self._tiles["today"].set(human_bytes(totals["today"]))
        self._tiles["week"].set(human_bytes(totals["week"]))
        self._tiles["month"].set(human_bytes(totals["month"]))
        self._tiles["lifetime"].set(human_bytes(totals["lifetime"]))
        self._tiles["files"].set(str(totals["files"]))

        system = self._system.sample()
        upload = self.manager.torrent_upload_rate()
        self.download_graph.push([reading.current])
        self.upload_graph.push([upload])
        self.network_graph.push([system.net_recv_per_sec, system.net_sent_per_sec])
        self.cpu_graph.push([system.cpu_percent])
        self.disk_graph.push([system.disk_bytes_per_sec])

        from app.core import net

        vpn = net.active_vpn_interfaces()
        self._vpn_label.setText(
            t("VPN active on {interfaces}", interfaces=", ".join(vpn))
            if vpn
            else t("VPN not detected")
        )

        self._fill_tree(self.server_tree, self.manager.stats_by_host())
        self._fill_tree(self.category_tree, self.manager.stats_by_category())

    @staticmethod
    def _fill_tree(tree: QTreeWidget, rows: list[tuple[str, int, int]]) -> None:
        tree.clear()
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        for name, byte_count, files in rows:
            item = QTreeWidgetItem([name, human_bytes(byte_count), str(files)])
            item.setTextAlignment(1, right)
            item.setTextAlignment(2, right)
            tree.addTopLevelItem(item)

    def done(self, result: int) -> None:
        self._timer.stop()
        super().done(result)
