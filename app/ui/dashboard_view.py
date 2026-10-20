"""The embedded Dashboard page: live speed + volume tiles, area-chart cards
(download / upload / network / CPU / disk), a VPN banner, and per-server /
per-category tables. Its timer only runs while the page is on screen.
"""

from __future__ import annotations

import time
from typing import cast

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core import net
from app.core.i18n import t
from app.core.manager import DownloadManager
from app.core.models import JobStatus
from app.core.stats import SpeedTracker, SystemSampler
from app.ui import components, motion, theme, threads
from app.ui.format import human_bytes
from app.ui.work_threads import FileOpThread

#: How often the heavier, slow-changing dashboard readouts (volume rollups, VPN
#: status, per-host/category tables) refresh - seldom enough not to stutter the
#: 60fps graph animation, often enough to feel live.
_HEAVY_INTERVAL = 1.2


def _pct(v: float) -> str:
    return f"{v:.0f}%"


class DashboardView(QWidget):
    def __init__(self, manager: DownloadManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self._speed = SpeedTracker()
        self._system = SystemSampler()
        self._heavy_at = 0.0  # monotonic time of the last heavy refresh
        self._heavy_pending = False
        self._summary_worker: FileOpThread | None = None
        self._stopped = False
        p = theme.current()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        body = QWidget()
        root = QVBoxLayout(body)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(16)
        scroll.setWidget(body)

        self._tiles: dict[str, components.StatTile] = {}

        root.addWidget(components.SectionLabel(t("Speed")))
        root.addLayout(
            self._tile_row(
                [
                    ("current", t("Current"), False),  # data, not an action: no accent
                    ("average", t("Average"), False),
                    ("peak", t("Peak"), False),
                    ("eta", t("ETA (all)"), False),
                    ("active", t("Active"), False),
                ]
            )
        )
        root.addWidget(components.SectionLabel(t("Volume")))
        root.addLayout(
            self._tile_row(
                [
                    ("today", t("Today"), False),
                    ("week", t("This week"), False),
                    ("month", t("This month"), False),
                    ("lifetime", t("Lifetime"), False),
                    ("files", t("Files"), False),
                ]
            )
        )

        root.addWidget(components.SectionLabel(t("Graphs: last 60 seconds")))
        grid = QGridLayout()
        grid.setSpacing(10)
        self.g_download = components.GraphCard(t("Download"), [p.g_dl], motion.fmt_speed)
        self.g_upload = components.GraphCard(t("Upload"), [p.g_ul], motion.fmt_speed)
        self.g_network = components.GraphCard(
            t("Network (system)"), [p.g_ndown, p.g_nup], motion.fmt_speed
        )
        self.g_cpu = components.GraphCard(t("CPU"), [p.g_cpu], _pct, fixed_max=100.0)
        self.g_disk = components.GraphCard(t("Disk I/O"), [p.g_disk], motion.fmt_speed)
        grid.addWidget(self.g_download, 0, 0)
        grid.addWidget(self.g_upload, 0, 1)
        grid.addWidget(self.g_cpu, 1, 0)
        grid.addWidget(self.g_disk, 1, 1)
        grid.addWidget(self.g_network, 2, 0, 1, 2)
        root.addLayout(grid)

        self._vpn = QLabel("")
        self._vpn.setObjectName("VpnBanner")
        root.addWidget(self._vpn)

        tables = QHBoxLayout()
        tables.setSpacing(12)
        self.server_tree = self._table(t("Per server"), t("Host"))
        self.category_tree = self._table(t("Per category"), t("Category"))
        tables.addWidget(self._wrap(self.server_tree))
        tables.addWidget(self._wrap(self.category_tree))
        root.addLayout(tables)
        root.addStretch(1)

        self._timer = QTimer(self)
        # Settings → Statistics: sampling interval (default 500ms).
        self._timer.setInterval(self.manager.settings.dashboard_refresh_ms)
        self._timer.timeout.connect(self._tick)
        # Sample continuously, even while the dashboard isn't the visible page,
        # so the graphs keep a real rolling history instead of starting from
        # empty each time it's opened. Off screen the tick takes its cheap
        # path, and while the whole window is hidden it does nothing - see _tick.
        self._timer.start()

    def _tile_row(self, specs: list[tuple[str, str, bool]]) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        for key, caption, accent in specs:
            tile = components.StatTile(caption, accent=accent)
            self._tiles[key] = tile
            row.addWidget(tile)
        return row

    def _table(self, title: str, first: str) -> QTreeWidget:
        tree = QTreeWidget()
        tree.setObjectName("DashTable")
        tree.setProperty("title", title)  # read by _wrap for the section heading
        tree.setHeaderLabels([first, t("Downloaded"), t("Files")])
        tree.setRootIsDecorated(False)
        tree.setColumnWidth(0, 190)
        # Tall enough to read ~8 rows without scrolling; these were collapsing
        # to a couple of lines because the page's trailing stretch ate the space.
        tree.setMinimumHeight(240)
        return tree

    def _wrap(self, tree: QTreeWidget) -> QWidget:
        """A titled block: the section name above its table."""
        box = QWidget()
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(6)
        col.addWidget(components.SectionLabel(str(tree.property("title") or "")))
        col.addWidget(tree, 1)
        return box

    # ------------------------------------------------------------- lifecycle

    def showEvent(self, event: object) -> None:
        super().showEvent(event)  # type: ignore[arg-type]
        # Keep recent readouts when switching back; a slow interface driver
        # must never hold up painting the page.
        QTimer.singleShot(0, self._tick)

    def _tick(self) -> None:
        if self._stopped:
            return
        # Hidden to the tray: nobody can see the graphs and showEvent refills
        # them on the way back, so sampling here would only burn a full jobs
        # query (and a psutil read) twice a second for no one.
        window = self.window()
        if window is not None and not window.isVisible():
            return
        views = self.manager.snapshot()
        downloaded = sum(v.downloaded for v in views if v.status is JobStatus.DOWNLOADING)
        active = sum(1 for v in views if v.status is JobStatus.DOWNLOADING)
        remaining = sum(
            max(0, (v.total_size or 0) - v.downloaded)
            for v in views
            if v.status is JobStatus.DOWNLOADING and v.total_size
        )
        reading = self._speed.update(downloaded, remaining if active else None)

        # Off screen: keep the graphs' rolling history and nothing else. The
        # tiles, tables and VPN label are read, not stored, so computing them
        # for nobody is pure cost - and this page is off screen most of the
        # time the app is open.
        if not self.isVisible():
            system = self._system.sample()
            self.g_download.push([reading.current])
            self.g_upload.push([self.manager.torrent_upload_rate()])
            self.g_network.push([system.net_recv_per_sec, system.net_sent_per_sec])
            self.g_cpu.push([system.cpu_percent])
            self.g_disk.push([system.disk_bytes_per_sec])
            return

        # Every tick, but cheap: the live speed tiles and the graph samples.
        # The graphs animate at 60fps between these, so this path must stay light
        # or it stalls that animation into a judder (the reason the heavy work
        # below is throttled).
        self._tiles["current"].set_value(motion.fmt_speed(reading.current))
        self._tiles["average"].set_value(motion.fmt_speed(reading.average))
        self._tiles["peak"].set_value(motion.fmt_speed(reading.peak))
        self._tiles["eta"].set_value(motion.fmt_eta(reading.eta_seconds))
        self._tiles["active"].set_value(str(active), t("downloads"))

        system = self._system.sample()
        self.g_download.push([reading.current])
        self.g_upload.push([self.manager.torrent_upload_rate()])
        self.g_network.push([system.net_recv_per_sec, system.net_sent_per_sec])
        self.g_cpu.push([system.cpu_percent])
        self.g_disk.push([system.disk_bytes_per_sec])

        # Heavier and slow-changing (SQLite rollups, interface enumeration, two
        # table rebuilds): refresh a few times a second, not every frame, so the
        # graph animation stays smooth. Still effectively live for these values.
        now = time.monotonic()
        if self._heavy_pending or now - self._heavy_at < _HEAVY_INTERVAL:
            return
        self._heavy_pending = True
        manager = self.manager

        def collect() -> object:
            return (
                manager.stat_totals(),
                net.active_vpn_interfaces(),
                manager.stats_by_host(),
                manager.stats_by_category(),
            )

        worker = FileOpThread(collect)
        self._summary_worker = worker
        worker.done.connect(self._apply_summary)
        threads.retain(worker)
        worker.start()

    @Slot(object, object)
    def _apply_summary(self, result: object, error: object) -> None:
        self._heavy_pending = False
        self._summary_worker = None
        # Measure the interval from completion, so a slow probe cannot cause
        # an endless queue of work or immediately launch another probe.
        self._heavy_at = time.monotonic()
        if error is not None or self._stopped:
            return
        totals, vpn, hosts, categories = cast(
            tuple[
                dict[str, int], list[str], list[tuple[str, int, int]], list[tuple[str, int, int]]
            ],
            result,
        )

        self._tiles["today"].set_value(human_bytes(totals["today"]))
        self._tiles["week"].set_value(human_bytes(totals["week"]))
        self._tiles["month"].set_value(human_bytes(totals["month"]))
        self._tiles["lifetime"].set_value(human_bytes(totals["lifetime"]))
        self._tiles["files"].set_value(f"{totals['files']:,}", t("total"))

        if vpn:
            self._vpn.setText(
                t(
                    "VPN active: {interfaces}    ·    No geo lookup",
                    interfaces=", ".join(vpn),
                )
            )
        else:
            self._vpn.setText(t("VPN not detected"))

        self._fill(self.server_tree, hosts)
        self._fill(self.category_tree, categories)

    def shutdown(self) -> None:
        self._stopped = True
        self._timer.stop()
        if self._summary_worker is not None:
            self._summary_worker.wait()

    @staticmethod
    def _fill(tree: QTreeWidget, rows: list[tuple[str, int, int]]) -> None:
        p = theme.current()
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        from PySide6.QtGui import QColor

        while tree.topLevelItemCount() > len(rows):
            tree.takeTopLevelItem(tree.topLevelItemCount() - 1)
        for index, (name, byte_count, files) in enumerate(rows):
            item = tree.topLevelItem(index)
            if item is None:
                item = QTreeWidgetItem()
                tree.addTopLevelItem(item)
            for column, value in enumerate((name, human_bytes(byte_count), str(files))):
                if item.text(column) != value:
                    item.setText(column, value)
            item.setForeground(1, QColor(p.accent))
            item.setTextAlignment(1, right)
            item.setTextAlignment(2, right)
