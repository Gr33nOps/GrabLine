"""Settings: every persisted option, organized into the sidebar sections the
embedded Settings page shows (General → About).

This dialog owns the fields and the save logic; it is normally never shown -
SettingsView lifts its tab pages into the embedded page. ``apply()`` persists
every field and is shared by both paths.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QThread, QTime, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLayout,
    QLayoutItem,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from app import __version__
from app.core import i18n, launcher, paths
from app.core.errors import DownloadError
from app.core.ffmpeg import ensure_ffmpeg, find_ffmpeg
from app.core.i18n import N_, t
from app.core.settings import MAX_CONNECTIONS, SESSION_BROWSERS, Settings
from app.ui import chrome, components, design, guard, theme, threads
from app.ui.format import human_bytes

_PROJECT_URL = "https://github.com/Gr33nOps/GrabLine"
_SUPPORT_URL = "https://ko-fi.com/zain021xd"


def _note(text: str) -> QLabel:
    """A muted, wrapping explanation label (theme-following). Translates its
    text, so callers pass the English literal - the string extractor recognizes
    ``_note(...)`` too (tools/i18n_extract.py)."""
    label = components.role_label(t(text), "muted")
    label.setWordWrap(True)
    # Without this, a wrapping label in a scroll area grows to the full text
    # width and the right edge of About (and any similar page) gets clipped
    # when the window is narrow - every other Settings tab uses a form that
    # already constrains field width, so only free-form pages need this.
    label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    label.setMinimumWidth(0)
    return label


def _prepare_freeform_page(page: QWidget) -> None:
    """Let a free-form Settings page shrink with the scroll pane and reflow.

    Form tabs already constrain field width; VBox pages (Video, About, …) do
    not, so without this the unwrapped size-hint forces a horizontal clip.
    """
    page.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    page.setMinimumWidth(0)


class _FlowLayout(QLayout):
    """Left-to-right, wrap-to-next-row layout for a row of buttons.

    QHBoxLayout never wraps, so six About buttons overflow a narrow Settings
    pane and get clipped by the scroll area. This lays them out like a
    paragraph of chips instead.
    """

    def __init__(self, parent: QWidget | None = None, *, spacing: int = 8) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect: QRect, *, test: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(
            margins.left(), margins.top(), -margins.right(), -margins.bottom()
        )
        x = effective.x()
        y = effective.y()
        row_height = 0
        space = self.spacing()
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + space
            if next_x - space > effective.right() + 1 and row_height > 0:
                x = effective.x()
                y += row_height + space
                next_x = x + hint.width() + space
                row_height = 0
            if not test:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            row_height = max(row_height, hint.height())
        return y + row_height - rect.y() + margins.bottom()


def _inline_row() -> QHBoxLayout:
    """A horizontal row of controls that reads as separate controls.

    Qt's default spacing is tight enough that a checkbox's box lands hard
    against the previous control's text - "Size[x]Progress" - because a
    checkbox has no border of its own to hold the gap open."""
    row = QHBoxLayout()
    row.setSpacing(14)
    return row


def _parse_rename_rules(text: str) -> list[tuple[str, str]]:
    """'find -> replace' per line; a line without '->' deletes the text."""
    rules: list[tuple[str, str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        find, _, replace = line.partition("->")
        if find.strip():
            rules.append((find.strip(), replace.strip()))
    return rules


def _parse_host_limits(text: str) -> dict[str, int]:
    """'host = KB/s' per line -> {host: kbps}; bad lines are dropped."""
    limits: dict[str, int] = {}
    for line in text.splitlines():
        host, _, value = line.partition("=")
        host = host.strip().lower()
        try:
            kbps = int(value.strip())
        except ValueError:
            continue
        if host and kbps > 0:
            limits[host] = kbps
    return limits


class _ShortcutsPage(QWidget):
    """Settings -> Shortcuts: one capture field per action, with per-row reset,
    a Reset-all, a live conflict warning, and a link to the cheat-sheet. On
    Save, :meth:`overrides` yields only the bindings that differ from the coded
    default, so the stored map stays small and future default changes flow
    through to anyone who never customized that key."""

    #: Read by SettingsView: this page scrolls its own list, so it should fill
    #: the content pane's height rather than sit in a short box.
    fills_height = True

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from app.ui.shortcuts import DEFAULTS, by_category, normalize

        self._settings = settings
        self._normalize = normalize
        self._defaults = {shortcut.id: shortcut.default for shortcut in DEFAULTS}
        self._edits: dict[str, QKeySequenceEdit] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(
            _note(
                "Click a shortcut and press the new key combination. Backspace "
                "clears it (unbinds the action). Changes apply when you Save."
            )
        )
        self._warning = components.role_label("", "muted")
        self._warning.setWordWrap(True)
        self._warning.setStyleSheet(f"color: {theme.current().warn};")
        outer.addWidget(self._warning)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        grid = QGridLayout(body)
        grid.setContentsMargins(2, 4, 12, 4)
        grid.setColumnStretch(0, 1)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)
        overrides = settings.shortcuts
        row = 0
        for category, shortcuts in by_category():
            grid.addWidget(components.role_label(t(category), "strong", bold=True), row, 0, 1, 3)
            row += 1
            for shortcut in shortcuts:
                grid.addWidget(QLabel(t(shortcut.label)), row, 0)
                edit = QKeySequenceEdit()
                edit.setMaximumSequenceLength(1)  # one chord (Ctrl+N), not a Qt sequence
                raw = overrides.get(shortcut.id, shortcut.default)
                edit.setKeySequence(QKeySequence(normalize(raw)))
                edit.keySequenceChanged.connect(self._check_conflicts)
                self._edits[shortcut.id] = edit
                grid.addWidget(edit, row, 1)
                reset = QPushButton(t("Reset"))
                reset.clicked.connect(lambda _checked=False, sid=shortcut.id: self._reset_one(sid))
                grid.addWidget(reset, row, 2)
                row += 1
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        bottom = QHBoxLayout()
        cheatsheet = QPushButton(t("View all shortcuts…"))
        cheatsheet.clicked.connect(self._open_cheatsheet)
        bottom.addWidget(cheatsheet)
        bottom.addStretch(1)
        reset_all = QPushButton(t("Reset all to defaults"))
        reset_all.clicked.connect(self._reset_all)
        bottom.addWidget(reset_all)
        outer.addLayout(bottom)

        self._check_conflicts()

    def _current(self) -> dict[str, str]:
        return {
            shortcut_id: self._normalize(
                edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
            )
            for shortcut_id, edit in self._edits.items()
        }

    def overrides(self) -> dict[str, str]:
        """Only the bindings that differ from their default, ready for
        ``Settings.shortcuts``."""
        current = self._current()
        return {
            shortcut_id: sequence
            for shortcut_id, sequence in current.items()
            if sequence != self._normalize(self._defaults[shortcut_id])
        }

    def _reset_one(self, shortcut_id: str) -> None:
        self._edits[shortcut_id].setKeySequence(QKeySequence(self._defaults[shortcut_id]))
        self._check_conflicts()

    def _reset_all(self) -> None:
        for shortcut_id, edit in self._edits.items():
            edit.setKeySequence(QKeySequence(self._defaults[shortcut_id]))
        self._check_conflicts()

    def _check_conflicts(self) -> None:
        from app.ui.shortcuts import BY_ID, conflicts

        clashes = conflicts(self._current())
        if not clashes:
            self._warning.setText("")
            return
        parts = []
        for sequence, ids in clashes.items():
            keys = QKeySequence(sequence).toString(QKeySequence.SequenceFormat.NativeText)
            names = ", ".join(t(BY_ID[i].label) for i in ids if i in BY_ID)
            parts.append(f"{keys} is bound to {names}")
        self._warning.setText("Conflict: " + "; ".join(parts))

    def _open_cheatsheet(self) -> None:
        from app.ui.shortcuts_dialog import ShortcutsDialog

        ShortcutsDialog(self._current(), self).exec()


class _FfmpegInstaller(QThread):
    progressed = Signal(int, object)  # received bytes, total or None
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, proxy: str | None = None) -> None:
        super().__init__()
        self._proxy = proxy

    def run(self) -> None:
        try:
            path = ensure_ffmpeg(progress=self.progressed.emit, proxy=self._proxy)
            self.succeeded.emit(str(path))
        except DownloadError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            self.failed.emit(f"unexpected error: {exc}")


class SettingsDialog(chrome.Dialog):
    #: Emitted after "Reset all settings" so the window reloads what it caches.
    settings_reset = Signal()

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle(t("Settings"))
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        self.tabs = tabs  # exposed so SettingsView can embed the pages
        layout.addWidget(tabs)

        # ---- General ---------------------------------------------------------
        general_form = self._add_form_tab(tabs, t("General"))
        self.language_combo = QComboBox()
        for code, name, native in i18n.available_languages():
            self.language_combo.addItem(native if native == name else f"{native} ({name})", code)
        general_form.addRow(t("Language:"), self.language_combo)
        general_form.addRow(
            _note("Changing the language asks you to restart GrabLine so the UI can rebuild.")
        )
        self.autostart_check = QCheckBox(t("Start GrabLine when I log in (minimized to the tray)"))
        general_form.addRow(self.autostart_check)
        self.updates_check = QCheckBox(t("Check for GrabLine updates on startup"))
        general_form.addRow(self.updates_check)
        self.start_min_check = QCheckBox(t("Start minimized to the tray"))
        general_form.addRow(self.start_min_check)
        self.tray_min_check = QCheckBox(t("Minimize to the tray instead of the taskbar"))
        general_form.addRow(self.tray_min_check)
        self.tray_close_check = QCheckBox(t("Closing the window keeps GrabLine in the tray"))
        general_form.addRow(self.tray_close_check)
        self.confirm_exit_check = QCheckBox(t("Confirm exit while downloads are running"))
        general_form.addRow(self.confirm_exit_check)
        self.confirm_downloads_check = QCheckBox(
            t("Ask for name and folder before each browser download")
        )
        general_form.addRow(self.confirm_downloads_check)
        self.new_dl_combo = QComboBox()
        self.new_dl_combo.addItem(t("Start automatically"), True)
        self.new_dl_combo.addItem(t("Add paused (start by hand)"), False)
        general_form.addRow(t("New downloads:"), self.new_dl_combo)
        general_form.addRow(_note("GrabLine runs as a single instance."))

        # ---- Downloads -------------------------------------------------------
        downloads_form = self._add_form_tab(tabs, t("Downloads"))
        folder_row = QHBoxLayout()
        self.folder_edit = QLineEdit()
        browse = QPushButton(t("Browse…"))
        browse.clicked.connect(self._browse_folder)
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(browse)
        downloads_form.addRow(t("Download folder:"), folder_row)
        self.categories_check = QCheckBox(
            t(
                "Sort into Video / Music / Images / Documents / "
                "Archives / Programs / Games / Torrents"
            )
        )
        downloads_form.addRow(self.categories_check)
        self.ask_save_check = QCheckBox(t("Ask where to save each download"))
        downloads_form.addRow(self.ask_save_check)
        self.concurrent_spin = QSpinBox()
        self.concurrent_spin.setRange(1, 10)
        downloads_form.addRow(t("Simultaneous downloads:"), self.concurrent_spin)
        self.free_mb_spin = QSpinBox()
        self.free_mb_spin.setRange(0, 1_000_000)
        self.free_mb_spin.setSingleStep(100)
        self.free_mb_spin.setSuffix(" MB")
        self.free_mb_spin.setSpecialValueText(t("Off"))
        downloads_form.addRow(t("Low disk space warning:"), self.free_mb_spin)
        downloads_form.addRow(
            _note(
                "Existing files are renamed name (1).ext, never overwritten; "
                "a duplicate URL asks first."
            )
        )

        # ---- Download Engine -------------------------------------------------
        engine_form = self._add_form_tab(tabs, t("Download Engine"))
        self.connections_spin = QSpinBox()
        self.connections_spin.setRange(1, MAX_CONNECTIONS)
        engine_form.addRow(t("Connections per download:"), self.connections_spin)
        self.speed_spin = QSpinBox()
        self.speed_spin.setRange(0, 1_000_000)
        self.speed_spin.setSingleStep(256)
        self.speed_spin.setSuffix(" KB/s")
        self.speed_spin.setSpecialValueText(t("Unlimited"))
        engine_form.addRow(t("Speed limit:"), self.speed_spin)
        self.fair_speed_check = QCheckBox(t("Share bandwidth evenly across simultaneous downloads"))
        engine_form.addRow(t("Fair speed:"), self.fair_speed_check)
        engine_form.addRow(
            _note(
                "Splits your speed limit (or measured line speed) equally: "
                "2 downloads → half each, 4 → a quarter each. Steadiest with a "
                "Speed limit set above."
            )
        )
        schedule_row = _inline_row()
        self.schedule_check = QCheckBox(t("Full speed between"))
        self.full_from = QTimeEdit()
        self.full_from.setDisplayFormat("HH:mm")
        self.full_to = QTimeEdit()
        self.full_to.setDisplayFormat("HH:mm")
        schedule_row.addWidget(self.schedule_check)
        schedule_row.addWidget(self.full_from)
        schedule_row.addWidget(QLabel(t("and")))
        schedule_row.addWidget(self.full_to)
        schedule_row.addStretch(1)
        engine_form.addRow(t("Speed schedule:"), schedule_row)
        retry_row = _inline_row()
        self.retry_check = QCheckBox(t("Auto-retry failed downloads, up to"))
        self.retry_spin = QSpinBox()
        self.retry_spin.setRange(0, 99)
        self.retry_spin.setSuffix(" times")
        self.retry_spin.setSpecialValueText(t("Forever"))
        retry_row.addWidget(self.retry_check)
        retry_row.addWidget(self.retry_spin)
        retry_row.addStretch(1)
        engine_form.addRow(t("Reconnect:"), retry_row)
        engine_form.addRow(
            _note(
                t("Right-click a download to override its speed limit, connections, and mirrors.")
            )
        )
        engine_form.addRow(
            _note(
                "Segmentation, HTTP/2, IPv6, mirror failover, and crash or reconnect "
                "resume are automatic. HTTP/3 and QUIC aren't supported yet."
            )
        )

        # ---- Browser Integration ---------------------------------------------
        browser_tab = QWidget()
        _prepare_freeform_page(browser_tab)
        browser_layout = QVBoxLayout(browser_tab)
        pairing = QGroupBox(t("Browser extension"))
        pairing_layout = QHBoxLayout(pairing)
        pairing_label = QLabel(
            t(
                "Pair GrabLine with your browsers so the GrabLine Connect "
                "extension can hand downloads over."
            )
        )
        pairing_label.setWordWrap(True)
        pairing_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        pairing_label.setMinimumWidth(0)
        pairing_layout.addWidget(pairing_label, 1)
        pair_button = QPushButton(t("Pair browsers"))
        pair_button.clicked.connect(self._pair_browsers)
        pairing_layout.addWidget(pair_button)
        setup_button = QPushButton(t("Setup wizard…"))
        setup_button.clicked.connect(self._open_setup_wizard)
        pairing_layout.addWidget(setup_button)
        browser_layout.addWidget(pairing)

        session = QGroupBox(t("YouTube sign-in"))
        session_layout = QVBoxLayout(session)
        browser_row = QHBoxLayout()
        browser_row.addWidget(QLabel(t("I use:")))
        self.browser_combo = QComboBox()
        for browser in SESSION_BROWSERS:
            self.browser_combo.addItem(browser.capitalize(), browser)
        browser_row.addWidget(self.browser_combo, 1)
        session_layout.addLayout(browser_row)
        session_layout.addWidget(
            _note(
                "GrabLine signs YouTube in for you using this browser - no cookies file, "
                "no extra steps. Just stay signed in to YouTube there. Kept in memory per "
                "download, never stored."
            )
        )
        browser_layout.addWidget(session)
        self.clipboard_check = QCheckBox(
            t("URL catcher: offer to download links copied to the clipboard")
        )
        browser_layout.addWidget(self.clipboard_check)
        browser_layout.addWidget(
            _note(
                "Hover buttons, download takeover, and media detection are controlled in the "
                "extension's toolbar popup."
            )
        )
        browser_layout.addStretch(1)
        tabs.addTab(browser_tab, t("Browser Integration"))

        # ---- Video Downloader ------------------------------------------------
        video_tab = QWidget()
        _prepare_freeform_page(video_tab)
        video_layout = QVBoxLayout(video_tab)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.setSpacing(12)
        ffmpeg_group = QGroupBox(t("FFmpeg (needed for MP3, merging, and streams)"))
        ffmpeg_layout = QHBoxLayout(ffmpeg_group)
        self.ffmpeg_status = QLabel()
        self.ffmpeg_status.setWordWrap(True)
        self.ffmpeg_status.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.ffmpeg_status.setMinimumWidth(0)
        ffmpeg_layout.addWidget(self.ffmpeg_status, 1)
        self.ffmpeg_button = QPushButton()
        self.ffmpeg_button.clicked.connect(self._install_ffmpeg)
        ffmpeg_layout.addWidget(self.ffmpeg_button)
        self._refresh_ffmpeg_status()
        video_layout.addWidget(ffmpeg_group)
        playlist_form = QFormLayout()
        playlist_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.playlist_cap_spin = QSpinBox()
        self.playlist_cap_spin.setRange(1, 500)
        playlist_form.addRow(t("Preselect playlist entries:"), self.playlist_cap_spin)
        video_layout.addLayout(playlist_form)
        # QCheckBox cannot wrap; keep the control short and put the long
        # explanation in a note so a narrow Settings pane does not clip it.
        self.hq_first_check = QCheckBox(t("Prefer highest quality over a fast start"))
        video_layout.addWidget(self.hq_first_check)
        video_layout.addWidget(
            _note(
                "Solves YouTube's JS challenge up front; can add minutes before a download begins."
            )
        )
        self.confirm_video_quality_check = QCheckBox(
            t("Show the quality panel for browser video downloads")
        )
        video_layout.addWidget(self.confirm_video_quality_check)
        video_layout.addWidget(
            _note(
                "Off queues immediately at the default quality below, with no panel. "
                "Separate from 'Ask for name and folder' in General - turning that off "
                "no longer turns this off too."
            )
        )
        defaults_form = QFormLayout()
        defaults_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.default_quality_combo = QComboBox()
        for label in ("Best", "2160p", "1440p", "1080p", "720p", "480p", "MP3", "M4A", "FLAC"):
            self.default_quality_combo.addItem(label)
        defaults_form.addRow(t("Default quality:"), self.default_quality_combo)
        self.bitrate_combo = QComboBox()
        for rate in ("128", "192", "256", "320"):
            self.bitrate_combo.addItem(f"{rate} kbps", rate)
        defaults_form.addRow(t("MP3 bitrate:"), self.bitrate_combo)
        cookies_row = QHBoxLayout()
        self.cookies_edit = QLineEdit()
        self.cookies_edit.setPlaceholderText(t("Optional fallback only"))
        cookies_browse = QPushButton(t("Browse…"))
        cookies_browse.clicked.connect(self._browse_cookies)
        cookies_row.addWidget(self.cookies_edit, 1)
        cookies_row.addWidget(cookies_browse)
        defaults_form.addRow(t("Cookies file (optional):"), cookies_row)
        video_layout.addLayout(defaults_form)
        video_layout.addWidget(
            _note(
                "YouTube login is automatic from your browser (see Browser Integration). "
                "A cookies.txt here is only a rare fallback. Quality, subtitles, trimming, "
                "and SponsorBlock are chosen per download in the panel."
            )
        )
        video_layout.addWidget(_note(self._js_runtime_text()))
        video_layout.addWidget(
            _note(
                "Quality, audio format, subtitles, trimming, and chapters are chosen when you "
                "add a video."
            )
        )
        video_layout.addStretch(1)
        tabs.addTab(video_tab, t("Video Downloader"))

        # ---- Torrent ----------------------------------------------------------
        torrent_form = self._add_form_tab(tabs, t("Torrent"))
        self.torrent_port_spin = QSpinBox()
        self.torrent_port_spin.setRange(1024, 65535)
        torrent_form.addRow(t("Listen port:"), self.torrent_port_spin)
        self.dht_check = QCheckBox(t("DHT (find peers without trackers; needed for magnets)"))
        torrent_form.addRow(self.dht_check)
        self.upnp_check = QCheckBox(t("UPnP port mapping"))
        torrent_form.addRow(self.upnp_check)
        self.natpmp_check = QCheckBox(t("NAT-PMP port mapping"))
        torrent_form.addRow(self.natpmp_check)
        seed_row = _inline_row()
        self.seed_check = QCheckBox(t("Seed after downloading, up to ratio"))
        self.ratio_spin = QDoubleSpinBox()
        self.ratio_spin.setRange(0.0, 100.0)
        self.ratio_spin.setSingleStep(0.5)
        self.ratio_spin.setSpecialValueText(t("Forever"))
        seed_row.addWidget(self.seed_check)
        seed_row.addWidget(self.ratio_spin)
        seed_row.addStretch(1)
        torrent_form.addRow(t("Seeding:"), seed_row)
        self.upload_spin = QSpinBox()
        self.upload_spin.setRange(0, 1_000_000)
        self.upload_spin.setSingleStep(64)
        self.upload_spin.setSuffix(" KB/s")
        self.upload_spin.setSpecialValueText(t("Unlimited"))
        torrent_form.addRow(t("Upload limit:"), self.upload_spin)
        self.sequential_check = QCheckBox(t("Sequential download by default (stream-friendly)"))
        torrent_form.addRow(self.sequential_check)
        torrent_dir_row = QHBoxLayout()
        self.torrent_dir_edit = QLineEdit()
        self.torrent_dir_edit.setPlaceholderText(t("blank = the download folder"))
        torrent_browse = QPushButton(t("Browse…"))
        torrent_browse.clicked.connect(self._browse_torrent_folder)
        torrent_dir_row.addWidget(self.torrent_dir_edit, 1)
        torrent_dir_row.addWidget(torrent_browse)
        torrent_form.addRow(t("Save torrents to:"), torrent_dir_row)
        self.search_url_edit = QLineEdit()
        self.search_url_edit.setPlaceholderText(t("https://example.com/search?q=%s"))
        torrent_form.addRow(t("Search URL:"), self.search_url_edit)
        self.rss_edit = QPlainTextEdit()
        self.rss_edit.setPlaceholderText(t("feed URL, or:  feed URL | filter text\none per line"))
        self.rss_edit.setFixedHeight(72)
        torrent_form.addRow(t("RSS feeds:"), self.rss_edit)
        self.rss_interval_spin = QSpinBox()
        self.rss_interval_spin.setRange(5, 1440)
        self.rss_interval_spin.setSuffix(" min")
        torrent_form.addRow(t("Check feeds every:"), self.rss_interval_spin)
        self.encryption_combo = QComboBox()
        for label, value in (
            (N_("Prefer encrypted peers"), "prefer"),
            (N_("Require encryption"), "require"),
            (N_("Plaintext only"), "off"),
        ):
            self.encryption_combo.addItem(t(label), value)
        torrent_form.addRow(t("Encryption:"), self.encryption_combo)
        self.seed_minutes_spin = QSpinBox()
        self.seed_minutes_spin.setRange(0, 100_000)
        self.seed_minutes_spin.setSuffix(" min")
        self.seed_minutes_spin.setSpecialValueText(t("No time limit"))
        torrent_form.addRow(t("Seeding time limit:"), self.seed_minutes_spin)
        self.trackers_default_edit = QPlainTextEdit()
        self.trackers_default_edit.setPlaceholderText(
            t("default tracker URLs for Create Torrent, one per line")
        )
        self.trackers_default_edit.setFixedHeight(56)
        torrent_form.addRow(t("Default trackers:"), self.trackers_default_edit)
        torrent_form.addRow(
            _note(
                "PEX and web seeds are always on. Concurrency is set in Queue manager; "
                "bandwidth follows the global limits."
            )
        )

        # ---- Cloud Downloads ---------------------------------------------------
        cloud_tab = QWidget()
        _prepare_freeform_page(cloud_tab)
        cloud_layout = QVBoxLayout(cloud_tab)
        cloud_layout.addWidget(
            _note(
                "Paste an sftp, ftp, ftps, scp, s3, or webdav address, or a public Drive, "
                "Dropbox, OneDrive, or Nextcloud share link into Add URL."
            )
        )
        accounts_row = QHBoxLayout()
        manage_accounts = QPushButton(t("Manage saved logins…"))
        manage_accounts.clicked.connect(self._manage_cloud_accounts)
        accounts_row.addWidget(manage_accounts)
        accounts_row.addStretch(1)
        cloud_layout.addLayout(accounts_row)
        cloud_layout.addWidget(
            _note(
                "Passwords are kept in your system keychain; the right account "
                "is chosen by address."
            )
        )
        cloud_layout.addWidget(_note("Not supported: Mega, Proton Drive, and iCloud."))
        cloud_layout.addStretch(1)
        tabs.addTab(cloud_tab, t("Cloud Downloads"))

        # ---- Archive Manager ---------------------------------------------------
        archive_form = self._add_form_tab(tabs, t("Archive Manager"))
        self.extract_check = QCheckBox(
            t("Extract archives automatically (zip/tar/gz/bz2/xz; rar/7z with 7z installed)")
        )
        archive_form.addRow(self.extract_check)
        self.scan_check = QCheckBox(t("Virus-scan archives before extracting"))
        archive_form.addRow(self.scan_check)
        self.passwords_edit = QPlainTextEdit()
        self.passwords_edit.setPlaceholderText(t("one password per line"))
        self.passwords_edit.setFixedHeight(72)
        archive_form.addRow(t("Archive passwords:"), self.passwords_edit)
        self.extract_subfolder_check = QCheckBox(t("Extract into a folder named after the archive"))
        archive_form.addRow(self.extract_subfolder_check)
        self.delete_archive_check = QCheckBox(t("Delete the archive after a clean extraction"))
        archive_form.addRow(self.delete_archive_check)
        archive_form.addRow(
            _note(
                "ZIP, TAR, GZ, BZ2, and XZ are built in; RAR and 7Z need an installed 7-Zip. "
                "Right-click a finished archive to preview or extract it."
            )
        )

        # ---- File Management ---------------------------------------------------
        files_form = self._add_form_tab(tabs, t("File Management"))
        self.favorites_edit = QPlainTextEdit()
        self.favorites_edit.setPlaceholderText(t("one folder per line, e.g. /home/me/Movies"))
        self.favorites_edit.setFixedHeight(72)
        files_form.addRow(t("Favorite folders:"), self.favorites_edit)
        self.rename_edit = QPlainTextEdit()
        self.rename_edit.setPlaceholderText(t("find -> replace   (one rule per line)"))
        self.rename_edit.setFixedHeight(72)
        files_form.addRow(t("Rename rules:"), self.rename_edit)
        self.default_tags_edit = QLineEdit()
        self.default_tags_edit.setPlaceholderText(t("tags for every new download, comma separated"))
        files_form.addRow(t("Default tags:"), self.default_tags_edit)
        files_form.addRow(
            _note(
                "Files are named from the page title, illegal characters are "
                "stripped, and duplicates become name (1).ext. Categories map "
                "by file extension."
            )
        )

        # ---- Queue Manager -----------------------------------------------------
        queue_form = self._add_form_tab(tabs, t("Queue Manager"))
        self.default_queue_combo = QComboBox()
        self.default_queue_combo.addItem(t("(the global default)"), 0)
        for queue_row in settings.db.list_queues():
            self.default_queue_combo.addItem(queue_row.name, queue_row.id)
        queue_form.addRow(t("Default queue:"), self.default_queue_combo)
        queue_form.addRow(
            _note(
                "Named queues (concurrency, order, schedules, and dependencies) are managed "
                "on the Queue page. The global download limit is under Downloads."
            )
        )

        # ---- Scheduler ---------------------------------------------------------
        scheduler_form = self._add_form_tab(tabs, t("Scheduler"))
        window_row = _inline_row()
        self.download_schedule_check = QCheckBox(t("Only download between"))
        self.download_start = QTimeEdit()
        self.download_start.setDisplayFormat("HH:mm")
        self.download_stop = QTimeEdit()
        self.download_stop.setDisplayFormat("HH:mm")
        window_row.addWidget(self.download_schedule_check)
        window_row.addWidget(self.download_start)
        window_row.addWidget(QLabel(t("and")))
        window_row.addWidget(self.download_stop)
        window_row.addStretch(1)
        scheduler_form.addRow(t("Download times:"), window_row)
        days_row = _inline_row()
        self.day_checks: list[QCheckBox] = []
        for label in (N_("Mon"), N_("Tue"), N_("Wed"), N_("Thu"), N_("Fri"), N_("Sat"), N_("Sun")):
            check = QCheckBox(t(label))
            self.day_checks.append(check)
            days_row.addWidget(check)
        days_row.addStretch(1)
        scheduler_form.addRow(t("Days:"), days_row)
        battery_row = _inline_row()
        self.battery_check = QCheckBox(t("Pause downloads on battery, below"))
        self.battery_pct_spin = QSpinBox()
        self.battery_pct_spin.setRange(0, 100)
        self.battery_pct_spin.setSuffix(" %")
        self.battery_pct_spin.setSpecialValueText(t("any charge"))
        battery_row.addWidget(self.battery_check)
        battery_row.addWidget(self.battery_pct_spin)
        battery_row.addStretch(1)
        scheduler_form.addRow(battery_row)
        self.network_check = QCheckBox(t("Wait for internet, resume the moment it reconnects"))
        scheduler_form.addRow(self.network_check)
        self.after_combo = QComboBox()
        for label, value in (
            (N_("Do nothing"), "nothing"),
            (N_("Quit GrabLine"), "quit"),
            (N_("Sleep the computer"), "sleep"),
            (N_("Hibernate the computer"), "hibernate"),
            (N_("Shut down the computer"), "shutdown"),
            (N_("Lock the computer"), "lock"),
        ):
            self.after_combo.addItem(t(label), value)
        scheduler_form.addRow(t("When the queue empties:"), self.after_combo)
        scheduler_form.addRow(
            _note(
                "Right-click a download for one-off scheduling: Start at… a "
                "chosen time, or Start after… another download finishes."
            )
        )

        # ---- Network -----------------------------------------------------------
        net_tab = QWidget()
        _prepare_freeform_page(net_tab)
        net_layout = QVBoxLayout(net_tab)
        proxy_form = QFormLayout()
        self.proxy_edit = QLineEdit()
        self.proxy_edit.setPlaceholderText(
            t("http(s):// · socks5:// · socks4:// host:port  (blank = direct)")
        )
        proxy_form.addRow(t("Proxy:"), self.proxy_edit)
        net_layout.addLayout(proxy_form)
        net_layout.addWidget(_note(self._vpn_status_text()))

        throttle_group = QGroupBox(t("Automatic throttle (polite mode)"))
        throttle_layout = QFormLayout(throttle_group)
        throttle_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self.auto_throttle_check = QCheckBox(
            t("Slow downloads when other apps are using the network")
        )
        throttle_layout.addRow(self.auto_throttle_check)
        self.throttle_limit_spin = QSpinBox()
        self.throttle_limit_spin.setRange(1, 1_000_000)
        self.throttle_limit_spin.setSuffix(" KB/s")
        throttle_layout.addRow(t("Slow down to:"), self.throttle_limit_spin)
        self.throttle_threshold_spin = QSpinBox()
        self.throttle_threshold_spin.setRange(1, 1_000_000)
        self.throttle_threshold_spin.setSuffix(" KB/s")
        throttle_layout.addRow(t("When others use over:"), self.throttle_threshold_spin)
        net_layout.addWidget(throttle_group)

        host_group = QGroupBox(t("Per-host speed limits"))
        host_layout = QVBoxLayout(host_group)
        host_layout.addWidget(QLabel(t("One 'host = KB/s' per line, e.g.  cdn.example.com = 500")))
        self.host_limits_edit = QPlainTextEdit()
        self.host_limits_edit.setPlaceholderText(t("cdn.example.com = 500"))
        self.host_limits_edit.setFixedHeight(90)
        host_layout.addWidget(self.host_limits_edit)
        net_layout.addWidget(host_group)
        extras_form = QFormLayout()
        self.bypass_edit = QLineEdit()
        self.bypass_edit.setPlaceholderText(t("hosts that skip the proxy, comma separated"))
        extras_form.addRow(t("Proxy bypass:"), self.bypass_edit)
        self.ua_edit = QLineEdit()
        self.ua_edit.setPlaceholderText(
            t("custom User-Agent for plain downloads (blank = default)")
        )
        extras_form.addRow(t("User-Agent:"), self.ua_edit)
        net_layout.addLayout(extras_form)
        net_layout.addWidget(
            _note(
                "One proxy covers HTTP, HTTPS, SOCKS5, and SOCKS4, with user:pass@ auth. VPN "
                "status shows on the Dashboard and never blocks downloads."
            )
        )
        net_layout.addStretch(1)
        tabs.addTab(net_tab, t("Network"))

        # ---- Security ----------------------------------------------------------
        security_form = self._add_form_tab(tabs, t("Security"))
        security_form.addRow(
            _note("These checks only warn. A flagged file stays usable and it's your call.")
        )
        self.scan_downloads_check = QCheckBox(
            t("Security-check every finished download (local virus scan + VirusTotal if set)")
        )
        security_form.addRow(self.scan_downloads_check)
        self.enforce_https_check = QCheckBox(t("Warn before downloading over unencrypted HTTP"))
        security_form.addRow(self.enforce_https_check)
        self.virustotal_edit = QLineEdit()
        self.virustotal_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.virustotal_edit.setPlaceholderText(t("your VirusTotal API key (optional)"))
        security_form.addRow(t("VirusTotal key:"), self.virustotal_edit)
        self.safebrowsing_edit = QLineEdit()
        self.safebrowsing_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.safebrowsing_edit.setPlaceholderText(t("your Google Safe Browsing API key (optional)"))
        security_form.addRow(t("Safe Browsing key:"), self.safebrowsing_edit)
        self.scanner_combo = QComboBox()
        for label, value in (
            (N_("Automatic (first found)"), "auto"),
            ("Windows Defender", "defender"),
            ("ClamAV", "clamav"),
        ):
            self.scanner_combo.addItem(t(label), value)
        security_form.addRow(t("Virus scanner:"), self.scanner_combo)
        self.scan_ext_edit = QLineEdit()
        self.scan_ext_edit.setPlaceholderText(
            t("only scan these types, e.g. exe, msi, zip (blank = all)")
        )
        security_form.addRow(t("File types to scan:"), self.scan_ext_edit)
        security_form.addRow(
            _note(
                "Checksums support MD5, SHA-1, SHA-256, SHA-512, and CRC32. TLS is always "
                "validated. Nothing is ever quarantined or deleted."
            )
        )

        # ---- Notifications -----------------------------------------------------
        notify_form = self._add_form_tab(tabs, t("Notifications"))
        self.notify_check = QCheckBox(t("Show a notification when a download completes"))
        notify_form.addRow(self.notify_check)
        sound_row = _inline_row()
        self.sound_check = QCheckBox(t("Play a sound"))
        self.sound_file_edit = QLineEdit()
        self.sound_file_edit.setPlaceholderText(t("blank = system sound"))
        sound_browse = QPushButton(t("Browse…"))
        sound_browse.clicked.connect(self._browse_sound)
        sound_row.addWidget(self.sound_check)
        sound_row.addWidget(self.sound_file_edit, 1)
        sound_row.addWidget(sound_browse)
        notify_form.addRow(t("On completion:"), sound_row)
        self.open_folder_check = QCheckBox(t("Open the folder when a download completes"))
        notify_form.addRow(self.open_folder_check)
        self.notify_failed_check = QCheckBox(t("Notify when a download fails"))
        notify_form.addRow(self.notify_failed_check)
        self.notify_queue_check = QCheckBox(t("Notify when the whole queue finishes"))
        notify_form.addRow(self.notify_queue_check)
        self.toast_spin = QSpinBox()
        self.toast_spin.setRange(1, 30)
        self.toast_spin.setSuffix(" s")
        notify_form.addRow(t("Notification duration:"), self.toast_spin)
        quiet_row = _inline_row()
        self.quiet_check = QCheckBox(t("Quiet hours between"))
        self.quiet_from_edit = QTimeEdit()
        self.quiet_from_edit.setDisplayFormat("HH:mm")
        self.quiet_to_edit = QTimeEdit()
        self.quiet_to_edit.setDisplayFormat("HH:mm")
        quiet_row.addWidget(self.quiet_check)
        quiet_row.addWidget(self.quiet_from_edit)
        quiet_row.addWidget(QLabel(t("and")))
        quiet_row.addWidget(self.quiet_to_edit)
        quiet_row.addStretch(1)
        notify_form.addRow(quiet_row)
        notify_form.addRow(
            _note("Quiet hours silence notifications and sounds; downloads keep running.")
        )

        # ---- Statistics --------------------------------------------------------
        stats_tab = QWidget()
        _prepare_freeform_page(stats_tab)
        stats_layout = QVBoxLayout(stats_tab)
        self.stats_label = QLabel()
        self.stats_label.setWordWrap(True)
        self.stats_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.stats_label.setMinimumWidth(0)
        self._refresh_stats_label()
        stats_layout.addWidget(self.stats_label)
        stats_layout.addWidget(
            _note(
                "Graphs and per-server breakdowns are on the Dashboard. "
                "Statistics stay on this machine."
            )
        )
        stats_form = QFormLayout()
        self.stats_enabled_check = QCheckBox(t("Record download statistics (always local-only)"))
        stats_form.addRow(self.stats_enabled_check)
        self.retention_spin = QSpinBox()
        self.retention_spin.setRange(0, 3650)
        self.retention_spin.setSuffix(" days")
        self.retention_spin.setSpecialValueText(t("Keep forever"))
        stats_form.addRow(t("Keep daily data for:"), self.retention_spin)
        self.refresh_spin = QSpinBox()
        self.refresh_spin.setRange(100, 5000)
        self.refresh_spin.setSingleStep(100)
        self.refresh_spin.setSuffix(" ms")
        stats_form.addRow(t("Dashboard refresh:"), self.refresh_spin)
        stats_layout.addLayout(stats_form)
        clear_row = QHBoxLayout()
        clear_stats = QPushButton(t("Clear statistics…"))
        clear_stats.clicked.connect(self._clear_stats)
        export_stats = QPushButton(t("Export CSV…"))
        export_stats.clicked.connect(self._export_stats)
        clear_row.addWidget(clear_stats)
        clear_row.addWidget(export_stats)
        clear_row.addStretch(1)
        stats_layout.addLayout(clear_row)
        stats_layout.addStretch(1)
        tabs.addTab(stats_tab, t("Statistics"))

        # ---- Appearance --------------------------------------------------------
        appearance_form = self._add_form_tab(tabs, t("Appearance"))
        self.theme_combo = QComboBox()
        for label, value in (
            (N_("Match system"), "system"),
            (N_("Light"), "light"),
            (N_("Dark"), "dark"),
        ):
            self.theme_combo.addItem(t(label), value)
        appearance_form.addRow(t("Theme:"), self.theme_combo)
        self.accent_combo = QComboBox()
        for label, value in design.ACCENT_PRESETS:
            self.accent_combo.addItem(t(label), value)
        appearance_form.addRow(t("Accent color:"), self.accent_combo)
        self.density_combo = QComboBox()
        self.density_combo.addItem(t("Comfortable"), "comfortable")
        self.density_combo.addItem(t("Compact"), "compact")
        appearance_form.addRow(t("List density:"), self.density_combo)
        columns_row = _inline_row()
        self.column_checks: dict[str, QCheckBox] = {}
        for key, label in (
            ("size", "Size"),
            ("progress", "Progress"),
            ("speed", "Speed"),
            ("eta", "ETA"),
            ("status", "Status"),
        ):
            check = QCheckBox(t(label))
            self.column_checks[key] = check
            columns_row.addWidget(check)
        columns_row.addStretch(1)
        appearance_form.addRow(t("Visible columns:"), columns_row)
        appearance_form.addRow(_note("Theme and accent apply instantly."))

        # ---- Advanced ----------------------------------------------------------
        advanced_form = self._add_form_tab(tabs, t("Advanced"))
        self.script_edit = QLineEdit()
        self.script_edit.setPlaceholderText(t("e.g. /usr/bin/my-script --scan"))
        advanced_form.addRow(t("Run command:"), self.script_edit)
        self.ffmpeg_override_edit = QLineEdit()
        self.ffmpeg_override_edit.setPlaceholderText(t("blank = found automatically"))
        advanced_form.addRow(t("FFmpeg path override:"), self.ffmpeg_override_edit)
        data_label = QLabel(str(paths.data_dir()))
        data_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        advanced_form.addRow(t("Data folder:"), data_label)
        advanced_form.addRow(
            _note("Holds the download list, settings, and statistics (grabline.db).")
        )
        self.log_combo = QComboBox()
        for level in ("debug", "info", "warning", "error"):
            self.log_combo.addItem(level.capitalize(), level)
        advanced_form.addRow(t("Logging level:"), self.log_combo)
        self.logfile_check = QCheckBox(t("Also write the log to grabline.log in the data folder"))
        advanced_form.addRow(self.logfile_check)
        io_row = QHBoxLayout()
        export_btn = QPushButton(t("Export settings…"))
        export_btn.clicked.connect(self._export_settings)
        import_btn = QPushButton(t("Import settings…"))
        import_btn.clicked.connect(self._import_settings)
        reset_btn = QPushButton(t("Reset all settings…"))
        reset_btn.clicked.connect(self._reset_settings)
        io_row.addWidget(export_btn)
        io_row.addWidget(import_btn)
        io_row.addWidget(reset_btn)
        io_row.addStretch(1)
        advanced_form.addRow(t("Backup:"), io_row)
        db_row = QHBoxLayout()
        vacuum_btn = QPushButton(t("Compact database"))
        vacuum_btn.clicked.connect(self._vacuum_db)
        integrity_btn = QPushButton(t("Check database"))
        integrity_btn.clicked.connect(self._check_db)
        db_row.addWidget(vacuum_btn)
        db_row.addWidget(integrity_btn)
        db_row.addStretch(1)
        advanced_form.addRow(t("Maintenance:"), db_row)
        advanced_form.addRow(
            _note(
                "There's no remote-control port and no bundled yt-dlp binary. Logging changes "
                "apply on the next launch."
            )
        )

        # ---- About -------------------------------------------------------------
        about_tab = QWidget()
        # Preferred/Minimum so the scroll area can shrink the page to its width
        # (without this, word-wrap and the button flow never reflow - they force
        # a horizontal clip instead, which is why only About looked cut off).
        _prepare_freeform_page(about_tab)
        about_layout = QVBoxLayout(about_tab)
        about_layout.setContentsMargins(0, 0, 0, 0)
        about_layout.setSpacing(12)
        head = QHBoxLayout()
        head.addWidget(components.app_logo(44))
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        name_label = components.role_label("GrabLine", "strong")
        font = name_label.font()
        font.setPointSize(font.pointSize() + 5)
        font.setBold(True)
        name_label.setFont(font)
        title_box.addWidget(name_label)
        title_box.addWidget(
            components.role_label(t("Version {version}", version=__version__), "muted")
        )
        head.addSpacing(10)
        head.addLayout(title_box)
        head.addStretch(1)
        about_layout.addLayout(head)
        about_layout.addWidget(
            _note(
                "A free, open-source download manager under the AGPL-3.0 license. "
                "No ads, no telemetry. Built in spare time. If GrabLine helps you, "
                "a coffee keeps development going."
            )
        )
        links_row = _FlowLayout(spacing=8)
        update_btn = QPushButton(t("Check for updates"))
        update_btn.clicked.connect(self._check_updates_now)
        project_btn = QPushButton(t("Project page"))
        project_btn.clicked.connect(lambda: self._open_url(_PROJECT_URL))
        releases_btn = QPushButton(t("Changelog && releases"))
        releases_btn.clicked.connect(lambda: self._open_url(f"{_PROJECT_URL}/releases"))
        report_btn = QPushButton(t("Report an issue"))
        report_btn.clicked.connect(lambda: self._open_url(f"{_PROJECT_URL}/issues"))
        support_btn = QPushButton(t("Buy me a coffee"))
        support_btn.clicked.connect(lambda: self._open_url(_SUPPORT_URL))
        diag_btn = QPushButton(t("Copy diagnostics"))
        diag_btn.clicked.connect(self._copy_diagnostics)
        for btn in (
            update_btn,
            project_btn,
            releases_btn,
            report_btn,
            support_btn,
            diag_btn,
        ):
            links_row.addWidget(btn)
        about_layout.addLayout(links_row)
        about_layout.addWidget(
            _note(
                "Built on open source: yt-dlp (video engine), PySide6/Qt "
                "(interface), libtorrent (torrents), FFmpeg (conversion), "
                "httpx, paramiko, boto3, psutil."
            )
        )
        about_layout.addStretch(1)

        # ---- Shortcuts -------------------------------------------------------
        self._shortcuts_page = _ShortcutsPage(settings)
        tabs.addTab(self._shortcuts_page, t("Shortcuts"))

        tabs.addTab(about_tab, t("About"))

        self._load_values()

        # Enum-ish fields hug a sane width instead of stretching form-wide.
        components.cap_field_widths(self)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._installer: _FfmpegInstaller | None = None
        #: Keys of one-shot actions currently running, so a double-click can't
        #: launch a second dialog or task (see app/ui/guard.py).
        self._in_flight: set[str] = set()

    @staticmethod
    def _add_form_tab(tabs: QTabWidget, title: str) -> QFormLayout:
        """Add a tab whose body is a top-aligned form, and return that form."""
        page = QWidget()
        form = QFormLayout(page)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        # Everything hangs off one left edge - labels, fields, and spanning
        # checkbox rows - instead of the platform default's ragged mix.
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        # Roomy, even rhythm: a wide gap between a label and its field, generous
        # row spacing, and consistent page padding across every section.
        form.setHorizontalSpacing(24)
        form.setVerticalSpacing(13)
        form.setContentsMargins(4, 8, 12, 8)
        tabs.addTab(page, title)
        return form

    @staticmethod
    def _js_runtime_text() -> str:
        from app.core import jsruntime

        runtime = jsruntime.detect_js_runtime()
        if runtime is not None:
            return f"JavaScript runtime (for YouTube): {runtime[0]} at {runtime[1]}."
        return (
            "JavaScript runtime (for YouTube): none found. Deno (~40 MB, "
            "verified) is installed automatically on the first YouTube download."
        )

    @staticmethod
    def _open_url(url: str) -> None:
        QDesktopServices.openUrl(QUrl(url))

    def _refresh_stats_label(self) -> None:
        total_bytes, total_files = self.settings.db.lifetime_bytes()
        self.stats_label.setText(
            t(
                "Lifetime: {size} downloaded across {count} file(s).",
                size=human_bytes(total_bytes),
                count=total_files,
            )
        )

    def _clear_stats(self) -> None:
        answer = QMessageBox.question(
            self,
            "GrabLine",
            t("Clear all download statistics? The download list itself is not touched."),
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.settings.db.clear_stats()
            self._refresh_stats_label()

    def _browse_cookies(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self, t("Cookies file"), "", "cookies.txt (*.txt);;All files (*)"
        )
        if chosen:
            self.cookies_edit.setText(chosen)

    def _export_stats(self) -> None:
        path, _f = QFileDialog.getSaveFileName(
            self, t("Export statistics"), "grabline-stats.csv", "CSV (*.csv)"
        )
        if not path:
            return
        import csv

        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["day", "category", "host", "bytes", "files"])
            writer.writerows(self.settings.db.stats_rows())
        QMessageBox.information(self, "GrabLine", t("Statistics exported to {path}", path=path))

    def _export_settings(self) -> None:
        path, _f = QFileDialog.getSaveFileName(
            self, t("Export settings"), "grabline-settings.json", "JSON (*.json)"
        )
        if not path:
            return
        import json as _json

        from app.core.settings import sanitized_export

        # Never export secrets in plain text: API keys dropped, proxy creds redacted.
        payload = sanitized_export(self.settings.db.all_settings())
        with open(path, "w", encoding="utf-8") as handle:
            _json.dump(payload, handle, indent=2, sort_keys=True)
        QMessageBox.information(
            self,
            "GrabLine",
            t(
                "Settings exported to {path}\n(API keys and proxy credentials are not included.)",
                path=path,
            ),
        )

    def _import_settings(self) -> None:
        path, _f = QFileDialog.getOpenFileName(
            self, t("Import settings"), "", "JSON (*.json);;All files (*)"
        )
        if not path:
            return
        import json as _json

        try:
            with open(path, encoding="utf-8") as handle:
                payload = _json.load(handle)
            if not isinstance(payload, dict):
                raise ValueError("not a settings export")
            count = self.settings.db.import_settings({str(k): str(v) for k, v in payload.items()})
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "GrabLine", t("Could not import: {exc}", exc=exc))
            return
        QMessageBox.information(
            self,
            "GrabLine",
            t(
                "Imported {count} setting(s). Reopen Settings (or restart) to see them all.",
                count=count,
            ),
        )

    def _reset_settings(self) -> None:
        answer = QMessageBox.question(
            self,
            "GrabLine",
            t("Reset all settings to their defaults? Your downloads and history are not touched."),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.settings.reset()
        # Re-read every field so the page shows the defaults immediately, and
        # repaint - a reset that needs a restart to be believed isn't one.
        self._load_values()
        app = QApplication.instance()
        if isinstance(app, QApplication):
            theme.apply_theme(app, self.settings.theme, self.settings.accent_color)
        self.settings_reset.emit()

    def _vacuum_db(self) -> None:
        self.settings.db.vacuum()
        QMessageBox.information(self, "GrabLine", t("Database compacted."))

    def _check_db(self) -> None:
        verdict = self.settings.db.integrity_check()
        if verdict == "ok":
            QMessageBox.information(self, "GrabLine", t("Database check: OK."))
        else:
            QMessageBox.warning(
                self, "GrabLine", t("Database check reported:\n{verdict}", verdict=verdict)
            )

    def _copy_diagnostics(self) -> None:
        import platform
        import sys as _sys

        lines = [f"GrabLine {__version__}"]
        lines.append(f"OS: {platform.platform()}")
        lines.append(f"Python: {_sys.version.split()[0]}")
        for module, label in (
            ("yt_dlp", "yt-dlp"),
            ("PySide6", "PySide6"),
            ("libtorrent", "libtorrent"),
            ("httpx", "httpx"),
        ):
            try:
                imported = __import__(module)
                version = getattr(imported, "__version__", None) or getattr(
                    getattr(imported, "version", None), "__version__", "?"
                )
                lines.append(f"{label}: {version}")
            except (ImportError, AttributeError):  # optional dep absent/odd shape
                lines.append(f"{label}: not available")
        from app.core.ffmpeg import find_ffmpeg as _find

        lines.append(f"FFmpeg: {_find(self.settings) or 'not found'}")
        QGuiApplication.clipboard().setText("\n".join(lines))
        QMessageBox.information(self, "GrabLine", t("Diagnostics copied to the clipboard."))

    def _browse_sound(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            t("Completion sound"),
            "",
            "Sounds (*.wav *.oga *.ogg *.aiff *.mp3);;All files (*)",
        )
        if chosen:
            self.sound_file_edit.setText(chosen)

    def _manage_cloud_accounts(self) -> None:
        from app.core.credentials import CredentialStore
        from app.ui.cloud_dialog import CloudAccountsDialog

        with guard.single_flight(self._in_flight, "cloud") as go:
            if go:
                CloudAccountsDialog(CredentialStore(self.settings.db), self).exec()

    @staticmethod
    def _vpn_status_text() -> str:
        from app.core import net

        interfaces = net.active_vpn_interfaces()
        if interfaces:
            return t("VPN detected: active on {interfaces}.", interfaces=", ".join(interfaces))
        return t("VPN detected: none (no tunnel interface is up).")

    def _browse_torrent_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, t("Save torrents to"), self.torrent_dir_edit.text() or str(Path.home())
        )
        if chosen:
            self.torrent_dir_edit.setText(chosen)

    def _browse_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, t("Choose download folder"), self.folder_edit.text()
        )
        if chosen:
            self.folder_edit.setText(chosen)

    def _open_setup_wizard(self) -> None:
        """The step-by-step browser wizard from the first launch. Parent it to
        the button's window: these tab pages are re-hosted inside the embedded
        Settings view, so ``self`` is not in the visible widget tree."""
        from app.ui.setup_dialog import SetupDialog

        with guard.single_flight(self._in_flight, "setup") as go:
            if go:
                sender = self.sender()
                parent = sender.window() if isinstance(sender, QWidget) else self
                SetupDialog(parent, settings=self.settings).exec()

    def _check_updates_now(self) -> None:
        """Manual update check. The main window owns the checker; reach it via
        the button's window (see ``_open_setup_wizard`` for why not ``self``)."""
        sender = self.sender()
        window = sender.window() if isinstance(sender, QWidget) else None
        check = getattr(window, "check_for_updates", None)
        if callable(check):
            check(quiet=False)
        else:  # standalone dialog (tests): fall back to the releases page
            self._open_url(f"{_PROJECT_URL}/releases")

    def _pair_browsers(self) -> None:
        from app.native_host.install import install as install_host

        try:
            written = install_host()
        except OSError as exc:
            QMessageBox.warning(self, "GrabLine", t("Pairing failed: {exc}", exc=exc))
            return
        QMessageBox.information(
            self,
            "GrabLine",
            t(
                "Registered with {count} browser location(s).\n\n"
                "Now install (or reload) the GrabLine Connect extension in your "
                "browser. Its toolbar popup should say “connected”.",
                count=len(written),
            ),
        )

    def _refresh_ffmpeg_status(self) -> None:
        path = find_ffmpeg(self.settings)
        if path:
            self.ffmpeg_status.setText(t("Found: {path}", path=path))
            self.ffmpeg_button.setText(t("Reinstall"))
        else:
            self.ffmpeg_status.setText(t("Not found"))
            self.ffmpeg_button.setText(t("Install FFmpeg"))

    def _install_ffmpeg(self) -> None:
        if not guard.begin(self._in_flight, "ffmpeg"):
            return  # already downloading - a second click does nothing
        progress = QProgressDialog(t("Downloading FFmpeg…"), t("Hide"), 0, 0, self)
        progress.setWindowTitle("GrabLine")
        progress.setMinimumDuration(0)
        bar = QProgressBar(progress)
        bar.setRange(0, 0)
        bar.setTextVisible(False)  # the themed 5px bar has no room for "42%"
        progress.setBar(bar)
        installer = _FfmpegInstaller(self.settings.proxy)
        self._installer = installer

        def on_progress(received: int, total: object) -> None:
            if isinstance(total, int) and total > 0:
                progress.setMaximum(100)
                progress.setValue(int(received / total * 100))
            progress.setLabelText(t("Downloading FFmpeg… {size}", size=human_bytes(received)))

        def on_success(path: str) -> None:
            guard.end(self._in_flight, "ffmpeg")
            progress.close()
            self._refresh_ffmpeg_status()
            QMessageBox.information(
                self, "GrabLine", t("FFmpeg installed and verified:\n{path}", path=path)
            )

        def on_failure(message: str) -> None:
            guard.end(self._in_flight, "ffmpeg")
            progress.close()
            self._refresh_ffmpeg_status()
            QMessageBox.warning(self, "GrabLine", message)

        installer.progressed.connect(on_progress)
        installer.succeeded.connect(on_success)
        installer.failed.connect(on_failure)
        # The download can outlive the Settings dialog; retain() owns it so a
        # dialog close never destroys a running thread. See app/ui/threads.
        threads.retain(installer)
        installer.start()

    def _load_values(self) -> None:
        """Fill every field from the stored settings. Called once while
        building, and again after Reset so the page updates without a restart.
        The mirror image of ``apply()`` - keep the two in step."""
        s = self.settings

        # General. Autostart is an OS registration, not a stored setting, so it
        # reads back from the system and a reset leaves it alone.
        self.autostart_check.setChecked(launcher.autostart_enabled())
        self.updates_check.setChecked(s.check_updates)
        self.start_min_check.setChecked(s.start_minimized)
        self.tray_min_check.setChecked(s.minimize_to_tray)
        self.tray_close_check.setChecked(s.close_to_tray)
        self.confirm_exit_check.setChecked(s.confirm_exit_active)
        self.confirm_downloads_check.setChecked(s.confirm_downloads)
        self.new_dl_combo.setCurrentIndex(0 if s.auto_start_downloads else 1)

        # Downloads.
        self.folder_edit.setText(str(s.download_dir))
        self.categories_check.setChecked(s.categories_enabled)
        self.ask_save_check.setChecked(s.ask_save_dir)
        self.concurrent_spin.setValue(s.max_concurrent)
        self.free_mb_spin.setValue(s.min_free_mb)

        # Download engine.
        self.connections_spin.setValue(s.connections)
        self.speed_spin.setValue(s.speed_limit_kbps)
        self.fair_speed_check.setChecked(s.fair_speed)
        self.schedule_check.setChecked(s.speed_schedule_enabled)
        self.full_from.setTime(QTime.fromString(s.speed_full_from, "HH:mm"))
        self.full_to.setTime(QTime.fromString(s.speed_full_to, "HH:mm"))
        self.retry_check.setChecked(s.auto_retry)
        self.retry_spin.setValue(s.auto_retry_max)

        # Browser integration.
        self.browser_combo.setCurrentIndex(SESSION_BROWSERS.index(s.session_browser))
        self.clipboard_check.setChecked(s.clipboard_watcher)

        # Video downloader.
        self.playlist_cap_spin.setValue(s.playlist_batch_cap)
        self.hq_first_check.setChecked(s.video_hq_first)
        self.confirm_video_quality_check.setChecked(s.confirm_video_quality)
        self.default_quality_combo.setCurrentText(s.video_default_quality)
        self.bitrate_combo.setCurrentIndex(max(0, self.bitrate_combo.findData(s.audio_bitrate)))
        self.cookies_edit.setText(s.cookies_file)

        # Torrent.
        self.torrent_port_spin.setValue(s.torrent_port)
        self.dht_check.setChecked(s.torrent_dht)
        self.upnp_check.setChecked(s.torrent_upnp)
        self.natpmp_check.setChecked(s.torrent_natpmp)
        self.seed_check.setChecked(s.torrent_seed)
        self.ratio_spin.setValue(s.torrent_ratio_limit)
        self.upload_spin.setValue(s.torrent_upload_kbps)
        self.sequential_check.setChecked(s.torrent_sequential)
        self.torrent_dir_edit.setText(str(s.torrent_dir) if s.torrent_dir else "")
        self.search_url_edit.setText(s.torrent_search_url)
        self.rss_edit.setPlainText("\n".join(s.rss_feeds))
        self.rss_interval_spin.setValue(s.rss_interval_minutes)
        self.encryption_combo.setCurrentIndex(
            max(0, self.encryption_combo.findData(s.torrent_encryption))
        )
        self.seed_minutes_spin.setValue(s.torrent_seed_minutes)
        self.trackers_default_edit.setPlainText("\n".join(s.torrent_trackers))

        # Archive manager.
        self.extract_check.setChecked(s.auto_extract)
        self.scan_check.setChecked(s.scan_before_extract)
        self.passwords_edit.setPlainText("\n".join(s.archive_passwords))
        self.extract_subfolder_check.setChecked(s.extract_to_subfolder)
        self.delete_archive_check.setChecked(s.delete_archive_after_extract)

        # File management.
        self.favorites_edit.setPlainText("\n".join(s.favorite_folders))
        self.rename_edit.setPlainText(
            "\n".join(f"{find} -> {replace}" for find, replace in s.rename_rules)
        )
        self.default_tags_edit.setText(s.default_tags)

        # Queue manager.
        self.default_queue_combo.setCurrentIndex(
            max(0, self.default_queue_combo.findData(s.default_queue_id))
        )

        # Scheduler.
        self.download_schedule_check.setChecked(s.download_schedule_enabled)
        self.download_start.setTime(QTime.fromString(s.download_start, "HH:mm"))
        self.download_stop.setTime(QTime.fromString(s.download_stop, "HH:mm"))
        enabled_days = set(s.download_days)
        for index, check in enumerate(self.day_checks):
            check.setChecked(index in enabled_days)
        self.battery_check.setChecked(s.pause_on_battery)
        self.battery_pct_spin.setValue(s.battery_min_percent)
        self.network_check.setChecked(s.wait_for_network)
        self.after_combo.setCurrentIndex(max(0, self.after_combo.findData(s.after_queue_action)))

        # Network.
        self.proxy_edit.setText(s.proxy or "")
        self.auto_throttle_check.setChecked(s.auto_throttle)
        self.throttle_limit_spin.setValue(s.auto_throttle_kbps)
        self.throttle_threshold_spin.setValue(s.auto_throttle_threshold_kbps)
        self.host_limits_edit.setPlainText(
            "\n".join(f"{host} = {kbps}" for host, kbps in s.host_limits.items())
        )
        self.bypass_edit.setText(", ".join(s.proxy_bypass))
        self.ua_edit.setText(s.user_agent)

        # Security.
        self.scan_downloads_check.setChecked(s.scan_downloads)
        self.enforce_https_check.setChecked(s.enforce_https)
        self.virustotal_edit.setText(s.virustotal_key)
        self.safebrowsing_edit.setText(s.safebrowsing_key)
        self.scanner_combo.setCurrentIndex(max(0, self.scanner_combo.findData(s.scanner_pref)))
        self.scan_ext_edit.setText(s.scan_extensions)

        # Notifications.
        self.notify_check.setChecked(s.notify_on_complete)
        self.sound_check.setChecked(s.sound_on_complete)
        self.sound_file_edit.setText(s.sound_file)
        self.open_folder_check.setChecked(s.auto_open_folder)
        self.notify_failed_check.setChecked(s.notify_on_failed)
        self.notify_queue_check.setChecked(s.notify_queue_done)
        self.toast_spin.setValue(s.toast_seconds)
        self.quiet_check.setChecked(s.quiet_enabled)
        self.quiet_from_edit.setTime(QTime.fromString(s.quiet_from, "HH:mm"))
        self.quiet_to_edit.setTime(QTime.fromString(s.quiet_to, "HH:mm"))

        # Statistics.
        self.stats_enabled_check.setChecked(s.stats_enabled)
        self.retention_spin.setValue(s.stats_retention_days)
        self.refresh_spin.setValue(s.dashboard_refresh_ms)

        # Language: the saved choice, or the active one when none is stored yet.
        self.language_combo.setCurrentIndex(
            max(0, self.language_combo.findData(s.language or i18n.current_language()))
        )

        # Appearance.
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(s.theme)))
        self.accent_combo.setCurrentIndex(max(0, self.accent_combo.findData(s.accent_color)))
        self.density_combo.setCurrentIndex(max(0, self.density_combo.findData(s.ui_density)))
        hidden_now = set(s.hidden_columns)
        for key, check in self.column_checks.items():
            check.setChecked(key not in hidden_now)

        # Advanced.
        self.script_edit.setText(s.script_on_complete)
        self.ffmpeg_override_edit.setText(s.ffmpeg_path or "")
        self.log_combo.setCurrentIndex(max(0, self.log_combo.findData(s.log_level)))
        self.logfile_check.setChecked(s.log_to_file)

    def _save(self) -> None:
        if self.apply():
            self.accept()

    def apply(self) -> bool:
        """Persist every field. Returns False (without saving) if the proxy is
        malformed. Shared by the modal Save button and the embedded page."""
        from app.core import net

        proxy_error = net.validate_proxy(self.proxy_edit.text())
        if proxy_error is not None:
            QMessageBox.warning(self, "GrabLine", proxy_error)
            return False
        language = str(self.language_combo.currentData())
        language_changed = language != i18n.current_language()
        self.settings.language = language
        self.settings.download_dir = self.folder_edit.text().strip() or str(
            self.settings.download_dir
        )
        self.settings.categories_enabled = self.categories_check.isChecked()
        self.settings.clipboard_watcher = self.clipboard_check.isChecked()
        self.settings.session_browser = self.browser_combo.currentData()
        self.settings.max_concurrent = self.concurrent_spin.value()
        self.settings.connections = self.connections_spin.value()
        self.settings.speed_limit_kbps = self.speed_spin.value()
        self.settings.fair_speed = self.fair_speed_check.isChecked()
        self.settings.speed_schedule_enabled = self.schedule_check.isChecked()
        self.settings.speed_full_from = self.full_from.time().toString("HH:mm")
        self.settings.speed_full_to = self.full_to.time().toString("HH:mm")
        self.settings.download_schedule_enabled = self.download_schedule_check.isChecked()
        self.settings.download_start = self.download_start.time().toString("HH:mm")
        self.settings.download_stop = self.download_stop.time().toString("HH:mm")
        self.settings.check_updates = self.updates_check.isChecked()
        self.settings.auto_retry = self.retry_check.isChecked()
        self.settings.auto_retry_max = self.retry_spin.value()
        self.settings.download_days = [
            index for index, check in enumerate(self.day_checks) if check.isChecked()
        ]
        self.settings.pause_on_battery = self.battery_check.isChecked()
        self.settings.wait_for_network = self.network_check.isChecked()
        self.settings.sound_on_complete = self.sound_check.isChecked()
        self.settings.sound_file = self.sound_file_edit.text()
        self.settings.script_on_complete = self.script_edit.text()
        self.settings.theme = self.theme_combo.currentData()
        self.settings.proxy = self.proxy_edit.text().strip() or None
        self.settings.auto_throttle = self.auto_throttle_check.isChecked()
        self.settings.auto_throttle_kbps = self.throttle_limit_spin.value()
        self.settings.auto_throttle_threshold_kbps = self.throttle_threshold_spin.value()
        self.settings.host_limits = _parse_host_limits(self.host_limits_edit.toPlainText())
        self.settings.notify_on_complete = self.notify_check.isChecked()
        self.settings.auto_open_folder = self.open_folder_check.isChecked()
        self.settings.auto_extract = self.extract_check.isChecked()
        self.settings.scan_before_extract = self.scan_check.isChecked()
        self.settings.scan_downloads = self.scan_downloads_check.isChecked()
        self.settings.enforce_https = self.enforce_https_check.isChecked()
        self.settings.virustotal_key = self.virustotal_edit.text()
        self.settings.safebrowsing_key = self.safebrowsing_edit.text()
        self.settings.archive_passwords = self.passwords_edit.toPlainText().splitlines()
        self.settings.favorite_folders = self.favorites_edit.toPlainText().splitlines()
        self.settings.rename_rules = _parse_rename_rules(self.rename_edit.toPlainText())
        self.settings.playlist_batch_cap = self.playlist_cap_spin.value()
        self.settings.video_hq_first = self.hq_first_check.isChecked()
        self.settings.confirm_video_quality = self.confirm_video_quality_check.isChecked()
        self.settings.start_minimized = self.start_min_check.isChecked()
        self.settings.minimize_to_tray = self.tray_min_check.isChecked()
        self.settings.close_to_tray = self.tray_close_check.isChecked()
        self.settings.confirm_exit_active = self.confirm_exit_check.isChecked()
        self.settings.confirm_downloads = self.confirm_downloads_check.isChecked()
        self.settings.auto_start_downloads = bool(self.new_dl_combo.currentData())
        self.settings.ask_save_dir = self.ask_save_check.isChecked()
        self.settings.min_free_mb = self.free_mb_spin.value()
        self.settings.video_default_quality = self.default_quality_combo.currentText()
        self.settings.audio_bitrate = self.bitrate_combo.currentData()
        self.settings.cookies_file = self.cookies_edit.text()
        self.settings.torrent_encryption = self.encryption_combo.currentData()
        self.settings.torrent_seed_minutes = self.seed_minutes_spin.value()
        self.settings.torrent_trackers = self.trackers_default_edit.toPlainText().splitlines()
        self.settings.extract_to_subfolder = self.extract_subfolder_check.isChecked()
        self.settings.delete_archive_after_extract = self.delete_archive_check.isChecked()
        self.settings.default_tags = self.default_tags_edit.text()
        self.settings.default_queue_id = int(self.default_queue_combo.currentData() or 0)
        self.settings.battery_min_percent = self.battery_pct_spin.value()
        self.settings.proxy_bypass = [h for h in self.bypass_edit.text().split(",") if h.strip()]
        self.settings.user_agent = self.ua_edit.text()
        self.settings.scanner_pref = self.scanner_combo.currentData()
        self.settings.scan_extensions = self.scan_ext_edit.text()
        self.settings.notify_on_failed = self.notify_failed_check.isChecked()
        self.settings.notify_queue_done = self.notify_queue_check.isChecked()
        self.settings.toast_seconds = self.toast_spin.value()
        self.settings.quiet_enabled = self.quiet_check.isChecked()
        self.settings.quiet_from = self.quiet_from_edit.time().toString("HH:mm")
        self.settings.quiet_to = self.quiet_to_edit.time().toString("HH:mm")
        self.settings.stats_enabled = self.stats_enabled_check.isChecked()
        self.settings.stats_retention_days = self.retention_spin.value()
        self.settings.dashboard_refresh_ms = self.refresh_spin.value()
        self.settings.accent_color = self.accent_combo.currentData()
        self.settings.ui_density = self.density_combo.currentData()
        self.settings.hidden_columns = [
            key for key, check in self.column_checks.items() if not check.isChecked()
        ]
        self.settings.log_level = self.log_combo.currentData()
        self.settings.log_to_file = self.logfile_check.isChecked()
        self.settings.ffmpeg_path = self.ffmpeg_override_edit.text().strip() or None
        self.settings.torrent_port = self.torrent_port_spin.value()
        self.settings.torrent_dht = self.dht_check.isChecked()
        self.settings.torrent_upnp = self.upnp_check.isChecked()
        self.settings.torrent_natpmp = self.natpmp_check.isChecked()
        self.settings.torrent_seed = self.seed_check.isChecked()
        self.settings.torrent_ratio_limit = self.ratio_spin.value()
        self.settings.torrent_upload_kbps = self.upload_spin.value()
        self.settings.torrent_sequential = self.sequential_check.isChecked()
        self.settings.torrent_dir = self.torrent_dir_edit.text().strip() or None
        self.settings.torrent_search_url = self.search_url_edit.text()
        self.settings.rss_feeds = self.rss_edit.toPlainText().splitlines()
        self.settings.rss_interval_minutes = self.rss_interval_spin.value()
        self.settings.after_queue_action = self.after_combo.currentData()
        self.settings.shortcuts = self._shortcuts_page.overrides()
        try:
            # The autostart file/registry entry IS the setting - no DB copy
            # that could drift from what the OS will actually do at login.
            launcher.set_autostart(self.autostart_check.isChecked())
        except OSError as exc:
            QMessageBox.warning(self, "GrabLine", t("Could not update autostart: {exc}", exc=exc))
        if language_changed:
            # Offer an immediate restart so the new catalog + layout direction
            # apply; "Later" keeps the current UI until the next launch.
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Information)
            box.setWindowTitle("GrabLine")
            box.setText(t("Restart GrabLine to apply the new language."))
            restart_btn = box.addButton(t("Restart now"), QMessageBox.ButtonRole.AcceptRole)
            box.addButton(t("Later"), QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is restart_btn:
                if launcher.restart_current():
                    app = QApplication.instance()
                    if app is not None:
                        app.quit()
                else:
                    QMessageBox.warning(
                        self,
                        "GrabLine",
                        t("Could not restart GrabLine. Please quit and open it again."),
                    )
        return True
