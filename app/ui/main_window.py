"""The queue window (F0.4) and the add-URL flow: paste a URL, the resolver
routes it in a background thread, and Smart Engine hits get the quality panel.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from PySide6.QtCore import (
    QEvent,
    QItemSelection,
    QItemSelectionModel,
    QPoint,
    QSignalBlocker,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QColor,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QFontMetrics,
    QGuiApplication,
    QIcon,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app import __version__
from app.core import (
    archive,
    categories,
    convert,
    crawler,
    dupes,
    listio,
    naming,
    reputation,
    reveal,
    rss,
    security,
    update,
    verify,
    virusscan,
)
from app.core.batch import expand_all, expand_pattern, extract_urls
from app.core.errors import DownloadError
from app.core.ffmpeg import find_ffmpeg
from app.core.i18n import N_, t
from app.core.manager import DownloadManager, JobView
from app.core.models import JobKind, JobStatus
from app.core.resolver import Resolution, Resolver
from app.core.settings import MAX_CONNECTIONS, Settings
from app.engines import cloud as cloud_engine
from app.engines import torrent as torrent_engine
from app.engines.smart import (
    cancel_download_prefetch,
    generic_quality_options,
    option_for_label,
    prefetch_download_ready,
)
from app.ui import (
    chrome,
    components,
    design,
    guard,
    icons,
    motion,
    theme,
    threads,
    work_threads,
)
from app.ui.archive_dialog import ArchiveDialog
from app.ui.batch_dialog import BatchImportDialog, BatchImportThread
from app.ui.cloud_dialog import CloudFolderDialog, prompt_cloud_url
from app.ui.dashboard import DashboardDialog
from app.ui.dashboard_view import DashboardView
from app.ui.detail_drawer import DetailDrawer
from app.ui.dupes_dialog import DupesDialog
from app.ui.format import human_bytes
from app.ui.gallery_panel import GalleryPanel
from app.ui.gif_dialog import GifDialog
from app.ui.inspector_dialog import InspectorDialog
from app.ui.link_panel import LinkPanel
from app.ui.playlist_panel import PlaylistPanel
from app.ui.quality_panel import QualityPanel
from app.ui.queue_dialog import QueueManagerDialog
from app.ui.security_dialog import SecurityDialog
from app.ui.setup_dialog import SetupDialog
from app.ui.torrent_dialog import AddTorrentDialog, CreateTorrentDialog

#: Table columns: an icon, name, size, progress, speed, ETA, status.
_COLUMNS = (N_(""), N_("Name"), N_("Size"), N_("Progress"), N_("Speed"), N_("ETA"), N_("Status"))
_COL_ICON, _COL_NAME, _COL_SIZE, _COL_PROGRESS, _COL_SPEED, _COL_ETA, _COL_STATUS = range(7)
_VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}

#: Filter key -> the statuses it shows (empty tuple = everything).
_FILTER_STATUSES: dict[str, tuple[JobStatus, ...]] = {
    "all": (),
    "active": (JobStatus.DOWNLOADING, JobStatus.QUEUED, JobStatus.PAUSED),
    "completed": (JobStatus.COMPLETED,),
    "failed": (JobStatus.FAILED, JobStatus.CANCELLED),
}


class _ScanFlagged(DownloadError):
    """A pre-extract virus scan flagged the archive. Advisory, not fatal: the
    caller asks the user whether to extract anyway."""

    def __init__(self, scanner: str, detail: str) -> None:
        super().__init__(f"{scanner} flagged this archive")
        self.scanner = scanner
        self.detail = detail


class MainWindow(QMainWindow):
    #: Emitted when a download finishes (display name, file path) and when the
    #: last active/queued download drains, so __main__ can toast and act.
    job_completed = Signal(str, str)
    job_failed = Signal(str, str)  # display name, error text
    queue_drained = Signal()

    def __init__(self, manager: DownloadManager, settings: Settings) -> None:
        super().__init__()
        self.manager = manager
        self.settings = settings
        self.resolver = Resolver()
        self.close_to_tray = False
        self.tray: QSystemTrayIcon | None = None
        self.setWindowTitle("GrabLine")
        self.resize(1040, 600)
        # Big enough that the toolbar never overlaps itself and the panels stay
        # aligned; below this the layout starts to crowd, so we don't allow it.
        self.setMinimumSize(940, 560)
        self.setAcceptDrops(True)  # drop URLs (or text with URLs) onto the window
        # Custom chrome: no native title bar; ours draws the caption controls.
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)

        # State that per-row rendering + the theme toggle rely on.
        self._retintable: list[components.IconButton | components.SidebarButton] = []
        self._nav: dict[str, components.SidebarButton] = {}
        self._progress_bars: dict[int, motion.SmoothProgressBar] = {}
        self._pills: dict[int, components.StatusPill] = {}
        #: job id -> the icon state each row currently shows, so the poll can
        #: skip setIcon (which always repaints) when nothing changed.
        self._row_icons: dict[int, tuple[str, str, bool, str]] = {}
        #: What the visible-row filter was last computed from; see _apply_filter.
        self._filter_signature: object = None
        #: job id -> the view each row was last drawn from, so a settled row
        #: can be skipped entirely on the next poll.
        self._rendered: dict[int, JobView] = {}
        self._speed_smoothers: dict[int, motion.SpeedSmoother] = {}
        #: Per-download speed trail for the detail drawer's graph, so switching
        #: away and back restores the history instead of restarting it.
        self._spark_history: dict[int, deque[float]] = {}
        #: Force-removed jobs hidden immediately; cancelling a running worker
        #: is asynchronous, so its row would otherwise linger for seconds.
        self._removing: set[int] = set()
        #: Optimistic status for instant pause/cancel feedback. Pausing an
        #: active download only signals the worker, which keeps the row on
        #: "Downloading" until it winds down (seconds, on a slow read) - so the
        #: button felt dead. We show the intended status right away and let the
        #: real one take over the moment the worker actually settles.
        self._optimistic_status: dict[int, JobStatus] = {}
        #: One-shot actions currently running, so a double-click can't open a
        #: second dialog or start a second background task (see app/ui/guard.py).
        self._in_flight: set[str] = set()
        #: True while a right-click selects its row - selection then must not
        #: pop the details drawer (that is a left-click affordance).
        self._suppress_drawer = False
        #: True while inside a handoff's modal dialog, to stop the 1s handoff
        #: timer re-entering through the nested event loop and stacking dialogs.
        self._in_handoff = False
        #: Set once shutdown() runs; the pollers become no-ops so a late tick
        #: (e.g. the 15s RSS singleShot, which stop() cannot cancel) never
        #: touches the database after it closes.
        self._shutting_down = False

        # Shell: a 48px icon rail on the left, the stacked content on the right.
        self._pages = QStackedWidget()
        self._pages.addWidget(self._build_downloads_page())  # index 0
        # The Dashboard is built now, not on demand: its sampler is what gives
        # the graphs their rolling history, and starting that at first visit
        # would show an empty minute. It costs ~40ms.
        self._dashboard_view = DashboardView(self.manager)
        self._pages.addWidget(self._dashboard_view)  # index 1
        self._page_index = {"downloads": 0, "dashboard": 1}
        # Queue and Settings are built the first time they are opened. Settings
        # alone is ~500ms of widget construction (18 sections, ~100 fields) and
        # most sessions never open it.
        self._queue_view: QWidget | None = None
        self._settings_view: QWidget | None = None
        self._page_factories: dict[str, Callable[[], QWidget]] = {
            "queue": self._make_queue_page,
            "settings": self._make_settings_page,
        }
        shell = QWidget()
        row = QHBoxLayout(shell)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self._build_sidebar())
        row.addWidget(self._pages, 1)
        central = QWidget()
        column = QVBoxLayout(central)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        self._title_bar = chrome.TitleBar(self)
        column.addWidget(self._title_bar)
        column.addWidget(shell, 1)
        self.setCentralWidget(central)
        # Created after the content so the resize overlay stacks above it. The
        # title bar is passed so the overlay carves out the caption buttons and
        # never swallows a Maximize/Close click.
        self._resizer = chrome.EdgeResizer(self, self._title_bar)
        self.statusBar().showMessage(t("Ready"))
        # A global activity indicator: whenever anything is working in the
        # background (analyzing, hashing, extracting, converting, listing,
        # crawling…) this shimmer runs next to the status text, so a wait is
        # never a mystery. Permanent widgets survive showMessage().
        self._busy_ops = 0
        self._busy_bar = motion.SmoothProgressBar()
        self._busy_bar.setFixedWidth(90)
        self._busy_bar.hide()
        self._busy_count = components.role_label("", "muted", size=design.FONT["small"])
        self._busy_count.hide()
        self.statusBar().addPermanentWidget(self._busy_count)
        self.statusBar().addPermanentWidget(self._busy_bar)
        self._status_info = components.role_label("", "muted", size=design.FONT["small"])
        self.statusBar().addPermanentWidget(self._status_info)

        self._row_job_ids: list[int] = []
        self._last_views: dict[int, JobView] = {}
        #: Speed measured once per poll, shared by the rows, drawer and toolbar.
        self._speeds: dict[int, float] = {}
        self._selected_ids: set[int] = set()
        #: The sidebar page currently shown, so Ctrl+Tab can cycle from it.
        self._current_page = "downloads"
        self.clipboard_suppressor: Callable[[str], None] | None = None
        self._prev_status: dict[int, JobStatus] = {}
        self._was_active = False
        self._resolve_threads: list[work_threads.ResolveThread] = []
        self._file_ops: set[work_threads.FileOpThread] = set()
        # Cancel flags for unbounded file ops (the installer download): set on
        # shutdown so the worker stops promptly and the exit wait doesn't time
        # out on a still-running QThread (which Qt aborts the process over).
        self._op_cancels: set[threading.Event] = set()
        self._auto_extracted: set[int] = set()
        self._scanned: set[int] = set()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(500)
        # GrabLine Connect drops URLs into the handoffs table; pick them up.
        # 250ms, not 1s: this poll is the lag between a click in the browser
        # and anything visibly happening, and the claim query is one indexed
        # SELECT on an almost-always-empty table.
        self._handoff_timer = QTimer(self)
        self._handoff_timer.timeout.connect(self._poll_handoffs)
        self._handoff_timer.start(250)
        # RSS torrent feeds: poll on the configured interval (plus once soon
        # after launch so a restart doesn't wait half an hour).
        self._rss_timer = QTimer(self)
        self._rss_timer.timeout.connect(self._poll_rss)
        self._rss_timer.start(self.settings.rss_interval_minutes * 60_000)
        QTimer.singleShot(15_000, self._poll_rss)
        self.refresh()
        self._install_shortcuts()

    # ------------------------------------------------------------- shell

    def _build_sidebar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("Sidebar")
        bar.setFixedWidth(48)
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(5, 10, 5, 10)
        lay.setSpacing(2)
        lay.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        # The rail is pure navigation now that every tool lives on the
        # downloads toolbar. Settings takes the slot the Tools page held.
        nav = (
            ("downloads", "download", t("Downloads"), lambda: self._switch_view("downloads")),
            ("dashboard", "dashboard", t("Dashboard"), lambda: self._switch_view("dashboard")),
            ("queue", "queue", t("Queue manager"), lambda: self._switch_view("queue")),
            ("settings", "settings", t("Settings"), lambda: self._switch_view("settings")),
        )
        for key, icon_name, tip, handler in nav:
            btn = components.SidebarButton(icon_name, tip)
            btn.clicked.connect(handler)
            self._nav[key] = btn
            self._retintable.append(btn)
            lay.addWidget(btn, 0, Qt.AlignmentFlag.AlignHCenter)
        self._nav["downloads"].set_active(True)

        lay.addStretch(1)
        return bar

    def _build_downloads_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        col.addWidget(self._build_toolbar())
        col.addWidget(self._build_filter_bar())
        # table + slide-in detail drawer side by side
        split = QWidget()
        srow = QHBoxLayout(split)
        srow.setContentsMargins(0, 0, 0, 0)
        srow.setSpacing(0)
        srow.addWidget(self._build_table(), 1)
        self._drawer = DetailDrawer(
            self.manager,
            ffmpeg=find_ffmpeg(self.settings),
            on_open_file=self._open_view_file,
            on_open_folder=self._open_view_folder,
            on_rename=self._rename_view,
            on_remove=self._remove_from_drawer,
            on_extract=lambda v: self._extract(Path(v.dest_dir) / v.filename),
            on_security=self._security_view,
        )
        srow.addWidget(self._drawer)
        col.addWidget(split, 1)
        return page

    def _copy_view_url(self, view: JobView) -> None:
        if self.clipboard_suppressor is not None:
            self.clipboard_suppressor(view.url)
        QGuiApplication.clipboard().setText(view.url)
        self.statusBar().showMessage(t("URL copied"), 3000)

    def _copy_view_path(self, view: JobView) -> None:
        QGuiApplication.clipboard().setText(str(Path(view.dest_dir) / view.filename))
        self.statusBar().showMessage(t("File path copied"), 3000)

    def _open_view_file(self, view: JobView) -> None:
        path = Path(view.dest_dir) / view.filename
        if path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            self.statusBar().showMessage(t("The file is not on disk"), 3000)

    def _open_view_folder(self, view: JobView) -> None:
        # The file path, so the manager highlights the download when it exists;
        # open_folder falls back to the folder when it doesn't (a failed job).
        if not reveal.open_folder(Path(view.dest_dir) / view.filename):
            self.statusBar().showMessage(t("Could not open the folder"), 3000)

    def _security_view(self, view: JobView) -> None:
        SecurityDialog(Path(view.dest_dir) / view.filename, view.url, self.settings, self).exec()

    def _rename_view(self, view: JobView) -> None:
        """Rename the file on disk and update the download's name. Refuses a
        blank name, a name that changes folder, or one that would clobber an
        existing file."""
        old = Path(view.dest_dir) / view.filename
        new_name, accepted = QInputDialog.getText(
            self, t("Rename"), t("New file name:"), text=view.filename
        )
        new_name = new_name.strip()
        if not (accepted and new_name) or new_name == view.filename:
            return
        if "/" in new_name or "\\" in new_name:
            QMessageBox.warning(self, "GrabLine", t("The name can't include a folder."))
            return
        target = old.with_name(new_name)
        if target.exists():
            QMessageBox.warning(
                self, "GrabLine", t("{name} already exists in this folder.", name=new_name)
            )
            return
        try:
            if old.exists():
                old.rename(target)
        except OSError as exc:
            QMessageBox.warning(self, "GrabLine", t("Could not rename: {exc}", exc=exc))
            return
        self.manager.db.update_job_filename(view.id, new_name)
        self.refresh()

    def _remove_from_drawer(self, view: JobView) -> None:
        self.manager.remove(view.id, force=True)
        self._removing.add(view.id)
        self._drawer.hide()
        self.refresh()

    def _build_toolbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("Toolbar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(8, 6, 10, 6)
        lay.setSpacing(2)

        def add_btn(icon_name: str, handler: object, tip: str, *, danger: bool = False) -> None:
            btn = components.IconButton(icon_name, danger=danger, tooltip=tip)
            btn.clicked.connect(handler)
            self._retintable.append(btn)
            lay.addWidget(btn)

        def add_menu_btn(icon_name: str, tip: str, items: tuple[tuple[str, object], ...]) -> None:
            btn = components.IconButton(icon_name, tooltip=tip)
            menu = QMenu(btn)
            for label, handler in items:
                menu.addAction(label).triggered.connect(handler)
            btn.clicked.connect(lambda: menu.exec(btn.mapToGlobal(QPoint(0, btn.height()))))
            self._retintable.append(btn)
            lay.addWidget(btn)

        # The one labeled, primary action. Everything else is an icon with a
        # tooltip, grouped by separators, so the whole toolbar fits one row.
        add_url = QPushButton("  " + t("Add URL"))
        add_url.setProperty("accent", "true")
        add_url.setCursor(Qt.CursorShape.PointingHandCursor)
        add_url.setIcon(icons.svg_icon("add", theme.current().accent_on))
        add_url.setIconSize(QSize(16, 16))
        add_url.setToolTip(t("Add a download from a URL"))
        add_url.setMinimumWidth(96)  # never clip its label when the window narrows
        add_url.clicked.connect(self._add_url)
        lay.addWidget(add_url)
        # All torrent actions live under one dropdown, next to the cloud button.
        add_menu_btn(
            "torrent",
            t("Torrent"),
            (
                (t("Add torrent…"), self._add_torrent_file),
                (t("Search torrents…"), self._search_torrents),
                (t("Create torrent…"), self._create_torrent),
            ),
        )
        add_btn("cloud", self._add_cloud, t("Add a cloud download"))

        lay.addWidget(self._sep())
        add_menu_btn(
            "import",
            t("Import"),
            ((t("Import links…"), self._import_links), (t("Import list…"), self._import_list)),
        )
        add_btn("export", self._export_list, t("Export list"))

        lay.addWidget(self._sep())
        add_btn("globe", self._grab_site, t("Grab site"))
        add_btn("inspect", self._inspect_url_prompt, t("Inspect URL"))
        add_btn("duplicate", self._find_duplicates, t("Find duplicate files"))

        lay.addWidget(self._sep())
        # One button that flips between pause and resume for the selection - it
        # shows Pause while something is running, Resume once it's paused. Its
        # icon/tooltip are refreshed by _update_toggle_button on every tick.
        self._toggle_btn = components.IconButton("pause", tooltip=t("Pause"))
        self._toggle_btn.clicked.connect(self._toggle_selected)
        self._retintable.append(self._toggle_btn)
        lay.addWidget(self._toggle_btn)
        add_btn("cancel", self._cancel_selected, t("Cancel"))
        add_btn("trash", self._remove_selected, t("Delete from list"), danger=True)

        lay.addWidget(self._sep())
        add_btn("folder", self._open_folder, t("Open downloads folder"))
        lay.addStretch(1)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText(t("Search downloads…"))
        self.search_box.setClearButtonEnabled(True)
        # Compresses first when the window narrows, so the toolbar never
        # overlaps itself at the minimum window width.
        self.search_box.setMinimumWidth(96)
        self.search_box.setMaximumWidth(210)
        self.search_box.textChanged.connect(lambda _t: self._apply_filter())
        lay.addWidget(self.search_box)
        lay.addWidget(self._sep())

        # Both of these are given a fixed width. They sit at the end of the
        # toolbar, so anything that changes width as the speed changes (":" ->
        # "1.98 MB/s") drags the search box and every button left and right
        # twice a second.
        self.speed_line = motion.Sparkline()
        self.speed_line.setFixedWidth(72)  # compact in the toolbar
        lay.addWidget(self.speed_line)
        self._total_speed = components.role_label("", "strong", size=design.FONT["h2"], bold=True)
        self._total_speed.setFont(design.numeric_font(self._total_speed.font()))
        self._total_speed.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        # Widest reading the formatter can produce, measured in the real font
        # rather than guessed at in pixels.
        self._total_speed.setFixedWidth(
            QFontMetrics(self._total_speed.font()).horizontalAdvance("1023.99 MB/s") + 4
        )
        lay.addWidget(self._total_speed)
        return bar

    def _sep(self) -> QFrame:
        s = QFrame()
        s.setObjectName("Separator")
        s.setFixedSize(1, 18)
        return s

    def _build_filter_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("FilterBar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(10, 0, 10, 0)
        lay.setSpacing(2)
        self._filter = "all"
        self._filter_buttons: dict[str, QPushButton] = {}
        for key, label in (
            ("all", t("All")),
            ("active", t("Active")),
            ("completed", t("Completed")),
            ("failed", t("Failed")),
        ):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, k=key: self._set_filter(k))
            self._filter_buttons[key] = btn
            lay.addWidget(btn)
        lay.addStretch(1)
        # Housekeeping lives with the list it acts on: visible only while
        # there is something completed to clear.
        clear = QPushButton(t("Clear completed"))
        clear.setCursor(Qt.CursorShape.PointingHandCursor)
        clear.setToolTip(t("Remove the completed downloads from the list (files stay on disk)"))
        clear.clicked.connect(self._clear_completed)
        clear.hide()
        self._clear_completed_btn = clear
        lay.addWidget(clear)
        self._style_filter_buttons()
        return bar

    def _style_filter_buttons(self) -> None:
        p = theme.current()
        for key, btn in self._filter_buttons.items():
            active = key == self._filter
            color = p.accent if active else p.text2
            border = p.accent if active else "transparent"
            weight = 600 if active else 400
            btn.setStyleSheet(
                f"QPushButton {{ border: none; background: transparent; padding: 8px 12px;"
                f" color: {color}; font-weight: {weight};"
                f" border-bottom: 2px solid {border}; }}"
                f" QPushButton:hover {{ color: {p.text}; }}"
            )
        self._clear_completed_btn.setStyleSheet(
            f"QPushButton {{ border: none; background: transparent; padding: 8px 12px;"
            f" color: {p.text3}; border-bottom: 2px solid transparent; }}"
            f" QPushButton:hover {{ color: {p.text}; }}"
        )

    def _set_filter(self, key: str) -> None:
        self._filter = key
        self._style_filter_buttons()
        self._apply_filter()

    def _build_table(self) -> QWidget:
        self.table = QTableWidget(0, len(_COLUMNS), self)
        self.table.setObjectName("JobsTable")  # full-bleed styling in the QSS
        self.table.setHorizontalHeaderLabels([t(column) for column in _COLUMNS])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_row_menu)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        header = self.table.horizontalHeader()
        header.setHighlightSections(False)
        self.table.setColumnWidth(_COL_ICON, 30)
        self.table.setColumnWidth(_COL_SIZE, 92)
        self.table.setColumnWidth(_COL_PROGRESS, 150)
        self.table.setColumnWidth(_COL_SPEED, 100)
        self.table.setColumnWidth(_COL_ETA, 84)
        # Hugging pills need less room than the old filled chips did.
        self.table.setColumnWidth(_COL_STATUS, 116)
        header.setStretchLastSection(False)
        # Header alignment matches its column's content: text left, numbers
        # right. (Qt centers headers by default, matching nothing.)
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        for col, align in (
            (_COL_NAME, left),
            (_COL_SIZE, right),
            (_COL_PROGRESS, left),
            (_COL_SPEED, right),
            (_COL_ETA, right),
            (_COL_STATUS, left),
        ):
            item = self.table.horizontalHeaderItem(col)
            if item is not None:
                item.setTextAlignment(align)
        from PySide6.QtWidgets import QHeaderView

        header.setSectionResizeMode(_COL_NAME, QHeaderView.ResizeMode.Stretch)
        # The empty state: one quiet line naming the action that exists,
        # instead of a bare surface. Purely visual; hidden once rows appear.
        self._empty_label = components.role_label(
            t("Nothing here yet. Paste a link or press Add URL."), "muted"
        )
        self._empty_label.setParent(self.table.viewport())
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.hide()
        self._apply_table_prefs()
        return self.table

    def _apply_table_prefs(self) -> None:
        """Appearance prefs: row density and which columns are visible."""
        compact = self.settings.ui_density == "compact"
        self.table.verticalHeader().setDefaultSectionSize(26 if compact else 34)
        hidden = set(self.settings.hidden_columns)
        toggles = {
            "size": _COL_SIZE,
            "progress": _COL_PROGRESS,
            "speed": _COL_SPEED,
            "eta": _COL_ETA,
            "status": _COL_STATUS,
        }
        for key, column in toggles.items():
            self.table.setColumnHidden(column, key in hidden)

    def _make_queue_page(self) -> QWidget:
        from app.ui.queue_view import QueueView

        return QueueView(self.manager)

    def _make_settings_page(self) -> QWidget:
        from app.ui.settings_view import SettingsView

        return SettingsView(self.settings, self._on_settings_applied)

    def _page_for(self, key: str) -> int | None:
        """This page's index in the stack, building it on first use."""
        if key in self._page_index:
            return self._page_index[key]
        factory = self._page_factories.pop(key, None)
        if factory is None:
            return None
        widget = factory()
        if key == "queue":
            self._queue_view = widget
        elif key == "settings":
            self._settings_view = widget
        self._page_index[key] = self._pages.addWidget(widget)
        return self._page_index[key]

    def _switch_view(self, key: str) -> None:
        """Sidebar navigation between the embedded pages."""
        index = self._page_for(key)
        if index is None:
            return
        for nav_key, btn in self._nav.items():
            btn.set_active(nav_key == key)
        self._pages.setCurrentIndex(index)
        self._current_page = key

    def _on_settings_applied(self) -> None:
        """Live-apply after the Settings page saves: theme (may have changed),
        rate/schedule/proxy, and retint the chrome."""
        app = QApplication.instance()
        if isinstance(app, QApplication):
            theme.apply_theme(app, self.settings.theme, accent=self.settings.accent_color or None)
        self.manager.reload_settings()
        self._apply_table_prefs()
        self._retint_all()
        # A rebind in Settings -> Shortcuts takes effect now, no restart.
        if getattr(self, "_shortcuts", None) is not None:
            self._shortcuts.reload()
        self.statusBar().showMessage(t("Settings saved"), 4000)

    def _retint_all(self) -> None:
        self._title_bar.retint()
        for btn in self._retintable:
            btn.retint()
        self._drawer.retint()
        self._style_filter_buttons()
        # Rebuild rows so per-row widgets (pills, bars) repaint in the new theme.
        self._row_job_ids = []
        self._release_row_widgets()
        self.refresh()

    # -------------------------------------------------- keyboard shortcuts

    def _install_shortcuts(self) -> None:
        """Map the shortcut registry (app/ui/shortcuts.py) to the real handlers
        and install the live QShortcuts. Rebuilt on demand by ``reload()`` after
        a change in Settings -> Shortcuts."""
        from app.ui.shortcuts import ShortcutManager

        actions: dict[str, Callable[[], None]] = {
            # General
            "download.add": self._add_url,
            "download.batch": self._import_links,
            "download.paste": self._paste_and_download,
            "torrent.add": self._add_torrent_file,
            "import.links": self._import_links,
            "list.export": self._export_list,
            "site.grab": self._grab_site,
            "url.inspect": self._inspect_url_prompt,
            "folder.open": self._open_folder,
            "search.focus": self._focus_search,
            "settings.open": lambda: self._switch_view("settings"),
            "app.quit": self._quit,
            "view.refresh": self.refresh,
            "help.shortcuts": self._show_shortcuts,
            # Navigation
            "nav.downloads": lambda: self._switch_view("downloads"),
            "nav.dashboard": lambda: self._switch_view("dashboard"),
            "nav.queue": lambda: self._switch_view("queue"),
            "nav.settings": lambda: self._switch_view("settings"),
            "nav.next": lambda: self._cycle_page(1),
            "nav.prev": lambda: self._cycle_page(-1),
            # Filters
            "filter.all": lambda: self._set_filter("all"),
            "filter.active": lambda: self._set_filter("active"),
            "filter.completed": lambda: self._set_filter("completed"),
            "filter.failed": lambda: self._set_filter("failed"),
            # Downloads (list scope)
            "dl.toggle": self._toggle_selected,
            "dl.pause": self._pause_selected,
            "dl.resume": self._resume_selected,
            "dl.remove": self._remove_selected,
            "dl.openfile": self._open_selected_file,
            "dl.openfolder": self._open_selected_folder,
            "dl.redownload": self._redownload_selected,
            "dl.copyurl": self._copy_selected_url,
            "dl.copyhash": self._copy_selected_hash,
            # Downloads (all-jobs, app scope)
            "dl.pauseall": self._pause_all,
            "dl.resumeall": self._resume_all,
            "dl.clear": self._clear_completed,
            # View
            "view.theme": self._toggle_theme,
        }
        self._shortcuts = ShortcutManager(self, self.settings, actions, self.table)
        self._shortcuts.install()

    def _selected_views(self) -> list[JobView]:
        """The JobViews for the currently selected rows (drops any that have
        since left the list)."""
        return [
            view
            for job_id in self._selected_job_ids()
            if (view := self._last_views.get(job_id)) is not None
        ]

    def _paste_and_download(self) -> None:
        text = QGuiApplication.clipboard().text().strip()
        urls = extract_urls(text)
        if not urls and text.lower().startswith(("http://", "https://", "magnet:", "ftp://")):
            urls = [text]
        if not urls:
            self.statusBar().showMessage(t("No downloadable link on the clipboard"), 4000)
            return
        if len(urls) == 1:
            self.begin_add_url(urls[0])
        else:
            self._run_batch(urls)

    def _focus_search(self) -> None:
        self._switch_view("downloads")
        self.search_box.setFocus()
        self.search_box.selectAll()

    def _quit(self) -> None:
        """A real quit that bypasses close-to-tray (Ctrl+Q / the tray menu)."""
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.quit()

    def _show_shortcuts(self) -> None:
        from app.ui.shortcuts_dialog import ShortcutsDialog

        with guard.single_flight(self._in_flight, "shortcuts") as go:
            if not go:
                return
            ShortcutsDialog(self._shortcuts.effective(), self).exec()

    def _cycle_page(self, delta: int) -> None:
        order = ("downloads", "dashboard", "queue", "settings")
        try:
            index = order.index(self._current_page)
        except ValueError:
            index = 0
        self._switch_view(order[(index + delta) % len(order)])

    def _toggle_selected(self) -> None:
        views = self._selected_views()
        if not views:
            return
        # If anything selected is live, the toggle pauses everything selected;
        # otherwise it resumes. One key, both directions, like a play/pause.
        active = any(v.status in (JobStatus.DOWNLOADING, JobStatus.QUEUED) for v in views)
        for view in views:
            if active:
                self._pause_job(view.id)
            else:
                self._resume_job(view.id)
        self.refresh()

    def _update_toggle_button(self) -> None:
        """The one toolbar pause/resume button: Pause while anything selected is
        running, Resume once it's all paused/stopped, Pause when nothing is
        selected."""
        views = self._selected_views()
        resume = bool(views) and not any(
            v.status in (JobStatus.DOWNLOADING, JobStatus.QUEUED) for v in views
        )
        self._toggle_btn.set_icon_name("resume" if resume else "pause")
        self._toggle_btn.setToolTip(t("Resume") if resume else t("Pause"))

    def _pause_all(self) -> None:
        for view in list(self._last_views.values()):
            if view.status in (JobStatus.DOWNLOADING, JobStatus.QUEUED):
                self._pause_job(view.id)
        self.refresh()

    def _resume_all(self) -> None:
        for view in list(self._last_views.values()):
            if view.status in (JobStatus.PAUSED, JobStatus.FAILED, JobStatus.CANCELLED):
                self._resume_job(view.id)
        self.refresh()

    def _open_selected_file(self) -> None:
        views = self._selected_views()
        if not views:
            return
        view = views[0]
        path = Path(view.dest_dir) / view.filename
        if view.status is JobStatus.COMPLETED and path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            self.statusBar().showMessage(t("That download isn't finished yet"), 3000)

    def _open_selected_folder(self) -> None:
        views = self._selected_views()
        if not views:
            return
        view = views[0]
        if not reveal.open_folder(Path(view.dest_dir) / view.filename):
            self.statusBar().showMessage(t("Could not open the folder"), 3000)

    def _redownload_selected(self) -> None:
        for view in self._selected_views():
            self.begin_add_url(view.url, allow_duplicate=True)

    def _copy_selected_url(self) -> None:
        views = self._selected_views()
        if not views:
            return
        url = views[0].url
        if self.clipboard_suppressor is not None:
            self.clipboard_suppressor(url)  # don't offer our own copy back
        QGuiApplication.clipboard().setText(url)
        self.statusBar().showMessage(t("URL copied"), 3000)

    def _copy_selected_hash(self) -> None:
        views = self._selected_views()
        if not views:
            return
        view = views[0]
        path = Path(view.dest_dir) / view.filename
        if view.status is JobStatus.COMPLETED and path.exists():
            self._copy_hash(path)
        else:
            self.statusBar().showMessage(t("Finish the download first to hash it"), 3000)

    def _toggle_theme(self) -> None:
        """Flip between light and dark and repaint live (Ctrl+Shift+L). Writes
        an explicit theme, so it also pins a 'system' theme to a real choice."""
        self.settings.theme = "light" if theme.is_dark() else "dark"
        app = QApplication.instance()
        if isinstance(app, QApplication):
            theme.apply_theme(app, self.settings.theme, accent=self.settings.accent_color or None)
        self._retint_all()

    def open_setup(self) -> None:
        # Reachable from both the first-run timer and the menu, so guard it: the
        # two must never stack a second wizard on top of the first.
        with guard.single_flight(self._in_flight, "setup") as go:
            if not go:
                return
            SetupDialog(self, settings=self.settings).exec()

    def shutdown(self) -> None:
        """Quiesce the window before the app closes the database. Stop the
        polling timers (so no tick touches a closed connection), then wait for
        this window's own worker threads to finish. A file-op or resolve thread
        is parented to the window; if it were still running when the window is
        destroyed at exit, Qt would abort the process. These workers wrap
        bounded operations (a subprocess convert, a network resolve), so the
        wait terminates."""
        self._shutting_down = True
        for timer in (self._timer, self._handoff_timer, self._rss_timer):
            timer.stop()
        # Ask any unbounded op (an in-flight installer download) to stop first, so
        # its worker returns quickly and the wait below doesn't time out on a
        # still-running thread - the abort in the update-download crash report.
        for cancel in self._op_cancels:
            cancel.set()
        for worker in [*self._file_ops, *self._resolve_threads]:
            worker.wait(8000)

    def _poll_handoffs(self) -> None:
        # Gallery/links handoffs open a modal dialog (exec) whose nested event
        # loop keeps this 1s timer firing. Without a guard, rapid browser
        # clicks re-enter here and stack modal dialogs on top of each other.
        # The guard makes a re-entrant tick a no-op; any handoffs that arrived
        # meanwhile are simply claimed on the next poll after the modal closes.
        if self._shutting_down or self._in_handoff:
            return
        self._in_handoff = True
        try:
            self._drain_handoffs()
        finally:
            self._in_handoff = False

    def _drain_handoffs(self) -> None:
        seen: set[str] = set()
        for handoff in self.manager.db.claim_handoffs():
            # One browser click can deposit the same URL many times (extension
            # retry / dual webRequest+onCreated). Claiming once still left a
            # batch of identical rows - collapse them here so we only open one
            # dialog and only queue one job.
            if handoff.source not in ("focus", "gallery", "links", "torrent", "cloud"):
                key = handoff.url.strip()
                if key in seen:
                    continue
                seen.add(key)
            if handoff.source == "focus":
                # "Open GrabLine" from the extension: raise the window, and jump
                # to a named page (e.g. settings) when one was requested.
                self.show()
                self.raise_()
                self.activateWindow()
                if handoff.page_title in self._page_index:
                    self._switch_view(handoff.page_title)
            elif handoff.source == "gallery" and handoff.payload:
                self._open_gallery(list(handoff.payload), handoff.page_title)
            elif handoff.source == "links" and handoff.payload:
                self._open_links(list(handoff.payload), handoff.page_title)
            elif handoff.source == "torrent" or torrent_engine.is_torrent_source(handoff.url):
                # 'Open with GrabLine' on a .torrent / magnet, from any source.
                self.add_torrent_source(handoff.url)
            elif handoff.source == "cloud" or cloud_engine.is_cloud_scheme(handoff.url):
                self.add_cloud_source(handoff.url)
            else:
                self.begin_add_url(
                    handoff.url,
                    page_title=handoff.page_title,
                    quality=handoff.quality,
                    fallbacks=handoff.payload,
                    headers=handoff.headers,
                    from_browser=True,
                )

    def _open_gallery(self, urls: list[str], page_title: str | None) -> None:
        """F2.2: the extension collected a page's images - pick and batch."""
        panel = GalleryPanel(urls, page_title=page_title, parent=self)
        if panel.exec() != GalleryPanel.DialogCode.Accepted:
            return
        for url in panel.selected_urls():
            self.manager.add_url(url)
        self.refresh()

    def _open_links(self, urls: list[str], page_title: str | None) -> None:
        """The extension collected a page's downloadable links - pick, then
        queue them through the resolver like a batch import."""
        panel = LinkPanel(urls, page_title=page_title, parent=self)
        if panel.exec() != LinkPanel.DialogCode.Accepted:
            return
        self._run_batch(panel.selected_urls())

    # ------------------------------------------------------------- actions

    def _add_url(self) -> None:
        url, accepted = QInputDialog.getText(
            self, t("Add download"), t("URL (ranges like file[1-20].jpg expand):")
        )
        if not (accepted and url.strip()):
            return
        expanded = expand_pattern(url.strip())
        if len(expanded) > 1:
            self._run_batch(expanded)  # a pattern: queue them all at defaults
        else:
            self.begin_add_url(expanded[0])

    def _import_links(self) -> None:
        """F2.4: paste/load many URLs; they queue at defaults, no panels."""
        dialog = BatchImportDialog(self)
        if dialog.exec() != BatchImportDialog.DialogCode.Accepted:
            return
        self._run_batch(dialog.urls())

    def _grab_site(self) -> None:
        """Crawl a page (optionally deeper) and pick from the files it finds."""
        url, accepted = QInputDialog.getText(self, t("Grab site"), t("Page URL:"))
        url = url.strip()
        if not (accepted and url):
            return
        depth, accepted = QInputDialog.getInt(
            self,
            t("Grab site"),
            t("How many levels deep to follow links?"),
            value=0,
            minValue=0,
            maxValue=3,
        )
        if not accepted:
            return
        self.statusBar().showMessage(t("Scanning {url} …", url=url))
        proxy = self.settings.proxy

        def done(result: object) -> None:
            found = cast(list[str], result)
            self.statusBar().showMessage(t("Found {count} file link(s)", count=len(found)), 6000)
            if found:
                self._open_links(found, url)
            else:
                QMessageBox.information(
                    self, "GrabLine", t("No downloadable files found on that page.")
                )

        self._run_file_op(partial(crawler.crawl, url, depth=depth, proxy=proxy), done)

    def _export_list(self) -> None:
        path, _f = QFileDialog.getSaveFileName(
            self, t("Export download list"), "grabline-downloads.json", "JSON (*.json)"
        )
        if not path:
            return
        try:
            count = listio.write_file(self.manager.db, Path(path))
        except OSError as exc:
            QMessageBox.warning(self, "GrabLine", t("Could not export: {exc}", exc=exc))
            return
        self.statusBar().showMessage(t("Exported {count} download(s)", count=count), 6000)

    def _import_list(self) -> None:
        path, _f = QFileDialog.getOpenFileName(
            self, t("Import download list"), "", "JSON (*.json);;All files (*)"
        )
        if not path:
            return
        try:
            count = listio.read_file(self.manager.db, Path(path))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "GrabLine", t("Could not import: {exc}", exc=exc))
            return
        self.statusBar().showMessage(t("Imported {count} download(s)", count=count), 6000)
        self.refresh()

    def check_for_updates(self, *, quiet: bool) -> None:
        """Look for a newer release; ``quiet`` skips the 'up to date' notice."""
        if not guard.begin(self._in_flight, "update"):
            return  # a check is already running - don't stack a second dialog
        if not quiet:
            self.statusBar().showMessage(t("Checking for updates…"))
        proxy = self.settings.proxy

        def done(result: object) -> None:
            guard.end(self._in_flight, "update")
            self.statusBar().showMessage(t("Ready"))  # never leave "Checking…" stuck
            if result is not None:
                tag, name, url, size = cast("tuple[str, str, str, int]", result)
                box = QMessageBox(self)
                box.setWindowTitle("GrabLine")
                box.setText(
                    t(
                        "GrabLine {tag} is available (you have {version}).\n\n"
                        "Update now downloads the installer and opens it.",
                        tag=tag,
                        version=__version__,
                    )
                )
                update_btn = box.addButton(t("Update now"), QMessageBox.ButtonRole.AcceptRole)
                site_btn = box.addButton(t("Download page"), QMessageBox.ButtonRole.ActionRole)
                box.addButton(QMessageBox.StandardButton.Cancel)
                box.exec()
                if box.clickedButton() is update_btn:
                    self._download_and_run_installer(name, url, size)
                elif box.clickedButton() is site_btn:
                    QDesktopServices.openUrl(QUrl(update.WEBSITE_DOWNLOAD_URL))
            elif not quiet:
                QMessageBox.information(self, "GrabLine", t("You have the latest version."))

        def failed(error: object) -> None:
            guard.end(self._in_flight, "update")
            self.statusBar().showMessage(t("Ready"))
            if not quiet:
                detail = str(error).strip() if error else ""
                QMessageBox.information(
                    self,
                    "GrabLine",
                    t("Could not check for updates right now.")
                    + (f"\n\n{detail}" if detail else ""),
                )

        self._run_file_op(partial(update.installer_update, proxy), done, failed)

    def _download_and_run_installer(self, name: str, url: str, size: int = 0) -> None:
        """Fetch the new installer to the download folder and open it - the
        closest we get to auto-update without a signed self-updater."""
        from PySide6.QtWidgets import QProgressBar, QProgressDialog

        progress = QProgressDialog(t("Downloading {name}…", name=name), t("Cancel"), 0, 1000, self)
        progress.setWindowTitle(t("GrabLine update"))
        progress.setMinimumDuration(0)
        bar = QProgressBar(progress)
        bar.setRange(0, 1000)
        bar.setTextVisible(False)  # the themed 5px bar has no room for "42%"
        progress.setBar(bar)
        proxy = self.settings.proxy
        dest = str(self.settings.download_dir)

        # Cancel must actually stop the download, not just hide the dialog and
        # then open the installer anyway. Signal the worker through an event its
        # streaming loop checks each chunk; clicking Cancel (or Esc) sets it.
        cancel_event = threading.Event()
        progress.canceled.connect(cancel_event.set)
        # Also cancelled on app shutdown, so quitting mid-download stops the
        # worker instead of leaving a running thread for Qt to abort over.
        self._op_cancels.add(cancel_event)

        relay = work_threads.ProgressRelay(self)
        relay.tick.connect(progress.setValue)
        relay.status.connect(progress.setLabelText)
        if size > 0:
            progress.setLabelText(
                t(
                    "Downloading {name}… {done} / {total}",
                    name=name,
                    done=human_bytes(0),
                    total=human_bytes(size),
                )
            )

        def report(received: int, total: object) -> None:
            # Called on the worker thread: emitting a signal is thread-safe
            # (queued to the GUI thread); starting a QTimer here is not.
            # Use 0..1000 so a 150MB AppImage moves the bar every ~150KB
            # instead of sitting on "0%" until a full percent (~1.5MB).
            known = total if isinstance(total, int) and total > 0 else (size if size > 0 else None)
            if known is not None:
                relay.tick.emit(min(1000, int(received * 1000 / known)))
                relay.status.emit(
                    t(
                        "Downloading {name}… {done} / {total}",
                        name=name,
                        done=human_bytes(received),
                        total=human_bytes(known),
                    )
                )
            else:
                relay.status.emit(
                    t(
                        "Downloading {name}… {done}",
                        name=name,
                        done=human_bytes(received),
                    )
                )

        def done(result: object) -> None:
            self._op_cancels.discard(cancel_event)
            progress.close()
            path = str(result)
            self.statusBar().showMessage(t("Update downloaded: {path}", path=path), 8000)
            # Open the installer; the user finishes the (unsigned) wizard.
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

        def failed(error: object) -> None:
            self._op_cancels.discard(cancel_event)
            progress.close()
            if isinstance(error, update.UpdateCancelled):
                self.statusBar().showMessage(t("Update cancelled"), 5000)
                return
            answer = QMessageBox.question(
                self,
                "GrabLine",
                t(
                    "Could not download the update ({error}).\nOpen the download page instead?",
                    error=error,
                ),
            )
            if answer == QMessageBox.StandardButton.Yes:
                QDesktopServices.openUrl(QUrl(update.WEBSITE_DOWNLOAD_URL))

        self._run_file_op(
            lambda: update.download_installer(
                url,
                dest,
                name,
                proxy=proxy,
                progress=report,
                cancel=cancel_event,
                expected_size=size or None,
            ),
            done,
            failed,
        )

    def _run_batch(self, urls: list[str]) -> None:
        """Queue many URLs through the resolver at sensible defaults."""
        if not urls:
            return
        thread = BatchImportThread(self.manager, self.settings, urls)
        self._busy_begin()
        thread.progress.connect(
            lambda done, total: self.statusBar().showMessage(
                t("Importing {done}/{total} …", done=done, total=total)
            )
        )
        thread.summary.connect(self._on_batch_summary)
        thread.finished.connect(self._busy_end)
        thread.start_tracked()

    def _on_batch_summary(self, queued: int, skipped: object) -> None:
        items = cast(list[tuple[str, str]], skipped)
        message = t("Imported {count} download(s)", count=queued)
        if items:
            message += t(", skipped {count}", count=len(items))
        self.statusBar().showMessage(message, 10000)
        if items:
            detail = "\n".join(f"• {url}: {reason}" for url, reason in items[:10])
            if len(items) > 10:
                detail += t("\n… and {count} more", count=len(items) - 10)
            QMessageBox.information(self, t("Import finished"), f"{message}.\n\n{detail}")
        self.refresh()

    def begin_add_url(
        self,
        url: str,
        page_title: str | None = None,
        quality: str | None = None,
        fallbacks: tuple[str, ...] = (),
        headers: dict[str, str] | None = None,
        allow_duplicate: bool = False,
        from_browser: bool = False,
    ) -> None:
        """Entry point shared by the toolbar, tray, clipboard, and extension.
        A ``quality`` label (F1.3 in-page panel) skips the quality dialog;
        ``fallbacks`` are sniffed stream URLs tried in order if ``url``
        resolves to nothing (blob players on streaming sites); ``headers``
        are browser cookies/referer passed through for login-gated files."""
        if not allow_duplicate:
            existing = self.manager.find_existing(url)
            if existing is not None:
                # Browser takeover can re-send the same URL while the first job
                # is still running. Asking "download again?" for every retry is
                # how one click became 10-15 cancel clicks - just keep the one
                # we already have.
                if from_browser and existing.status in (
                    JobStatus.QUEUED,
                    JobStatus.DOWNLOADING,
                    JobStatus.PAUSED,
                    JobStatus.COMPLETED,
                ):
                    self.statusBar().showMessage(
                        t("Already in the list: {name}", name=existing.filename), 4000
                    )
                    return
                what = (
                    t("was already downloaded")
                    if existing.status is JobStatus.COMPLETED
                    else t("is already in the list")
                )
                answer = QMessageBox.question(
                    self,
                    "GrabLine",
                    t(
                        "{name} {what}.\nDownload it again?",
                        name=existing.filename,
                        what=what,
                    ),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    self.statusBar().showMessage(t("Ready"))
                    return

        # Low-disk warning (advisory, Settings → Downloads): free space on the
        # download drive below the configured floor still lets you proceed.
        floor_mb = self.settings.min_free_mb
        if floor_mb:
            import shutil as _shutil

            try:
                free_mb = _shutil.disk_usage(str(self.settings.download_dir)).free // (1024 * 1024)
            except OSError:
                free_mb = None
            if free_mb is not None and free_mb < floor_mb:
                answer = QMessageBox.warning(
                    self,
                    "GrabLine",
                    t(
                        "Only {free_mb} MB free on the download drive "
                        "(warning floor: {floor_mb} MB). Download anyway?",
                        free_mb=free_mb,
                        floor_mb=floor_mb,
                    ),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    self.statusBar().showMessage(t("Ready"))
                    return
        # Advisory URL security: warn on plain HTTP (instant) and, if a Safe
        # Browsing key is set, check the URL off-thread. Both only warn - the
        # user can always proceed.
        scheme = urlsplit(url).scheme.lower()
        if self.settings.enforce_https and scheme == "http" and not self._confirm_insecure(url):
            self.statusBar().showMessage(t("Ready"))
            return
        args = (page_title, quality, fallbacks, headers, from_browser)
        if self.settings.safebrowsing_key and scheme in ("http", "https"):
            self._safebrowsing_then_resolve(url, args)
            return
        self._finish_add(url, *args)

    def _finish_add(
        self,
        url: str,
        page_title: str | None,
        quality: str | None,
        fallbacks: tuple[str, ...],
        headers: dict[str, str] | None,
        from_browser: bool,
    ) -> None:
        # A download started in the browser opens the Download Info dialog; a
        # paste/import goes straight through the resolver as before.
        if from_browser:
            self._browser_add(url, page_title, fallbacks, headers)
        else:
            self._resolve_and_queue(url, page_title, quality, fallbacks, headers)

    def _confirm_insecure(self, url: str) -> bool:
        answer = QMessageBox.warning(
            self,
            "GrabLine",
            t(
                "{url}\n\nThis download is over unencrypted HTTP. It could be "
                "tampered with in transit. Download anyway?",
                url=url,
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _safebrowsing_then_resolve(
        self,
        url: str,
        args: tuple[str | None, str | None, tuple[str, ...], dict[str, str] | None, bool],
    ) -> None:
        key = self.settings.safebrowsing_key
        proxy = self.settings.proxy
        self.statusBar().showMessage(t("Checking Safe Browsing …"))

        def done(result: object) -> None:
            threat = str(result) if result else ""
            if threat:
                answer = QMessageBox.warning(
                    self,
                    "GrabLine",
                    t(
                        "{url}\n\nGoogle Safe Browsing flags this as {threat}. Download anyway?",
                        url=url,
                        threat=threat,
                    ),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    self.statusBar().showMessage(t("Ready"))
                    return
            self._finish_add(url, *args)

        self._run_file_op(
            lambda: reputation.safebrowsing_check(url, key, proxy=proxy),
            done,
            lambda _e: self._finish_add(url, *args),  # a failed check never blocks
        )

    def _raise_to_front(self) -> None:
        """Bring GrabLine forward so its dialog or panel is right there, the way
        IDM pops up when you start a download in the browser."""
        self.show()
        self.raise_()
        self.activateWindow()

    def _browser_add(
        self,
        url: str,
        page_title: str | None,
        fallbacks: tuple[str, ...],
        headers: dict[str, str] | None,
    ) -> None:
        """A download started from the browser.

        A smart-engine video (YouTube and friends) is analysed so its real title
        resolves - a hover on a thumbnail no longer saves as "watch" - and the
        full quality panel (every format, subtitles, trimming) is offered, the
        same rich flow a pasted URL gets. A stream or a plain file takes the fast
        Download Info dialog with no analysis, unless that dialog is turned off.
        """
        if not self.resolver.smart.warm:
            # matches() below needs the extractor list, and building it costs
            # the GUI thread a couple of seconds of yt-dlp import - which is
            # exactly when a browser handoff tends to arrive, giving the "I
            # clicked download and GrabLine froze" report. Build it off-thread
            # and pick this up again after.
            self._when_smart_ready(lambda: self._browser_add(url, page_title, fallbacks, headers))
            return
        is_video = self.resolver.smart.matches(url)
        is_stream = urlsplit(url).path.lower().endswith((".m3u8", ".mpd"))
        if not is_video and not is_stream and fallbacks:
            # A blob-backed player: the sniffed stream the page loaded is the
            # real media, so grab that instead of the page's HTML.
            url = next(
                (f for f in fallbacks if urlsplit(f).path.lower().endswith((".m3u8", ".mpd"))),
                fallbacks[0],
            )
            is_stream = urlsplit(url).path.lower().endswith((".m3u8", ".mpd"))

        if is_video:
            if self.settings.confirm_downloads:
                # Analyse and show the full quality panel (formats, subtitles,
                # trimming) with the real title - the flow the user asked to get
                # back for YouTube. _resolve_and_queue with quality=None runs the
                # extractor, then _on_resolved opens QualityPanel.
                self._raise_to_front()
                self._resolve_and_queue(url, page_title, None, fallbacks, headers)
            else:
                # 'Start immediately': queue at the default quality, no panel, but
                # still with the real title (fetched via oEmbed) rather than the
                # URL's "watch" leaf.
                self._resolve_and_queue(
                    url, page_title, self.settings.video_default_quality, fallbacks, headers
                )
            return

        if is_stream:
            name = naming.clean_page_title(page_title) or Path(naming.filename_from_url(url)).stem
            category = "Video"
        else:
            name = naming.improved_filename(url, page_title, None)
            category = categories.category_for(name) or "Documents"

        if not self.settings.confirm_downloads:
            dest = str(categories.dest_dir_for(self.settings.download_dir, name, enabled=True))
            self._queue_download(url, name, dest, None, False, is_stream, headers, False)
            return

        from app.ui.add_download_dialog import AddDownloadDialog

        dialog = AddDownloadDialog(
            url,
            suggested_name=name,
            category=category,
            download_dir=str(self.settings.download_dir),
            with_quality=False,
            parent=self,
        )
        self._raise_to_front()
        if dialog.exec() != AddDownloadDialog.DialogCode.Accepted:
            self.statusBar().showMessage(t("Ready"))
            return
        if dialog.dont_ask_again():
            self.settings.confirm_downloads = False
        self._queue_download(
            url,
            dialog.chosen_name(),
            dialog.chosen_directory() or None,
            None,
            False,
            is_stream,
            headers,
            dialog.outcome() == "later",
        )

    def _queue_download(
        self,
        url: str,
        name: str,
        dest: str | None,
        quality: str | None,
        is_video: bool,
        is_stream: bool,
        headers: dict[str, str] | None,
        paused: bool,
    ) -> None:
        if is_video:
            option = option_for_label(quality or "Best") or generic_quality_options()[0]
            job = self.manager.add_smart_entry(
                url,
                name or "video",
                option,
                dest_dir=dest,
                use_session=self.settings.use_browser_session,
                session_browser=self.settings.session_browser,
                headers=headers,
            )
        elif is_stream:
            job = self.manager.add_hls(url, dest_dir=dest, title=name or None, headers=headers)
        else:
            job = self.manager.add_url(url, dest_dir=dest, filename=name or None, headers=headers)
        if paused:
            self.manager.pause(job.id)
        message = t("Queued {name}", name=name) if name else t("Queued")
        self.statusBar().showMessage(message, 5000)
        self.refresh()

    def _when_smart_ready(self, then: Callable[[], None]) -> None:
        """Build the yt-dlp extractor list off-thread, then run ``then``.

        The list is ~1700 classes behind a yt-dlp import: seconds of work on
        first use (worse on Windows behind antivirus). The startup warm-up
        usually gets there first; this covers a URL arriving before it does.
        """
        self.statusBar().showMessage(t("Starting…"))
        self._busy_begin()
        thread = work_threads.FileOpThread(self.resolver.smart.warm_up)

        def ready(_result: object, _error: object) -> None:
            self._busy_end()
            then()

        thread.done.connect(ready)
        threads.retain(thread)
        thread.start()

    def _resolve_and_queue(
        self,
        url: str,
        page_title: str | None,
        quality: str | None,
        fallbacks: tuple[str, ...],
        headers: dict[str, str] | None,
    ) -> None:
        # The in-page quality panel already chose - skip analysis entirely and
        # let the download's single extraction do everything (formats resolve
        # at download time, the file is named from the real title). This is
        # what makes a hover-button YouTube add start as fast as any other
        # site: one extraction instead of two.
        if quality and not self.resolver.smart.warm:
            # Answering "is this a smart-engine URL?" needs the extractor list;
            # building it on the GUI thread is a multi-second freeze. Warm it
            # behind the window and come straight back here.
            self._when_smart_ready(
                lambda: self._resolve_and_queue(url, page_title, quality, fallbacks, headers)
            )
            return
        if quality and self.resolver.smart.matches(url):
            option = option_for_label(quality)
            if option is not None:
                # The page title is often just the site name ("YouTube") -
                # queue with a placeholder and fetch the real title via the
                # site's oEmbed endpoint, which answers in well under a second.
                placeholder = naming.clean_page_title(page_title) or t("Fetching title…")
                job = self.manager.add_smart_entry(
                    url,
                    placeholder,
                    option,
                    use_session=self.settings.use_browser_session,
                    session_browser=self.settings.session_browser,
                    extras={"name_from_metadata": True},
                    headers=headers,
                )
                self.statusBar().showMessage(t("Queued ({label})", label=option.label), 5000)
                self.refresh()
                self._fetch_quick_title(job.id, url)
                return
        self.statusBar().showMessage(t("Analyzing {url} …", url=url))
        self._busy_begin()
        thread = work_threads.ResolveThread(
            self.resolver, url, self.settings, page_title, quality, fallbacks, headers, self
        )
        thread.resolved.connect(self._on_resolved)

        def _resolve_finished() -> None:
            self._resolve_threads.remove(thread)
            self._busy_end()
            thread.deleteLater()

        thread.finished.connect(_resolve_finished)
        self._resolve_threads.append(thread)
        thread.start()

    def _ask_dest(self) -> str | None:
        """Settings → Downloads 'Ask where to save': a folder for this add,
        "" when the setting is off (use defaults), or None on cancel."""
        if not self.settings.ask_save_dir:
            return ""
        chosen = QFileDialog.getExistingDirectory(
            self, t("Save this download to"), str(self.settings.download_dir)
        )
        return chosen or None

    def _fetch_quick_title(self, job_id: int, url: str) -> None:
        """Replace a queued job's placeholder name with the real video title
        (oEmbed, ~instant). Best effort - the download's own metadata naming
        corrects the file at completion regardless."""
        from app.core import titles

        proxy = self.settings.proxy

        def done(result: object) -> None:
            title = str(result) if result else ""
            if not title or self.manager.db.get_job(job_id) is None:
                return
            self.manager.db.update_job_title(job_id, title)
            self.refresh()

        self._run_file_op(lambda: titles.quick_title(url, proxy), done, lambda _e: None)

    def _on_resolved(
        self,
        resolution: Resolution,
        page_title: str | None,
        quality: str | None = None,
        fallbacks: tuple[str, ...] = (),
        headers: dict[str, str] | None = None,
    ) -> None:
        self.statusBar().showMessage(t("Ready"))
        if resolution.kind is None:
            if fallbacks:
                # The page itself had nothing - try the stream it played.
                self.statusBar().showMessage(t("Page had no direct media, trying its stream …"))
                self.begin_add_url(
                    fallbacks[0],
                    page_title=page_title,
                    quality=quality,
                    fallbacks=tuple(fallbacks[1:]),
                    headers=headers,
                )
                return
            QMessageBox.information(self, "GrabLine", resolution.message or t("No media found."))
            return
        if resolution.kind is JobKind.TORRENT:
            self.add_torrent_source(resolution.url)
            return
        if resolution.kind is JobKind.CLOUD:
            self.add_cloud_source(resolution.url)
            return
        if (
            quality
            and resolution.kind is JobKind.SMART
            and resolution.media is not None
            and (option := option_for_label(quality, resolution.media.options)) is not None
        ):
            # F1.3: the quality was already chosen in the page - no dialog.
            dest = self._ask_dest()
            if dest is None:
                return
            self.manager.add_smart(
                resolution.url,
                resolution.media,
                option,
                dest_dir=dest or None,
                use_session=self.settings.use_browser_session,
                session_browser=self.settings.session_browser,
                headers=headers,
            )
            self.statusBar().showMessage(
                t("Queued {name} ({label})", name=resolution.media.title, label=option.label),
                5000,
            )
            self.refresh()
            return
        # Kick off the download-ready extract before any folder/quality UI so
        # even "ask where to save" time overlaps with YouTube's slow pass.
        if resolution.kind is JobKind.SMART and resolution.media is not None:
            prefetch_download_ready(
                resolution.url,
                proxy=self.settings.proxy,
                session_browser=self.settings.session_browser or None,
                headers=headers,
            )
        dest = self._ask_dest()
        if dest is None:
            if resolution.kind is JobKind.SMART and resolution.media is not None:
                cancel_download_prefetch(resolution.url, self.settings.proxy)
            return
        if resolution.kind is JobKind.SMART and resolution.playlist is not None:
            playlist_panel = PlaylistPanel(
                resolution.playlist,
                preselect_cap=self.settings.playlist_batch_cap,
                parent=self,
            )
            if playlist_panel.exec() != PlaylistPanel.DialogCode.Accepted:
                return
            batch_option = playlist_panel.selected_option()
            for entry in playlist_panel.selected_entries():
                self.manager.add_smart_entry(
                    entry.url,
                    entry.title,
                    batch_option,
                    dest_dir=dest or None,
                    use_session=self.settings.use_browser_session,
                    session_browser=self.settings.session_browser,
                    headers=headers,
                )
        elif resolution.kind is JobKind.SMART and resolution.media is not None:
            quality_panel = QualityPanel(
                resolution.media, self, default_label=self.settings.video_default_quality
            )
            if quality_panel.exec() != QualityPanel.DialogCode.Accepted:
                cancel_download_prefetch(resolution.url, self.settings.proxy)
                return
            option = quality_panel.selected_option()
            if option is None:
                cancel_download_prefetch(resolution.url, self.settings.proxy)
                return
            self.manager.add_smart(
                resolution.url,
                resolution.media,
                option,
                dest_dir=dest or None,
                subtitles=quality_panel.subtitles_config(),
                trim=quality_panel.trim_range(),
                extras=quality_panel.extras_config(),
                use_session=self.settings.use_browser_session,
                session_browser=self.settings.session_browser,
                headers=headers,
            )
        elif resolution.kind is JobKind.HLS:
            variant = None
            if quality and resolution.variants:
                # F1.3: a label from the in-page panel picks the variant too.
                wanted = quality.strip().lower()
                variant = next(
                    (v for v in resolution.variants if v.label.lower() == wanted),
                    resolution.variants[0],
                )
            elif len(resolution.variants) > 1:
                labels = [v.description for v in resolution.variants]
                choice, accepted = QInputDialog.getItem(
                    self,
                    t("Stream quality"),
                    t("Pick a quality for this stream:"),
                    labels,
                    0,
                    False,
                )
                if not accepted:
                    return
                variant = resolution.variants[labels.index(choice)]
            elif resolution.variants:
                variant = resolution.variants[0]
            self.manager.add_hls(
                resolution.url,
                dest_dir=dest or None,
                title=page_title,
                variant=variant,
                headers=headers,
            )
        else:
            # F1.8 name fixer: prefer Content-Disposition, then rescue ugly
            # URL names (videoplayback.mp4 …) with the page title.
            probe = resolution.probe
            filename = (
                probe.filename
                if probe is not None and probe.filename
                else naming.improved_filename(
                    resolution.url,
                    page_title,
                    probe.content_type if probe is not None else None,
                )
            )
            # Any remaining sniffed stream URLs ride along as mirrors: if this
            # URL later dies for good, the download switches to the next one.
            self.manager.add_url(
                resolution.url,
                dest_dir=dest or None,
                filename=filename,
                headers=headers or None,
                mirrors=list(fallbacks) or None,
                probe=probe,
            )
        self.refresh()

    def _selected_job_ids(self) -> list[int]:
        rows = self.table.selectionModel().selectedRows()
        ids = [self._row_job_ids[i.row()] for i in rows if 0 <= i.row() < len(self._row_job_ids)]
        # Selection can be lost when the table rebuilds mid-download; fall back
        # to the ids we remembered so the toolbar buttons keep working.
        if not ids:
            ids = [job_id for job_id in self._selected_ids if job_id in self._row_job_ids]
        return ids

    def _on_selection_changed(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        self._selected_ids = {
            self._row_job_ids[i.row()] for i in rows if 0 <= i.row() < len(self._row_job_ids)
        }
        # Right-click must never pop the drawer. Qt updates the selection on
        # the right-button PRESS itself - before customContextMenuRequested
        # runs - so the suppress flag alone missed it; also gate on the live
        # mouse state.
        if self._suppress_drawer or (QApplication.mouseButtons() & Qt.MouseButton.RightButton):
            return
        # A single selection opens the detail drawer; multi/empty closes it.
        if len(self._selected_ids) == 1:
            (job_id,) = tuple(self._selected_ids)
            view = self._last_views.get(job_id)
            if view is not None:
                self._drawer.show_view(
                    view, self._smoothed_speed(view), self._spark_history.get(job_id)
                )
        else:
            self._drawer.hide()

    def _apply_optimistic_status(self, views: list[JobView], present: set[int]) -> list[JobView]:
        """Mask a still-``DOWNLOADING`` row with the status the user just asked
        for (Paused/Cancelled), until the worker actually settles - then the
        real status wins. Any other real status means the worker resolved, so
        the override is dropped immediately."""
        if not self._optimistic_status:
            return views
        self._optimistic_status = {k: v for k, v in self._optimistic_status.items() if k in present}
        masked: list[JobView] = []
        for view in views:
            target = self._optimistic_status.get(view.id)
            if target is not None and view.status is JobStatus.DOWNLOADING:
                masked.append(replace(view, status=target))
            else:
                self._optimistic_status.pop(view.id, None)
                masked.append(view)
        return masked

    def _pause_selected(self) -> None:
        for job_id in self._selected_job_ids():
            self._pause_job(job_id)
        self.refresh()

    def _resume_selected(self) -> None:
        for job_id in self._selected_job_ids():
            self._resume_job(job_id)
        self.refresh()

    def _pause_job(self, job_id: int) -> None:
        """Pause one download and show it immediately (shared by the toolbar
        button and the right-click menu)."""
        self.manager.pause(job_id)
        self._optimistic_status[job_id] = JobStatus.PAUSED

    def _resume_job(self, job_id: int) -> None:
        self.manager.resume(job_id)
        # resume() sets the row to QUEUED synchronously, so no optimism needed;
        # just make sure any lingering pause override is gone.
        self._optimistic_status.pop(job_id, None)

    def _cancel_selected(self) -> None:
        for job_id in self._selected_job_ids():
            self.manager.cancel(job_id)
            self._optimistic_status[job_id] = JobStatus.CANCELLED
        self.refresh()

    def _remove_selected(self) -> None:
        """Remove the selected downloads from the list, whatever state they are
        in - a running one is cancelled and dropped. Completed files stay on
        disk; a partial file is discarded with the job."""
        job_ids = self._selected_job_ids()
        for job_id in job_ids:
            self.manager.remove(job_id, force=True)
            self._removing.add(job_id)
        if job_ids:
            self.refresh()

    def _clear_completed(self) -> None:
        for view in list(self._last_views.values()):
            if view.status is JobStatus.COMPLETED:
                self.manager.remove(view.id)
        self.refresh()

    def _open_folder(self) -> None:
        if not reveal.open_folder(str(self.settings.download_dir)):
            self.statusBar().showMessage(t("Could not open the folder"), 3000)

    def _open_settings(self) -> None:
        from app.ui.settings_dialog import SettingsDialog

        with guard.single_flight(self._in_flight, "settings") as go:
            if not go:
                return
            dialog = SettingsDialog(self.settings, self)
            dialog.settings_reset.connect(self.manager.reload_settings)
            if dialog.exec() == SettingsDialog.DialogCode.Accepted:
                self.manager.reload_settings()
                app = QApplication.instance()
                if isinstance(app, QApplication):
                    theme.apply_theme(app, self.settings.theme)

    # -------------------------------------------------------- row actions

    def _view_for_row(self, row: int) -> JobView | None:
        if 0 <= row < len(self._row_job_ids):
            return self._last_views.get(self._row_job_ids[row])
        return None

    def _show_row_menu(self, position: QPoint) -> None:
        row = self.table.rowAt(position.y())
        view = self._view_for_row(row)
        if view is None:
            return
        # Select the row for the actions below, but never pop the details
        # drawer from a right-click - that is a left-click affordance.
        self._suppress_drawer = True
        try:
            self.table.selectRow(row)
        finally:
            self._suppress_drawer = False
        menu = QMenu(self)
        file_path = Path(view.dest_dir) / view.filename

        # Pause / Resume up top, one or the other depending on state, so the
        # most common control is the first thing under the cursor.
        can_pause = view.status in (JobStatus.DOWNLOADING, JobStatus.QUEUED)
        pause_action = menu.addAction(t("Pause"))
        pause_action.setVisible(can_pause)
        resume_label = t("Resume") if view.status is JobStatus.PAUSED else t("Start")
        resume_action = menu.addAction(resume_label)
        resume_action.setVisible(
            view.status in (JobStatus.PAUSED, JobStatus.FAILED, JobStatus.CANCELLED)
        )
        menu.addSeparator()

        open_file = menu.addAction(t("Open file"))
        open_file.setEnabled(view.status is JobStatus.COMPLETED and file_path.exists())
        open_folder = menu.addAction(t("Open folder"))
        rename_file = menu.addAction(t("Rename…"))
        rename_file.setEnabled(file_path.exists())
        copy_url = menu.addAction(t("Copy URL"))
        copy_path = menu.addAction(t("Copy path"))
        copy_path.setEnabled(file_path.exists())
        copy_magnet = menu.addAction(t("Copy magnet link"))
        copy_magnet.setVisible(view.kind is JobKind.TORRENT)
        redownload = menu.addAction(t("Download again"))
        # Convert to… - every FFmpeg target that makes sense for this file,
        # grouped Video / Audio / Image, plus the tuned GIF dialog.
        ffmpeg_path = find_ffmpeg(self.settings)
        convert_menu = menu.addMenu(t("Convert to"))
        convertible = (
            view.status is JobStatus.COMPLETED and file_path.exists() and ffmpeg_path is not None
        )
        sections = convert.targets_for(file_path) if convertible else {}
        convert_menu.setEnabled(bool(sections))
        if ffmpeg_path is None:
            convert_menu.setToolTip(t("Install FFmpeg in Settings, under Video Downloader"))
        convert_actions: dict[QAction, str] = {}
        to_gif: QAction | None = None
        if file_path.suffix.lower() in _VIDEO_SUFFIXES:
            to_gif = convert_menu.addAction(t("GIF…"))
            convert_menu.addSeparator()
        for section, formats in sections.items():
            section_action = convert_menu.addAction(section)
            section_action.setEnabled(False)  # a small group heading
            for fmt in formats:
                if f".{fmt}" == file_path.suffix.lower():
                    continue  # converting to itself is pointless
                action = convert_menu.addAction(fmt.upper())
                convert_actions[action] = fmt
            convert_menu.addSeparator()
        limit_speed = menu.addAction(t("Limit speed…"))
        limit_speed.setEnabled(view.status is not JobStatus.COMPLETED)
        set_connections = menu.addAction(t("Connections…"))
        set_connections.setEnabled(view.status is not JobStatus.COMPLETED)

        done = view.status is JobStatus.COMPLETED and file_path.exists()
        copy_hash = menu.addAction(t("Copy SHA-256"))
        copy_hash.setEnabled(done)
        verify_hash = menu.addAction(t("Verify checksum…"))
        verify_hash.setEnabled(done)
        security_action = menu.addAction(t("Security check…"))
        security_action.setEnabled(done)
        inspect_action = menu.addAction(t("Inspect…"))
        inspect_action.setEnabled(view.kind in (JobKind.DIRECT, JobKind.SMART, JobKind.HLS))
        extract_here = menu.addAction(t("Extract here"))
        extract_here.setEnabled(done and archive.is_archive(file_path))
        preview_archive = menu.addAction(t("Preview archive…"))
        preview_archive.setEnabled(done and archive.is_archive(file_path))

        move_menu = menu.addMenu(t("Move to"))
        favorites = self.settings.favorite_folders
        move_menu.setEnabled(done and bool(favorites))
        if not favorites:
            move_menu.setToolTip(t("Add favorite folders in Settings"))
        move_actions = {move_menu.addAction(folder): folder for folder in favorites}
        tags_action = menu.addAction(t("Tags && notes…"))

        queue_menu = menu.addMenu(t("Queue"))
        default_queue_action = queue_menu.addAction(t("Default"))
        default_queue_action.setCheckable(True)
        default_queue_action.setChecked(view.queue_id is None)
        queue_actions: dict[QAction, int] = {}
        for named_queue in self.manager.list_queues():
            queue_action = queue_menu.addAction(named_queue.name)
            queue_action.setCheckable(True)
            queue_action.setChecked(view.queue_id == named_queue.id)
            queue_actions[queue_action] = named_queue.id
        start_after = menu.addAction(t("Start after…"))
        start_after.setEnabled(view.status in (JobStatus.QUEUED, JobStatus.PAUSED))
        start_at = menu.addAction(t("Start at…"))
        start_at.setEnabled(view.status in (JobStatus.QUEUED, JobStatus.PAUSED))

        pending = view.status in (JobStatus.QUEUED, JobStatus.PAUSED)
        queue_menu = menu.addMenu(t("Move in queue"))
        queue_menu.setEnabled(pending)
        move_top = queue_menu.addAction(t("To top"))
        move_up = queue_menu.addAction(t("Up"))
        move_down = queue_menu.addAction(t("Down"))
        move_bottom = queue_menu.addAction(t("To bottom"))

        menu.addSeparator()
        # Force remove: works whatever the state, mid-download included.
        remove = menu.addAction(t("Remove from list"))
        chosen = menu.exec(self.table.viewport().mapToGlobal(position))
        if chosen is pause_action:
            self._pause_job(view.id)
            self.refresh()
        elif chosen is resume_action:
            self._resume_job(view.id)
            self.refresh()
        elif chosen is open_file:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(file_path)))
        elif chosen is open_folder:
            if not reveal.open_folder(file_path):
                self.statusBar().showMessage(t("Could not open the folder"), 3000)
        elif chosen is rename_file:
            self._rename_view(view)
        elif chosen is copy_url:
            self._copy_view_url(view)
        elif chosen is copy_path:
            self._copy_view_path(view)
        elif chosen is copy_magnet:
            self._copy_magnet(view)
        elif chosen is redownload:
            self.begin_add_url(view.url, allow_duplicate=True)
        elif to_gif is not None and chosen is to_gif and ffmpeg_path is not None:
            GifDialog(ffmpeg_path, file_path, self).exec()
        elif chosen in convert_actions and ffmpeg_path is not None:
            self._convert_file(file_path, convert_actions[chosen], ffmpeg_path)
        elif chosen is limit_speed:
            self._limit_speed(view)
        elif chosen is set_connections:
            self._set_connections(view)
        elif chosen is copy_hash:
            self._copy_hash(file_path)
        elif chosen is verify_hash:
            self._verify_hash(file_path)
        elif chosen is security_action:
            SecurityDialog(file_path, view.url, self.settings, self).exec()
        elif chosen is inspect_action:
            self._inspect_job(view, file_path)
        elif chosen is extract_here:
            self._extract(file_path)
        elif chosen is preview_archive:
            self._preview_archive(file_path)
        elif chosen in move_actions:
            self._move_to(view, move_actions[chosen])
        elif chosen is tags_action:
            self._edit_tags(view)
        elif chosen is default_queue_action:
            self.manager.set_job_queue(view.id, None)
        elif chosen in queue_actions:
            self.manager.set_job_queue(view.id, queue_actions[chosen])
        elif chosen is start_after:
            self._pick_start_after(view)
        elif chosen is start_at:
            self._pick_start_at(view)
        elif chosen is move_top:
            self.manager.move_to_top(view.id)
        elif chosen is move_up:
            self.manager.move_up(view.id)
        elif chosen is move_down:
            self.manager.move_down(view.id)
        elif chosen is move_bottom:
            self.manager.move_to_bottom(view.id)
        elif chosen is remove:
            self.manager.remove(view.id, force=True)
            self._removing.add(view.id)
            self.refresh()

    def _busy_begin(self) -> None:
        self._busy_ops += 1
        self._busy_bar.show()
        self._busy_bar.set_indeterminate(True)
        self._busy_count.setText(
            t("{count} tasks", count=self._busy_ops) if self._busy_ops > 1 else ""
        )
        self._busy_count.setVisible(self._busy_ops > 1)

    def _busy_end(self) -> None:
        self._busy_ops = max(0, self._busy_ops - 1)
        if self._busy_ops == 0:
            self._busy_bar.set_indeterminate(False)
            self._busy_bar.hide()
            self._busy_count.hide()
        else:
            self._busy_count.setText(
                t("{count} tasks", count=self._busy_ops) if self._busy_ops > 1 else ""
            )
            self._busy_count.setVisible(self._busy_ops > 1)

    def _run_file_op(
        self,
        work: Callable[[], object],
        on_done: Callable[[object], None],
        on_error: Callable[[object], None] | None = None,
    ) -> None:
        thread = work_threads.FileOpThread(work, self)
        self._file_ops.add(thread)
        self._busy_begin()

        def deliver(result: object, error: object) -> None:
            self._busy_end()
            if error is not None:
                if on_error is not None:
                    on_error(error)
                else:
                    QMessageBox.warning(self, "GrabLine", str(error))
            else:
                on_done(result)

        def cleanup() -> None:
            # Only after run() fully returned: safe to let the object go.
            self._file_ops.discard(thread)
            thread.deleteLater()

        thread.done.connect(deliver)
        thread.finished.connect(cleanup)
        thread.start()

    def _convert_file(self, path: Path, target_format: str, ffmpeg_path: str) -> None:
        """Convert a finished file with FFmpeg, silently in the background."""
        self.statusBar().showMessage(
            t("Converting {name} to {format} …", name=path.name, format=target_format.upper())
        )

        def done(result: object) -> None:
            self.statusBar().showMessage(t("Converted: {name}", name=Path(str(result)).name), 8000)

        self._run_file_op(lambda: convert.convert(ffmpeg_path, path, target_format), done)

    def _copy_hash(self, path: Path) -> None:
        self.statusBar().showMessage(t("Hashing {name} …", name=path.name))

        def done(result: object) -> None:
            QGuiApplication.clipboard().setText(str(result))
            self.statusBar().showMessage(t("SHA-256 copied for {name}", name=path.name), 6000)

        self._run_file_op(lambda: verify.hash_file(path), done)

    def _verify_hash(self, path: Path) -> None:
        expected, accepted = QInputDialog.getText(
            self,
            t("Verify checksum"),
            t(
                "Paste the expected MD5 / SHA-1 / SHA-256 / SHA-512 / CRC32 for {name}:",
                name=path.name,
            ),
        )
        if not (accepted and expected.strip()):
            return
        self.statusBar().showMessage(t("Verifying {name} …", name=path.name))

        def done(result: object) -> None:
            if result:
                QMessageBox.information(
                    self, "GrabLine", t("{name} matches the checksum.", name=path.name)
                )
            else:
                QMessageBox.warning(
                    self, "GrabLine", t("{name} does NOT match the checksum.", name=path.name)
                )

        self._run_file_op(lambda: verify.verify_file(path, expected.strip()), done)

    def _archive_work(
        self,
        path: Path,
        members: list[str] | None = None,
        passwords: tuple[str, ...] = (),
        *,
        scan: bool = True,
    ) -> Callable[[], object]:
        """Extraction work for a background thread: the optional virus scan
        first, then the extraction itself. A detection does not block - it
        raises _ScanFlagged so the caller can ask the user whether to go on."""

        def work() -> object:
            pref = self.settings.scanner_pref
            if (
                scan
                and self.settings.scan_before_extract
                and virusscan.find_scanner(pref) is not None
            ):
                result = virusscan.scan(path, pref)
                if not result.clean:
                    raise _ScanFlagged(result.scanner, result.detail)
            dest = path.parent / path.stem if self.settings.extract_to_subfolder else None
            extracted = archive.extract(path, dest, passwords=passwords, members=members)
            if self.settings.delete_archive_after_extract and members is None:
                # Only after a full, clean extraction - never on partial picks.
                path.unlink(missing_ok=True)
            return extracted

        return work

    def _extract(
        self,
        path: Path,
        members: list[str] | None = None,
        new_password: str | None = None,
        *,
        scan: bool = True,
    ) -> None:
        passwords = self.settings.archive_passwords
        if new_password:
            passwords = (new_password, *passwords)
        self.statusBar().showMessage(t("Extracting {name} …", name=path.name))

        def done(result: object) -> None:
            if new_password:
                # Remember what worked - next time it's tried automatically.
                self.settings.archive_passwords = (
                    new_password,
                    *self.settings.archive_passwords,
                )
            self.statusBar().showMessage(
                t("Extracted to {name}", name=Path(str(result)).name), 6000
            )

        def failed(error: object) -> None:
            if isinstance(error, _ScanFlagged):
                detail = f"\n\n{error.detail}" if error.detail else ""
                answer = QMessageBox.warning(
                    self,
                    "GrabLine",
                    t(
                        "{scanner} flagged {name}.{detail}\n\n"
                        "Antivirus false positives are common. Extract it anyway?",
                        scanner=error.scanner,
                        name=path.name,
                        detail=detail,
                    ),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer == QMessageBox.StandardButton.Yes:
                    self._extract(path, members, new_password, scan=False)
                return
            if isinstance(error, archive.PasswordRequired):
                password, accepted = QInputDialog.getText(
                    self,
                    t("Archive password"),
                    t("{name} is password-protected. Password:", name=path.name),
                    QLineEdit.EchoMode.Password,
                )
                if accepted and password:
                    self._extract(path, members, new_password=password)
                return
            QMessageBox.warning(self, "GrabLine", str(error))

        self._run_file_op(self._archive_work(path, members, passwords, scan=scan), done, failed)

    def _preview_archive(self, path: Path) -> None:
        self.statusBar().showMessage(t("Reading {name} …", name=path.name))

        def opened(result: object) -> None:
            self.statusBar().clearMessage()
            entries = cast("tuple[archive.ArchiveEntry, ...]", result)
            dialog = ArchiveDialog(path.name, entries, self)
            if dialog.exec() == ArchiveDialog.DialogCode.Accepted:
                self._extract(path, members=dialog.selected_members())

        self._run_file_op(lambda: archive.list_entries(path), opened)

    def _move_to(self, view: JobView, folder: str) -> None:
        self.statusBar().showMessage(t("Moving {name} …", name=view.filename))

        def done(result: object) -> None:
            self.statusBar().showMessage(t("Moved to {result}", result=result), 6000)
            self.refresh()

        self._run_file_op(lambda: self.manager.move_job_file(view.id, folder), done)

    def _edit_tags(self, view: JobView) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(t("Tags & notes: {name}", name=view.display_name))
        dialog.setMinimumWidth(420)
        form = QFormLayout(dialog)
        tags_edit = QLineEdit(view.tags)
        tags_edit.setPlaceholderText(t("comma, separated, labels"))
        form.addRow(t("Tags:"), tags_edit)
        notes_edit = QPlainTextEdit(view.notes)
        notes_edit.setPlaceholderText(t("Anything worth remembering about this download."))
        form.addRow(t("Notes:"), notes_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.manager.set_job_tags(view.id, tags_edit.text())
            self.manager.set_job_notes(view.id, notes_edit.toPlainText())
            self.refresh()

    def _find_duplicates(self) -> None:
        """Hash-compare every completed download and offer to delete the
        extra byte-identical copies (keeping one of each)."""
        owners: dict[Path, int] = {}
        for view in self._last_views.values():
            if view.status is JobStatus.COMPLETED:
                owners[Path(view.dest_dir) / view.filename] = view.id
        if not owners:
            QMessageBox.information(self, "GrabLine", t("No completed downloads to compare."))
            return
        self.statusBar().showMessage(t("Comparing files …"))

        def done(result: object) -> None:
            self.statusBar().showMessage(t("Ready"))
            groups = cast("list[list[Path]]", result)
            if not groups:
                QMessageBox.information(self, "GrabLine", t("No duplicate files found."))
                return
            dialog = DupesDialog(groups, self)
            if dialog.exec() != DupesDialog.DialogCode.Accepted:
                return
            doomed = dialog.selected_paths()
            if not doomed:
                return
            answer = QMessageBox.warning(
                self,
                "GrabLine",
                t(
                    "Permanently delete {count} duplicate file(s)? One copy of each is kept.",
                    count=len(doomed),
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            for path in doomed:
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    QMessageBox.warning(
                        self,
                        "GrabLine",
                        t("Could not delete {name}: {exc}", name=path.name, exc=exc),
                    )
                    continue
                job_id = owners.get(path)
                if job_id is not None:
                    self.manager.remove(job_id)
            self.statusBar().showMessage(
                t("Removed {count} duplicate file(s)", count=len(doomed)), 6000
            )
            self.refresh()

        self._run_file_op(lambda: dupes.find_duplicates(list(owners)), done)

    # ------------------------------------------------------------ dashboard

    def _open_dashboard(self) -> None:
        DashboardDialog(self.manager, self).exec()

    # ------------------------------------------------------------- security

    def _advisory_scan(self, view: JobView, file_path: Path) -> None:
        """Opt-in post-download check. Runs the report off-thread; only speaks
        up if something is worth a look - never blocks, never deletes."""
        if not file_path.exists():
            return
        allowed = self.settings.scan_extensions
        if allowed:
            wanted = {e.strip().lower().lstrip(".") for e in allowed.split(",") if e.strip()}
            if file_path.suffix.lower().lstrip(".") not in wanted:
                return  # the user scoped scanning to specific types
        key = self.settings.virustotal_key
        proxy = self.settings.proxy
        pref = self.settings.scanner_pref

        def work() -> object:
            return security.check_file(
                file_path, url=view.url, virustotal_key=key, proxy=proxy, scanner_pref=pref
            )

        def done(result: object) -> None:
            report = cast("security.SecurityReport", result)
            if report.level is security.Risk.WARNING:
                # Only a real warning interrupts; the file is already saved.
                findings = "\n".join(f"• {f}" for f in report.findings)
                answer = QMessageBox.warning(
                    self,
                    t("Security"),
                    t(
                        "{name}\n\n{findings}\n\nThe file is saved and usable. "
                        "This is advice only. Open the full report?",
                        name=view.display_name,
                        findings=findings,
                    ),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer == QMessageBox.StandardButton.Yes:
                    SecurityDialog(file_path, view.url, self.settings, self).exec()
            elif report.level is security.Risk.CAUTION:
                self.statusBar().showMessage(
                    t("{name}: {finding}", name=view.display_name, finding=report.findings[0]),
                    8000,
                )

        self._run_file_op(work, done, lambda _e: None)  # a scan error is silent

    # ------------------------------------------------------------ inspector

    def _inspect_url_prompt(self) -> None:
        url, accepted = QInputDialog.getText(self, t("Inspect URL"), t("URL to inspect:"))
        if accepted and url.strip():
            InspectorDialog(url.strip(), proxy=self.settings.proxy, parent=self).exec()

    def _inspect_job(self, view: JobView, file_path: Path) -> None:
        mirrors = ()
        job = self.manager.db.get_job(view.id)
        if job is not None:
            mirrors = tuple(job.options.get("mirrors") or ())
        checksum_work = None
        if view.status is JobStatus.COMPLETED and file_path.exists():
            checksum_work = lambda: verify.hash_file(file_path)  # noqa: E731
        InspectorDialog(
            view.url,
            mirrors=mirrors,
            checksum_work=checksum_work,
            proxy=self.settings.proxy,
            parent=self,
        ).exec()

    # --------------------------------------------------------------- queues

    def _open_queue_manager(self) -> None:
        QueueManagerDialog(self.manager, self).exec()
        self.refresh()

    def _pick_start_after(self, view: JobView) -> None:
        """Job dependency: hold this download until a chosen one finishes."""
        others = [
            v
            for v in self._last_views.values()
            if v.id != view.id and v.status is not JobStatus.COMPLETED
        ]
        if not others:
            QMessageBox.information(
                self, "GrabLine", t("No other unfinished downloads to wait for.")
            )
            return
        labels = [t("(nothing, start normally)")] + [v.display_name for v in others]
        choice, accepted = QInputDialog.getItem(
            self,
            t("Start after"),
            t("Start {name} only after:", name=view.display_name),
            labels,
            editable=False,
        )
        if not accepted:
            return
        index = labels.index(choice)
        self.manager.set_job_after(view.id, None if index == 0 else others[index - 1].id)

    def _pick_start_at(self, view: JobView) -> None:
        """Download later: hold this download until a chosen date and time."""
        from PySide6.QtCore import QDateTime
        from PySide6.QtWidgets import QDateTimeEdit

        dialog = QDialog(self)
        dialog.setWindowTitle(t("Start at: {name}", name=view.display_name))
        form = QFormLayout(dialog)
        when_edit = QDateTimeEdit(QDateTime.currentDateTime().addSecs(3600))
        when_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        when_edit.setCalendarPopup(True)
        form.addRow(t("Start no earlier than:"), when_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Reset
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Reset).setText(t("Start normally"))
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        buttons.button(QDialogButtonBox.StandardButton.Reset).clicked.connect(
            lambda: dialog.done(2)
        )
        form.addRow(buttons)
        result = dialog.exec()
        if result == QDialog.DialogCode.Accepted:
            from datetime import datetime as _datetime

            when = cast("_datetime", when_edit.dateTime().toPython())
            self.manager.set_job_start_at(view.id, when)
            self.statusBar().showMessage(
                t(
                    "{name} starts {when}",
                    name=view.display_name,
                    when=when_edit.dateTime().toString("yyyy-MM-dd HH:mm"),
                ),
                6000,
            )
        elif result == 2:
            self.manager.set_job_start_at(view.id, None)

    # ---------------------------------------------------------------- cloud

    def _add_cloud(self) -> None:
        url = prompt_cloud_url(self)
        if url:
            self.add_cloud_source(url)

    def add_cloud_source(self, url: str) -> None:
        """Queue a cloud protocol download. A URL ending in '/' is treated as a
        folder: its files are listed and offered in a picker."""
        if url.rstrip().endswith("/"):
            self.statusBar().showMessage(t("Listing remote folder …"))

            def listed(result: object) -> None:
                self.statusBar().clearMessage()
                files = cast("list[cloud_engine.RemoteFile]", result)
                if not files:
                    QMessageBox.information(self, "GrabLine", t("That folder is empty."))
                    return
                dialog = CloudFolderDialog(url, files, self)
                if dialog.exec() != CloudFolderDialog.DialogCode.Accepted:
                    return
                for file_url in dialog.selected_urls():
                    self.manager.add_cloud(file_url)
                self.statusBar().showMessage(
                    t("Queued {count} file(s)", count=len(dialog.selected_urls())), 5000
                )
                self.refresh()

            self._run_file_op(lambda: self.manager.list_cloud_folder(url), listed)
            return
        self.manager.add_cloud(url)
        self.statusBar().showMessage(t("Queued cloud download"), 5000)
        self.refresh()

    # ------------------------------------------------------------- torrents

    def _copy_magnet(self, view: JobView) -> None:
        if view.url.lower().startswith("magnet:"):
            magnet = view.url
            if self.clipboard_suppressor is not None:
                self.clipboard_suppressor(magnet)
            QGuiApplication.clipboard().setText(magnet)
            self.statusBar().showMessage(t("Magnet link copied"), 4000)
            return

        def done(result: object) -> None:
            magnet = str(result)
            if self.clipboard_suppressor is not None:
                self.clipboard_suppressor(magnet)
            QGuiApplication.clipboard().setText(magnet)
            self.statusBar().showMessage(t("Magnet link copied"), 4000)

        self._run_file_op(
            lambda: torrent_engine.magnet_from_torrent(
                torrent_engine.fetch_torrent_bytes(view.url, proxy=self.settings.proxy)
            ),
            done,
        )

    def _add_torrent_file(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self, t("Add torrent"), "", "Torrents (*.torrent);;All files (*)"
        )
        if chosen:
            self.add_torrent_source(chosen)

    def add_torrent_source(self, source: str) -> None:
        """Open the add-torrent dialog for a magnet link, a local .torrent
        path, or an http(s) .torrent URL - the one entry point used by the
        menu, the resolver, drag-and-drop, and 'open with GrabLine'."""
        default_dir = self.settings.torrent_dir or self.settings.download_dir
        if source.lower().startswith("magnet:"):
            name = torrent_engine.magnet_display_name(source) or t("Magnet link")
            self._open_add_torrent(source, name, None, default_dir)
            return
        self.statusBar().showMessage(t("Reading torrent …"))

        def loaded(result: object) -> None:
            self.statusBar().clearMessage()
            meta = cast("torrent_engine.TorrentMeta", result)
            self._open_add_torrent(source, meta.name, meta, default_dir)

        self._run_file_op(
            lambda: torrent_engine.parse_torrent(
                torrent_engine.fetch_torrent_bytes(source, proxy=self.settings.proxy)
            ),
            loaded,
        )

    def _open_add_torrent(self, source: str, name: str, meta: object, default_dir: Path) -> None:
        dialog = AddTorrentDialog(
            name,
            cast("torrent_engine.TorrentMeta | None", meta),
            default_dir,
            sequential_default=self.settings.torrent_sequential,
            parent=self,
        )
        if dialog.exec() != AddTorrentDialog.DialogCode.Accepted:
            return
        self.manager.add_torrent(
            source, dest_dir=dialog.dest_dir() or default_dir, name=name, options=dialog.options()
        )
        self.statusBar().showMessage(t("Queued torrent {name}", name=name), 5000)
        self.refresh()

    def _create_torrent(self) -> None:
        dialog = CreateTorrentDialog(self)
        if self.settings.torrent_trackers:  # Settings → Torrent: default trackers
            dialog.trackers_edit.setPlainText("\n".join(self.settings.torrent_trackers))
        if dialog.exec() != CreateTorrentDialog.DialogCode.Accepted:
            return
        source = dialog.source()
        target, _ = QFileDialog.getSaveFileName(
            self, t("Save torrent as"), f"{source.name}.torrent", "Torrents (*.torrent)"
        )
        if not target:
            return
        self.statusBar().showMessage(t("Hashing pieces …"))

        def work() -> object:
            data = torrent_engine.create_torrent_file(
                source,
                trackers=dialog.trackers(),
                web_seeds=dialog.web_seeds(),
                comment=dialog.comment(),
                private=dialog.private(),
            )
            Path(target).write_bytes(data)
            return target

        def done(result: object) -> None:
            self.statusBar().showMessage(t("Torrent created: {result}", result=result), 8000)

        self._run_file_op(work, done)

    def _search_torrents(self) -> None:
        template = self.settings.torrent_search_url
        if "%s" not in template:
            QMessageBox.information(
                self,
                "GrabLine",
                t(
                    "Set a search URL first, in Settings under Torrent. For example:\n"
                    "https://example.com/search?q=%s"
                ),
            )
            return
        query, accepted = QInputDialog.getText(self, t("Search torrents"), t("Search for:"))
        if accepted and query.strip():
            from urllib.parse import quote

            QDesktopServices.openUrl(QUrl(template.replace("%s", quote(query.strip()))))

    def _poll_rss(self) -> None:
        """Check the RSS feeds and queue new matching torrent items."""
        if self._shutting_down:  # the 15s singleShot outlives timer.stop()
            return
        feeds = self.settings.rss_feeds
        if not feeds:
            return
        seen = set(self.settings.rss_seen)
        proxy = self.settings.proxy

        def work() -> object:
            found: list[tuple[str, str]] = []  # (guid, link)
            for line in feeds:
                url, needle = rss.parse_feed_line(line)
                try:
                    items = rss.fetch_feed(url, proxy=proxy)
                except DownloadError:
                    continue  # a dead feed shouldn't spam errors every poll
                for item in rss.matching_items(items, needle):
                    if item.guid not in seen and torrent_engine.is_torrent_source(item.link):
                        found.append((item.guid, item.link))
            return found

        def done(result: object) -> None:
            found = cast("list[tuple[str, str]]", result)
            if not found:
                return
            for guid, link in found:
                self.manager.add_torrent(link)
                seen.add(guid)
            self.settings.rss_seen = list(seen)
            self.statusBar().showMessage(
                t("RSS: queued {count} torrent(s)", count=len(found)), 6000
            )
            self.refresh()

        self._run_file_op(work, done, lambda _e: None)  # quiet - it's a background poll

    def _set_connections(self, view: JobView) -> None:
        connections, accepted = QInputDialog.getInt(
            self,
            t("Connections"),
            t(
                "Parallel connections for this download\n"
                "(0 = automatic; beyond ~32 servers often throttle)\n{name}",
                name=view.display_name,
            ),
            value=0,
            minValue=0,
            maxValue=MAX_CONNECTIONS,
        )
        if accepted:
            self.manager.set_job_connections(view.id, connections)

    def _limit_speed(self, view: JobView) -> None:
        kbps, accepted = QInputDialog.getInt(
            self,
            t("Limit speed"),
            t(
                "Max speed for this download in KB/s\n(0 = no limit)\n{name}",
                name=view.display_name,
            ),
            value=view.speed_limit_kbps,
            minValue=0,
            maxValue=1_000_000,
            step=256,
        )
        if accepted:
            self.manager.set_job_speed(view.id, kbps)

    # ------------------------------------------------------------- refresh

    def refresh(self) -> None:
        if self._shutting_down:
            return
        if not self.isVisible():
            # Hidden to the tray. Nothing here is stored - every line below
            # writes into widgets nobody is looking at - and the manager keeps
            # downloading regardless. showEvent refreshes on the way back.
            return
        views = self.manager.snapshot()
        # Optimistic removal: a force-removed running job stays in the DB until
        # its worker actually stops - hide it from the list right away.
        present = {view.id for view in views}
        self._removing &= present
        if self._removing:
            views = [view for view in views if view.id not in self._removing]
        views = self._apply_optimistic_status(views, present)
        self._detect_transitions(views)
        self._measure_speeds(views)
        self._update_speed_line(views)
        self._last_views = {view.id: view for view in views}
        ids = [view.id for view in views]
        if ids != self._row_job_ids:
            self._rebuild_rows(views)
            self._restore_selection()
        for row, view in enumerate(views):
            # A settled row has nothing to redraw: its view is frozen and
            # compares equal, and only a downloading row's speed and ETA move
            # on their own. With a long history that skip is the difference
            # between touching a handful of rows a tick and all of them.
            if view.status is not JobStatus.DOWNLOADING and self._rendered.get(view.id) == view:
                continue
            self._update_row(row, view)
            self._rendered[view.id] = view
        self._update_filter_counts(views)
        self._update_status_info(views)
        self._apply_filter()
        self._empty_label.setGeometry(self.table.viewport().rect())
        self._empty_label.setVisible(not views)
        self._update_toggle_button()
        # Keep an open detail drawer live.
        drawer_id = self._drawer.current_id()
        if self._drawer.isVisible() and drawer_id is not None:
            live = self._last_views.get(drawer_id)
            if live is not None:
                self._drawer.show_view(live, self._smoothed_speed(live))
            else:
                self._drawer.hide()

    def _update_filter_counts(self, views: list[JobView]) -> None:
        counts = {
            "all": len(views),
            "active": sum(
                1
                for v in views
                if v.status in (JobStatus.DOWNLOADING, JobStatus.QUEUED, JobStatus.PAUSED)
            ),
            "completed": sum(1 for v in views if v.status is JobStatus.COMPLETED),
            "failed": sum(1 for v in views if v.status in (JobStatus.FAILED, JobStatus.CANCELLED)),
        }
        labels = {
            "all": t("All"),
            "active": t("Active"),
            "completed": t("Completed"),
            "failed": t("Failed"),
        }
        for key, btn in self._filter_buttons.items():
            btn.setText(f"{labels[key]}  {counts[key]}")
        self._clear_completed_btn.setVisible(counts["completed"] > 0)

    def _update_status_info(self, views: list[JobView]) -> None:
        active = sum(1 for v in views if v.status is JobStatus.DOWNLOADING)
        done = sum(1 for v in views if v.status is JobStatus.COMPLETED)
        counts = t(
            "{total} items · {active} active · {done} completed",
            total=len(views),
            active=active,
            done=done,
        )
        self._status_info.setText(f"{counts}     GrabLine v{__version__} · " + t("No telemetry"))

    def _restore_selection(self) -> None:
        """Re-select the remembered jobs after a rebuild so the toolbar keeps
        acting on what the user picked (multi-selection preserved)."""
        present = [job_id for job_id in self._selected_ids if job_id in self._row_job_ids]
        if not present:
            return
        model = self.table.model()
        selection = QItemSelection()
        for job_id in present:
            row = self._row_job_ids.index(job_id)
            selection.select(model.index(row, 0), model.index(row, len(_COLUMNS) - 1))
        self.table.blockSignals(True)
        self.table.selectionModel().select(
            selection, QItemSelectionModel.SelectionFlag.ClearAndSelect
        )
        self.table.blockSignals(False)

    def _update_speed_line(self, views: list[JobView]) -> None:
        """The toolbar total: the sum of the per-download speeds already
        measured this poll. Summing settled rates (rather than differencing a
        total that drops every time a download finishes) keeps it steady."""
        total = sum(self._speeds.get(v.id, 0.0) for v in views if v.status is JobStatus.DOWNLOADING)
        self.speed_line.push(total)
        self._total_speed.setText(motion.fmt_speed(total))

    def _detect_transitions(self, views: list[JobView]) -> None:
        """Notify on newly finished downloads and when the queue drains."""
        active_now = False
        for view in views:
            previous = self._prev_status.get(view.id)
            if view.status in (JobStatus.DOWNLOADING, JobStatus.QUEUED):
                active_now = True
            if (
                previous is not None
                and previous is not JobStatus.FAILED
                and view.status is JobStatus.FAILED
            ):
                self.job_failed.emit(view.display_name, view.error or t("download failed"))
            just_completed = (
                previous is not None
                and previous is not JobStatus.COMPLETED
                and view.status is JobStatus.COMPLETED
            )
            if just_completed:
                file_path = Path(view.dest_dir) / view.filename
                self.job_completed.emit(view.display_name, str(file_path))
                if self.settings.scan_downloads and view.id not in self._scanned:
                    self._scanned.add(view.id)
                    self._advisory_scan(view, file_path)
                if self.settings.auto_open_folder:
                    QDesktopServices.openUrl(QUrl.fromLocalFile(view.dest_dir))
                if (
                    self.settings.auto_extract
                    and view.id not in self._auto_extracted
                    and archive.is_archive(file_path)
                    and file_path.exists()
                ):
                    self._auto_extracted.add(view.id)
                    self.statusBar().showMessage(t("Extracting {name} …", name=file_path.name))

                    # Failures stay in the status bar - a modal mid-queue would
                    # interrupt; the row menu's Preview archive… can prompt.
                    def extract_failed(error: object, name: str = file_path.name) -> None:
                        self.statusBar().showMessage(
                            t("Did not extract {name}: {error}", name=name, error=error), 10000
                        )

                    self._run_file_op(
                        self._archive_work(file_path, passwords=self.settings.archive_passwords),
                        lambda r: self.statusBar().showMessage(
                            t("Extracted {name}", name=Path(str(r)).name), 6000
                        ),
                        extract_failed,
                    )
        if self._was_active and not active_now:
            self.queue_drained.emit()
        self._was_active = active_now
        self._prev_status = {view.id: view.status for view in views}

    def _apply_filter(self) -> None:
        needle = self.search_box.text().strip().lower()
        statuses = _FILTER_STATUSES.get(self._filter, ())
        # The poll calls this every 500ms, but what it decides only depends on
        # the query, the tab, and each row's id/status. Re-running the string
        # matching over every row when none of those moved is pure waste.
        signature = (
            needle,
            self._filter,
            tuple(
                (job_id, self._last_views[job_id].status)
                for job_id in self._row_job_ids
                if job_id in self._last_views
            ),
        )
        if signature == self._filter_signature:
            return
        self._filter_signature = signature
        for row in range(self.table.rowCount()):
            view = self._view_for_row(row)
            matches_search = not needle or (
                view is not None
                and (
                    needle in view.display_name.lower()
                    or needle in view.url.lower()
                    or needle in view.tags.lower()
                    or needle in view.notes.lower()
                )
            )
            matches_tab = not statuses or (view is not None and view.status in statuses)
            self.table.setRowHidden(row, not (matches_search and matches_tab))

    def _release_row_widgets(self) -> None:
        """Drop the old rows' per-cell widgets, giving back the shared ticker
        first - a progress bar destroyed mid-animation cannot do it itself."""
        for bar in self._progress_bars.values():
            bar.release()
        self._progress_bars.clear()
        self._pills.clear()
        self._row_icons.clear()
        self._rendered.clear()  # new cells: everything must be drawn again

    def _rebuild_rows(self, views: list[JobView]) -> None:
        self._release_row_widgets()
        # setRowCount can drop rows, and that emits selectionChanged while
        # _row_job_ids still describes the old layout - the selection handler
        # would then record ids for the wrong jobs (the toolbar acting on the
        # wrong download) and hide the drawer. Map first, and stay quiet.
        self._row_job_ids = [view.id for view in views]
        blocker = QSignalBlocker(self.table.selectionModel())
        self.table.setRowCount(len(views))
        del blocker
        for row, view in enumerate(views):
            # icon
            icon_item = QTableWidgetItem()
            icon_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, _COL_ICON, icon_item)
            right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            num_font = design.numeric_font(self.table.font())
            for col in (_COL_NAME, _COL_SIZE, _COL_SPEED, _COL_ETA):
                item = QTableWidgetItem("")
                if col != _COL_NAME:  # numbers: right-aligned, tabular digits
                    item.setTextAlignment(right)
                    item.setFont(num_font)
                self.table.setItem(row, col, item)
            bar = motion.SmoothProgressBar()
            self.table.setCellWidget(row, _COL_PROGRESS, self._pad(bar))
            self._progress_bars[view.id] = bar
            pill = components.StatusPill(view.status.value)
            self.table.setCellWidget(row, _COL_STATUS, self._pad(pill))
            self._pills[view.id] = pill

    @staticmethod
    def _pad(widget: QWidget) -> QWidget:
        """Wrap a cell widget with a little horizontal padding so it doesn't
        touch the gridless cell edges. The holder is transparent (see the
        #BareContainer rule) so row hover/selection paint straight through."""
        holder = QWidget()
        holder.setObjectName("BareContainer")
        lay = QHBoxLayout(holder)
        lay.setContentsMargins(6, 0, 6, 0)
        lay.addWidget(widget)
        return holder

    def _cell(self, row: int, column: int) -> QTableWidgetItem:
        item = self.table.item(row, column)
        assert item is not None  # _rebuild_rows creates every cell
        return item

    def _update_row(self, row: int, view: JobView) -> None:
        p = theme.current()
        # type icon. QIcon has no equality operator, so setIcon always counts
        # as a change and repaints the cell - twice a second, for every row,
        # forever. Only touch it when the icon it would draw actually differs.
        ext = view.filename
        name = icons.type_icon_name(
            view.kind.value if view.kind.value in ("torrent", "cloud") else ext
        )
        color = self._type_color(view)
        noted = bool(view.tags or view.notes)
        icon_key = (name, color, noted, p.text3)
        if self._row_icons.get(view.id) != icon_key:
            self._row_icons[view.id] = icon_key
            self._cell(row, _COL_ICON).setIcon(icons.svg_icon(name, color))
            # A small note icon next to the name marks a download that carries
            # tags or notes, so they're discoverable at a glance (not only on
            # hover) - a tinted glyph, never an emoji.
            note_icon = icons.svg_icon("note", p.text3) if noted else QIcon()
            self._cell(row, _COL_NAME).setIcon(note_icon)

        name_item = self._cell(row, _COL_NAME)
        name_item.setText(view.display_name)
        name_item.setToolTip(
            (t("Tags: {tags}\n", tags=view.tags) if view.tags else "")
            + (t("Has notes") if view.notes else "")
            if noted
            else ""
        )

        if view.total_size:
            self._cell(row, _COL_SIZE).setText(human_bytes(view.total_size))
        elif view.status is JobStatus.DOWNLOADING and view.downloaded:
            self._cell(row, _COL_SIZE).setText(human_bytes(view.downloaded))
        else:
            self._cell(row, _COL_SIZE).setText("")

        bar = self._progress_bars.get(view.id)
        if bar is not None:
            if view.total_size:
                bar.set_value(view.downloaded / view.total_size)
            elif view.status is JobStatus.DOWNLOADING:
                bar.set_indeterminate(True)
            else:
                bar.set_value(1.0 if view.status is JobStatus.COMPLETED else 0.0)
            bar.set_color(design.status_color(p, view.status.value))

        speed = self._smoothed_speed(view)
        speed_item = self._cell(row, _COL_SPEED)
        speed_item.setText(motion.fmt_speed(speed) if view.status is JobStatus.DOWNLOADING else "")
        # Speed is data, not an action: primary text while moving, muted idle.
        speed_item.setForeground(QColor(p.text if speed > 0 else p.text3))

        eta = ""
        if view.status is JobStatus.DOWNLOADING and speed > 1 and view.total_size:
            eta = motion.fmt_eta((view.total_size - view.downloaded) / speed)
        self._cell(row, _COL_ETA).setText(eta)

        pill = self._pills.get(view.id)
        if pill is not None:
            pill.set_status(view.status.value)
            pill.setToolTip(view.error or "")

    @staticmethod
    def _type_color(view: JobView) -> str:
        p = theme.current()
        return {
            JobKind.TORRENT: p.accent,
            JobKind.CLOUD: p.g_ndown,
            JobKind.SMART: p.g_ndown,
            JobKind.HLS: p.g_ndown,
        }.get(view.kind, p.text2)

    def _measure_speeds(self, views: list[JobView]) -> None:
        """Measure every running download's speed once per poll.

        Exactly once: the rows, the drawer and the toolbar total all read the
        cached numbers afterwards. Measuring per caller used to feed a second,
        zero-elapsed sample into the selected download's smoother every poll,
        which dragged its readout to zero.
        """
        now = time.monotonic()
        running = {v.id for v in views if v.status is JobStatus.DOWNLOADING}
        for job_id in list(self._speed_smoothers):
            if job_id not in running:
                self._speed_smoothers.pop(job_id, None)  # a finished job starts fresh
        for job_id in list(self._spark_history):
            if job_id not in running:
                self._spark_history.pop(job_id, None)
        speeds: dict[int, float] = {}
        for view in views:
            if view.id in running:
                smoother = self._speed_smoothers.setdefault(view.id, motion.SpeedSmoother())
                speed = smoother.push_total(now, view.downloaded)
                speeds[view.id] = speed
                # Keep a per-download speed trail for the detail drawer's graph,
                # measured every poll for every running job - so switching away
                # and back restores the history instead of starting over.
                self._spark_history.setdefault(view.id, deque(maxlen=motion.SPARK_HISTORY)).append(
                    speed
                )
            else:
                speeds[view.id] = 0.0
        self._speeds = speeds

    def _smoothed_speed(self, view: JobView) -> float:
        """This download's speed as measured by :meth:`_measure_speeds` for the
        current poll."""
        return self._speeds.get(view.id, 0.0)

    # ---------------------------------------------------------- drag & drop

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        data = event.mimeData()
        if data.hasUrls() or data.hasText():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        data = event.mimeData()
        # A .torrent file dropped from the file manager opens as a torrent.
        for dropped in data.urls():
            local = dropped.toLocalFile()
            if local and local.lower().endswith(".torrent"):
                event.acceptProposedAction()
                self.add_torrent_source(local)
                return
        text_parts = [url.toString() for url in data.urls()]
        if data.hasText():
            text_parts.append(data.text())
        magnets = [p for p in text_parts[-1:] if p.strip().lower().startswith("magnet:")]
        if magnets:
            event.acceptProposedAction()
            self.add_torrent_source(magnets[0].strip())
            return
        clouds = [p for p in text_parts[-1:] if cloud_engine.is_cloud_scheme(p.strip())]
        if clouds:
            event.acceptProposedAction()
            self.add_cloud_source(clouds[0].strip())
            return
        urls = expand_all(extract_urls("\n".join(text_parts)))
        if not urls:
            return
        event.acceptProposedAction()
        if len(urls) > 1:
            self._run_batch(urls)
        else:
            self.begin_add_url(urls[0])

    # --------------------------------------------------------------- close

    def showEvent(self, event: object) -> None:
        # Coming back from the tray: refresh() skipped every tick while hidden,
        # so catch the list up before the window is painted.
        super().showEvent(event)  # type: ignore[arg-type]
        self.refresh()

    def changeEvent(self, event: QEvent) -> None:
        # Minimize-to-tray (Settings → General): hide instead of the taskbar.
        if (
            event.type() == QEvent.Type.WindowStateChange
            and self.isMinimized()
            and self.close_to_tray  # tray is available
            and self.settings.minimize_to_tray
        ):
            QTimer.singleShot(0, self.hide)
        super().changeEvent(event)

    def _hint_still_running(self) -> None:
        """Say so, once, the first time closing the window only hides it -
        otherwise GrabLine looks like it quit and downloads look lost."""
        if self.tray is None or self.settings.tray_hint_shown:
            return
        self.settings.tray_hint_shown = True
        self.tray.showMessage(
            t("GrabLine is still running"),
            t("Downloads keep going. Click the tray icon to bring the window back."),
            QSystemTrayIcon.MessageIcon.Information,
            6000,
        )

    def _active_download_count(self) -> int:
        return sum(1 for v in self._last_views.values() if v.status is JobStatus.DOWNLOADING)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.close_to_tray and self.settings.close_to_tray and self.isVisible():
            event.ignore()
            self.hide()
            self._hint_still_running()
            return
        active = self._active_download_count()
        if active and self.settings.confirm_exit_active:
            noun = t("download is") if active == 1 else t("downloads are")
            answer = QMessageBox.question(
                self,
                "GrabLine",
                t(
                    "{active} {noun} still running. Quit anyway?\n"
                    "(Progress is saved; they resume next launch.)",
                    active=active,
                    noun=noun,
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()
