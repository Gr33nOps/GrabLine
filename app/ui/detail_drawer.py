"""The Downloads detail drawer: a 324px "download inspector" beside the table
when a single download is selected.

It is a persistent header (icon, name, status, progress, and a compact
per-connection bar strip for every download kind) over a tab strip -
Overview / Details / Media (or Contents / Peers) - and a compact action bar.
Tabs that don't apply to the selection are hidden, so a PDF shows two and a
video shows three. Widgets are built once; only their text and visibility change
as the selection or live progress updates, so nothing flickers. The expensive
reads (the full Job row, timestamps, an ffprobe of a media file, an archive's
entry list) happen only when the selection changes, never on the 0.5s tick.

Repeated facts live in one place: Overview holds live or session summary,
Details holds file path and source, Media holds codec metadata. Actions that
already exist on the row context menu (redownload, copy URL/path/hash) stay
off the footer - Open, Folder, Rename, and Remove are enough here.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.core import archive
from app.core.i18n import N_, t
from app.core.manager import DownloadManager, JobView
from app.core.mediainfo import MediaSummary, read_media_info
from app.core.models import Job, JobKind, JobStatus, Segment
from app.ui import components, design, motion, theme, threads
from app.ui.format import duration_text, human_bytes
from app.ui.icons import svg_icon, type_icon_name
from app.ui.work_threads import FileOpThread

log = logging.getLogger(__name__)

#: How many per-connection bars the header shows. More than this gets noisy in
#: a 324px drawer; extras still run, they just aren't listed individually.
_MAX_CONN_BARS = 8

#: Fixed label column so every bar ends on the same vertical line - a growing
#: "Conn 1 · 256.0 KB/s" used to shove its neighbours and clip the drawer edge.
_CONN_LABEL_WIDTH = 100


@dataclass(frozen=True)
class _ConnLane:
    """One row in the connection strip: fill 0..1, optional live speed."""

    index: int
    fraction: float
    speed_bps: float = 0.0
    key: str = ""


def _waterfall_lanes(
    count: int, fraction: float, *, active_speed: float = 0.0
) -> list[_ConnLane]:
    """Split overall progress across ``count`` lanes (YouTube / torrent / HLS
    don't expose real byte-ranges, but the strip still reads as multi-lane)."""
    count = max(1, min(_MAX_CONN_BARS, count))
    fraction = max(0.0, min(1.0, fraction))
    slot = 1.0 / count
    lanes: list[_ConnLane] = []
    for i in range(count):
        start = i * slot
        end = start + slot
        if fraction >= end - 1e-9:
            fill, speed = 1.0, 0.0
        elif fraction <= start:
            fill, speed = 0.0, 0.0
        else:
            fill = (fraction - start) / slot
            speed = active_speed
        lanes.append(_ConnLane(index=i + 1, fraction=fill, speed_bps=speed, key=f"w{i}"))
    return lanes


class _ConnectionBars(QWidget):
    """Thin per-connection progress rows under the main bar (IDM-style).

    Built once with a fixed row budget; each refresh only updates fill + label
    text so the header never rebuilds widgets mid-download. Labels are a fixed
    width so bars stay level; long speed text is elided instead of overflowing.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 2, 0, 0)
        root.setSpacing(3)
        self._title = components.role_label(
            t("Connections").upper(), "caption", size=design.FONT["caption"]
        )
        root.addWidget(self._title)
        self._rows: list[tuple[QWidget, QLabel, motion.SmoothProgressBar]] = []
        self._prev: dict[str, tuple[float, float]] = {}
        #: Whose lanes are on screen, so a parked download doesn't get
        #: re-queried twice a second for an unchanging answer.
        self._job_id: int | None = None
        self._lane_count = 0
        for _ in range(_MAX_CONN_BARS):
            row = QWidget()
            row.setFixedHeight(12)
            lay = QHBoxLayout(row)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(6)
            bar = motion.SmoothProgressBar()
            bar.setFixedHeight(3)
            bar.setMinimumWidth(24)
            bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            label = components.role_label("", "muted", size=design.FONT["caption"])
            label.setFixedWidth(_CONN_LABEL_WIDTH)
            label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
            lay.addWidget(bar, 1)
            lay.addWidget(label, 0)
            root.addWidget(row)
            row.hide()
            self._rows.append((row, label, bar))
        self.hide()

    def shows(self, job_id: int) -> bool:
        return self._job_id == job_id and self._lane_count > 0

    def note_job(self, job_id: int) -> None:
        self._job_id = job_id

    def clear(self) -> None:
        self._prev.clear()
        self._job_id = None
        self._lane_count = 0
        for row, label, bar in self._rows:
            row.hide()
            label.setText("")
            bar.set_value(0.0, immediate=True)
        self.hide()

    def update_lanes(
        self,
        lanes: Sequence[_ConnLane],
        *,
        status: JobStatus,
        color: str | None,
    ) -> None:
        if not lanes:
            self.clear()
            return
        shown = list(lanes)[:_MAX_CONN_BARS]
        self._lane_count = len(shown)
        live_keys = {lane.key or str(lane.index) for lane in shown}
        for key in list(self._prev):
            if key not in live_keys:
                del self._prev[key]

        metrics = QFontMetrics(self._rows[0][1].font())
        for index, (row, label, bar) in enumerate(self._rows):
            if index >= len(shown):
                row.hide()
                continue
            lane = shown[index]
            bar.set_color(color)
            bar.set_value(
                max(0.0, min(1.0, lane.fraction)),
                immediate=(status is not JobStatus.DOWNLOADING),
            )
            # Speeds come from the caller (segment byte deltas or overall
            # waterfall rate). Don't write _prev here - update_segments owns
            # those keys as byte counters.
            speed_text = ""
            if status is JobStatus.DOWNLOADING and lane.speed_bps > 0:
                speed_text = f" · {motion.fmt_speed(lane.speed_bps)}"
            text = t("Conn {n}{speed}", n=lane.index, speed=speed_text)
            label.setText(metrics.elidedText(text, Qt.TextElideMode.ElideRight, _CONN_LABEL_WIDTH))
            row.show()
        self.show()

    def update_segments(
        self,
        segments: Sequence[Segment],
        *,
        status: JobStatus,
        color: str | None,
        overall_speed: float = 0.0,
    ) -> None:
        """Map real byte-range segments onto lanes (direct downloads)."""
        if not segments:
            self.clear()
            return
        ordered = list(segments)
        if status is JobStatus.DOWNLOADING:
            ordered.sort(key=lambda s: (s.is_complete, s.index))
        shown = ordered[:_MAX_CONN_BARS]
        now = time.monotonic()
        lanes: list[_ConnLane] = []
        for seg in shown:
            size = (seg.end - seg.start + 1) if seg.end is not None else None
            if status is JobStatus.COMPLETED and size:
                fraction = 1.0
            elif size and size > 0:
                fraction = min(1.0, seg.downloaded / size)
            else:
                fraction = 0.0
            speed = 0.0
            key = f"s{seg.id}"
            if status is JobStatus.DOWNLOADING:
                prev = self._prev.get(key)
                self._prev[key] = (float(seg.downloaded), now)
                if prev is not None:
                    prev_bytes, prev_at = prev
                    dt = now - prev_at
                    if dt > 0.05:
                        speed = max(0.0, (seg.downloaded - prev_bytes) / dt)
            lanes.append(
                _ConnLane(index=seg.index + 1, fraction=fraction, speed_bps=speed, key=key)
            )
        # If every segment is quiet but the job is moving, put the overall
        # speed on the first incomplete lane so the strip isn't blank.
        if status is JobStatus.DOWNLOADING and overall_speed > 0 and not any(
            lane.speed_bps > 0 for lane in lanes
        ):
            for i, lane in enumerate(lanes):
                if lane.fraction < 1.0:
                    lanes[i] = _ConnLane(lane.index, lane.fraction, overall_speed, lane.key)
                    break
        self.update_lanes(lanes, status=status, color=color)


#: Long URLs and paths have no spaces, so a word-wrapping label can't break
#: them - it demands its full width and pushes the panel past the edge. Insert
#: zero-width spaces after the usual boundaries AND inside any long unbroken run
#: so the label can wrap. The value the user sees is unchanged; copy actions use
#: the real url/path, not this.
_BREAK_AFTER = re.compile(r"([/\\._\-?&=:@])")
_LONG_RUN = re.compile(r"(\S{18})")

#: extension -> human category, for the "Type" line ("MP4 Video").
_CATEGORY = {
    "t-video": N_("Video"),
    "t-audio": N_("Audio"),
    "t-image": N_("Image"),
    "t-document": N_("Document"),
    "t-archive": N_("Archive"),
    "t-program": N_("Program"),
    "t-game": N_("Game"),
}
_VIDEO_EXT = {"mp4", "mkv", "webm", "mov", "avi", "m4v"}
_AUDIO_EXT = {"mp3", "m4a", "flac", "wav", "ogg", "opus", "aac", "m4b"}
#: type_icon_name() falls back to "t-document" for anything it doesn't know, so
#: the "Document" label is only trustworthy for these real document extensions.
_DOC_EXT = {"pdf", "doc", "docx", "txt", "epub"}

#: Every stat-grid caption and section title, listed as literals so the string
#: extractor sees them - the grids and _group() translate their keys via
#: t(<variable>), which the extractor can't follow. Keep in sync with the grids
#: built in _build_pages().
_CAPTIONS = (
    N_("Speed"),
    N_("Average"),
    N_("Peak"),
    N_("ETA"),
    N_("Downloaded"),
    N_("Remaining"),
    N_("Finished"),
    N_("Elapsed"),
    N_("Retries"),
    N_("Type"),
    N_("Size on disk"),
    N_("Location"),
    N_("Added"),
    N_("Server"),
    N_("Final server"),
    N_("Protocol"),
    N_("Resume"),
    N_("Connections"),
    N_("Segments"),
    N_("Priority"),
    N_("Queue"),
    N_("Speed limit"),
    N_("Resolution"),
    N_("Duration"),
    N_("FPS"),
    N_("Video"),
    N_("Audio"),
    N_("Container"),
    N_("Files"),
    N_("Uncompressed"),
    N_("Status"),
    N_("Seeds"),
    N_("Peers"),
    N_("Down speed"),
    N_("Up speed"),
    N_("Uploaded"),
    N_("Ratio"),
    N_("File"),
    N_("Source"),
)


def _wrappable(text: str) -> str:
    text = _BREAK_AFTER.sub("\\1​", text)
    return _LONG_RUN.sub("\\1​", text)


def _type_label(filename: str) -> str:
    """ "Me.mp4" -> "MP4 Video"; unknown -> "BIN file"; no extension -> "File"."""
    ext = Path(filename).suffix.lower().lstrip(".")
    if not ext:
        return t("File")
    icon = type_icon_name(ext)
    category = _CATEGORY.get(icon)
    if icon == "t-document" and ext not in _DOC_EXT:
        category = None  # an unknown type, not a real document
    if category:
        return f"{ext.upper()} {t(category)}"
    return t("{ext} file", ext=ext.upper())


def _fmt_datetime(stamp: str | None) -> str:
    """SQLite's UTC 'YYYY-MM-DD HH:MM:SS' -> local '22 Jul 2026, 14:30'."""
    if not stamp:
        return ""
    try:
        parsed = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return stamp
    return parsed.astimezone().strftime("%d %b %Y, %H:%M")


def _elapsed(created: str | None, finished: str | None) -> str:
    if not (created and finished):
        return ""
    try:
        start = datetime.strptime(created, "%Y-%m-%d %H:%M:%S")
        end = datetime.strptime(finished, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""
    seconds = (end - start).total_seconds()
    return duration_text(seconds) if seconds >= 1 else "under a second"


def _swarm(connected: int, swarm: int) -> str:
    """ "12 of 340" when the tracker has reported the swarm size, else just the
    count we're connected to (libtorrent reports -1 before the first scrape)."""
    return (
        t("{connected} of {swarm}", connected=connected, swarm=swarm)
        if swarm >= 0
        else str(connected)
    )


class _StatGrid(QWidget):
    """A two-column caption/value list with a fixed, pre-built set of rows. A row
    with an empty value hides itself, so a grid only ever shows the facts it has
    - no blank placeholders. Updating text never rebuilds a widget (no flicker)."""

    def __init__(self, keys: Sequence[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(7)
        grid.setColumnStretch(1, 1)
        self._rows: dict[str, tuple[QLabel, QLabel]] = {}
        for index, key in enumerate(keys):
            # The key is a stable English identifier used for lookups; the shown
            # caption is its translation, upper-cased (a no-op for CJK scripts).
            caption = components.role_label(t(key).upper(), "caption", size=design.FONT["caption"])
            caption.setAlignment(Qt.AlignmentFlag.AlignTop)
            value = components.role_label("", "value", size=design.FONT["small"])
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            grid.addWidget(caption, index, 0)
            grid.addWidget(value, index, 1)
            self._rows[key] = (caption, value)

    def set(self, key: str, value: str) -> None:
        caption, label = self._rows[key]
        label.setText(value)
        caption.setVisible(bool(value))
        label.setVisible(bool(value))

    def clear(self) -> None:
        for key in self._rows:
            self.set(key, "")

    def any_visible(self) -> bool:
        # isHidden() reads the flag set() toggles, not "is on screen" - so this
        # is right even when the grid is on a stacked page that isn't current.
        return any(not label.isHidden() for _c, label in self._rows.values())


class DetailDrawer(QFrame):
    def __init__(
        self,
        manager: DownloadManager,
        *,
        ffmpeg: str | None,
        on_open_file: Callable[[JobView], None],
        on_open_folder: Callable[[JobView], None],
        on_rename: Callable[[JobView], None],
        on_remove: Callable[[JobView], None],
        on_extract: Callable[[JobView], None],
        on_security: Callable[[JobView], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.manager = manager
        self._ffmpeg = ffmpeg
        self._view: JobView | None = None
        self._job: Job | None = None
        self._times: tuple[str | None, str | None] = (None, None)
        #: (path, status) the action buttons were last decided from, and the
        #: filesystem answer for it - see _update_actions.
        self._actions_key: tuple[str, JobStatus] | None = None
        self._file_present = False
        #: This download's speed trail, as running totals rather than a list of
        #: samples: the drawer ticks several times a second for as long as the
        #: download lasts, so keeping every sample meant an ever-growing list
        #: that was re-summed and re-scanned on every one of those ticks.
        self._samples = 0
        self._speed_total = 0.0
        self._peak = 0.0
        #: Bumped every selection change; a late ffprobe result for an older
        #: selection is dropped rather than written to the wrong file's tab.
        self._probe_gen = 0
        self._on = {
            "open": on_open_file,
            "folder": on_open_folder,
            "rename": on_rename,
            "remove": on_remove,
            "extract": on_extract,
            "security": on_security,
        }
        self.setObjectName("Drawer")
        self.setFixedWidth(324)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())
        root.addWidget(self._build_tabstrip())
        root.addWidget(self._build_pages(), 1)
        root.addWidget(self._build_footer())

        self._select_tab(0)
        self.hide()

    # ----------------------------------------------------------- construction

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("DrawerHeader")
        outer = QVBoxLayout(header)
        outer.setContentsMargins(14, 10, 10, 10)
        outer.setSpacing(9)

        title_row = QHBoxLayout()
        title_row.addWidget(
            components.role_label(t("DETAILS"), "caption", size=design.FONT["caption"])
        )
        title_row.addStretch(1)
        close = components.IconButton("cancel", "")
        close.clicked.connect(self.hide)
        title_row.addWidget(close)
        outer.addLayout(title_row)

        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        self._icon = QLabel()
        self._icon.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._icon.setFixedWidth(18)
        self._name = components.role_label("", "strong", size=design.FONT["h2"], bold=True)
        self._name.setWordWrap(True)
        name_row.addWidget(self._icon)
        name_row.addWidget(self._name, 1)
        outer.addLayout(name_row)

        pill_row = QHBoxLayout()
        self._pill = components.StatusPill("queued")
        pill_row.addWidget(self._pill)
        pill_row.addStretch(1)
        outer.addLayout(pill_row)

        prow = QHBoxLayout()
        prow.setSpacing(8)
        self._progress = motion.SmoothProgressBar()
        self._percent = components.role_label("", "muted", size=design.FONT["small"])
        self._percent.setMinimumWidth(30)
        self._percent.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        prow.addWidget(self._progress, 1)
        prow.addWidget(self._percent, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addLayout(prow)

        self._size = components.role_label("", "muted", size=design.FONT["small"])
        outer.addWidget(self._size)
        self._conn_bars = _ConnectionBars()
        outer.addWidget(self._conn_bars)
        return header

    def _build_tabstrip(self) -> QWidget:
        strip = QFrame()
        strip.setObjectName("DrawerTabs")
        lay = QHBoxLayout(strip)
        lay.setContentsMargins(10, 0, 10, 0)
        lay.setSpacing(2)
        self._tab_group = QButtonGroup(self)
        self._tab_group.setExclusive(True)
        self._tabs: list[QPushButton] = []
        # Media is kind-adaptive (Media / Contents / Peers); Activity was folded
        # into Overview so finished downloads aren't a thin fourth page of
        # timestamps that Details already shows.
        tab_labels = (N_("Overview"), N_("Details"), N_("Media"))
        for index, label in enumerate(tab_labels):
            btn = QPushButton(t(label))
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setProperty("role", "tab")
            btn.clicked.connect(lambda _c=False, i=index: self._select_tab(i))
            self._tab_group.addButton(btn, index)
            self._tabs.append(btn)
            lay.addWidget(btn)
        lay.addStretch(1)
        return strip

    def _scroll_page(self) -> tuple[QScrollArea, QVBoxLayout]:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(12)
        scroll.setWidget(body)
        return scroll, lay

    def _group(self, layout: QVBoxLayout, title: str, grid: _StatGrid) -> None:
        layout.addWidget(
            components.role_label(t(title).upper(), "caption", size=design.FONT["caption"])
        )
        layout.addWidget(grid)

    def _build_pages(self) -> QWidget:
        self._pages = QStackedWidget()

        # -- Overview --------------------------------------------------------
        over, olay = self._scroll_page()
        self._overview = _StatGrid(
            (
                "Speed",
                "Average",
                "Peak",
                "ETA",
                "Downloaded",
                "Remaining",
                "Finished",
                "Elapsed",
                "Retries",
            )
        )
        olay.addWidget(self._overview)
        self._spark = motion.Sparkline()
        self._spark.setFixedHeight(52)
        self._spark_card = components.card_frame()
        sc = QVBoxLayout(self._spark_card)
        sc.setContentsMargins(11, 9, 11, 9)
        sc.setSpacing(4)
        sc.addWidget(
            components.role_label(t("SPEED · LAST 30s"), "caption", size=design.FONT["caption"])
        )
        sc.addWidget(self._spark)
        self._spark_val = components.role_label("", "accent", size=design.FONT["h2"], bold=True)
        self._spark_val.setAlignment(Qt.AlignmentFlag.AlignRight)
        sc.addWidget(self._spark_val)
        olay.addWidget(self._spark_card)
        self._ov_error = self._long_block(olay, N_("Error"))
        self._ov_notes = self._long_block(olay, N_("Notes"))
        self._tags_box = QWidget()
        self._tags_layout = QHBoxLayout(self._tags_box)
        self._tags_layout.setContentsMargins(0, 0, 0, 0)
        self._tags_layout.setSpacing(4)
        olay.addWidget(self._tags_box)
        self._security_btn = components.IconButton(
            "shield",
            t("Security check…"),
            tooltip=t("Scan the file with the configured security tools"),
        )
        self._security_btn.clicked.connect(lambda: self._fire("security"))
        olay.addWidget(self._security_btn)
        olay.addStretch(1)
        self._pages.addWidget(over)

        # -- Details ---------------------------------------------------------
        det, dlay = self._scroll_page()
        self._file_grid = _StatGrid(("Type", "Size on disk", "Location", "Added"))
        self._group(dlay, "File", self._file_grid)
        self._source_grid = _StatGrid(
            (
                "Server",
                "Final server",
                "Protocol",
                "Resume",
                "Connections",
                "Segments",
                "Priority",
                "Queue",
                "Speed limit",
            )
        )
        self._group(dlay, "Source", self._source_grid)
        self._det_url = self._long_block(dlay, N_("URL"))
        dlay.addStretch(1)
        self._pages.addWidget(det)

        # -- Media / Contents / Peers ----------------------------------------
        med, mlay = self._scroll_page()
        self._media_title = components.role_label(
            t("MEDIA"), "caption", size=design.FONT["caption"]
        )
        mlay.addWidget(self._media_title)
        self._media_status = components.role_label("", "muted", size=design.FONT["small"])
        mlay.addWidget(self._media_status)
        self._media_grid = _StatGrid(
            (
                "Resolution",
                "Duration",
                "FPS",
                "Video",
                "Audio",
                "Container",
                "Files",
                "Uncompressed",
            )
        )
        mlay.addWidget(self._media_grid)
        # The same third page carries a torrent's live swarm stats (Peers).
        self._peers_grid = _StatGrid(
            (
                "Status",
                "Seeds",
                "Peers",
                "Down speed",
                "Up speed",
                "Downloaded",
                "Uploaded",
                "Ratio",
            )
        )
        mlay.addWidget(self._peers_grid)
        self._extract_btn = QPushButton(t("Extract here"))
        self._extract_btn.setProperty("accent", "true")
        self._extract_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._extract_btn.clicked.connect(lambda: self._fire("extract"))
        mlay.addWidget(self._extract_btn)
        mlay.addStretch(1)
        self._pages.addWidget(med)

        return self._pages

    def _long_block(self, layout: QVBoxLayout, caption: str) -> QLabel:
        """A full-width caption + wrapping value, for URL / location / errors."""
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(
            components.role_label(t(caption).upper(), "caption", size=design.FONT["caption"])
        )
        value = components.role_label("", "value", size=design.FONT["small"])
        value.setWordWrap(True)
        value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(value)
        layout.addWidget(box)
        box.setVisible(False)
        return value

    def _build_footer(self) -> QWidget:
        footer = QFrame()
        footer.setObjectName("DrawerFooter")
        grid = QGridLayout(footer)
        grid.setContentsMargins(10, 8, 10, 8)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)
        # Essentials only. Redownload / copy URL / path / hash live on the
        # table's right-click menu so the drawer stays a short action strip.
        specs = (
            ("open", "open", t("Open"), t("Open the file"), False),
            ("folder", "folder", t("Folder"), t("Open the containing folder"), False),
            ("rename", "rename", t("Rename"), t("Rename the file"), False),
            ("remove", "trash", t("Remove"), t("Remove from the list (file stays on disk)"), True),
        )
        self._act_btns: dict[str, components.IconButton] = {}
        for index, (key, icon, label, tip, danger) in enumerate(specs):
            btn = components.IconButton(icon, label, danger=danger, tooltip=tip)
            btn.clicked.connect(lambda _c=False, k=key: self._fire(k))
            grid.addWidget(btn, index // 2, index % 2)
            self._act_btns[key] = btn
        return footer

    # -------------------------------------------------------------- behaviour

    def _select_tab(self, index: int) -> None:
        self._pages.setCurrentIndex(index)
        button = self._tab_group.button(index)
        if button is not None:
            button.setChecked(True)

    def _fire(self, key: str) -> None:
        if self._view is not None:
            self._on[key](self._view)

    def current_id(self) -> int | None:
        return self._view.id if self._view is not None else None

    def show_view(
        self, view: JobView, speed_bps: float, history: Iterable[float] | None = None
    ) -> None:
        new = self._view is None or self._view.id != view.id
        if new:
            trail = list(history or ())
            self._spark.set_samples(trail)
            self._conn_bars.clear()
            self._samples = len(trail)
            self._speed_total = sum(trail)
            self._peak = max(trail, default=0.0)
        self._view = view
        self._update(view, speed_bps, new)
        self.show()

    def retint(self) -> None:
        for btn in self._act_btns.values():
            btn.retint()
        self._security_btn.retint()

    # ----------------------------------------------------------------- update

    def _update(self, view: JobView, speed_bps: float, new: bool) -> None:
        palette = theme.current()
        if new:
            self._probe_gen += 1
            self._job = self.manager.db.get_job(view.id)
            self._times = self.manager.db.job_timestamps(view.id)
            self._fill_static(view, palette)
            self._start_probe(view)
        # live header
        self._pill.set_status(view.status.value)
        self._progress.set_color(design.status_color(palette, view.status.value))
        self._update_progress(view)
        self._update_connections(view, palette, speed_bps)
        self._update_overview(view, speed_bps, new)
        # A torrent's swarm changes constantly, so refresh the Peers tab live
        # (the initial fill + visibility decision happened in _start_probe).
        if not new and view.kind is JobKind.TORRENT and not self._tabs[2].isHidden():
            self._update_peers(view)
        self._update_actions(view)

    def _fill_static(self, view: JobView, palette: design.Palette) -> None:
        kind = view.kind.value if view.kind.value in ("torrent", "cloud") else view.filename
        self._icon.setPixmap(svg_icon(type_icon_name(kind), palette.accent).pixmap(16, 16))
        self._name.setText(view.display_name)

        path = Path(view.dest_dir) / view.filename
        created, _updated = self._times

        # Details -> File (identity + path; finished time lives on Overview)
        self._file_grid.clear()
        self._file_grid.set("Type", _type_label(view.filename))
        self._file_grid.set("Size on disk", self._disk_size(path))
        self._file_grid.set("Location", _wrappable(view.dest_dir))
        self._file_grid.set("Added", _fmt_datetime(created))

        # Details -> Source
        self._source_grid.clear()
        self._source_grid.set("Server", urlsplit(view.url).hostname or "")
        final_host = (
            urlsplit(self._job.final_url).hostname if self._job and self._job.final_url else ""
        )
        if final_host and final_host != (urlsplit(view.url).hostname or ""):
            self._source_grid.set("Final server", final_host)
        self._source_grid.set("Protocol", (urlsplit(view.url).scheme or "").upper())
        if view.kind in (JobKind.DIRECT, JobKind.SMART, JobKind.HLS) and self._job is not None:
            self._source_grid.set("Resume", "Yes" if self._job.resumable else "No")
        if view.kind is not JobKind.TORRENT:
            self._source_grid.set("Connections", str(self.manager.connections))
        self._source_grid.set("Segments", self._segment_count(view))
        self._source_grid.set("Priority", self._priority_label())
        self._source_grid.set("Queue", self._queue_name(view.queue_id))
        if view.speed_limit_kbps:
            self._source_grid.set("Speed limit", f"{view.speed_limit_kbps} KB/s")
        self._det_url.setText(_wrappable(view.url))
        url_box = self._det_url.parentWidget()
        if url_box is not None:
            url_box.setVisible(bool(view.url))

        self._rebuild_tags(view)

    def _update_progress(self, view: JobView) -> None:
        if view.total_size:
            # A finished download reads full, even if the byte counter lagged.
            downloaded = view.total_size if view.status is JobStatus.COMPLETED else view.downloaded
            fraction = min(1.0, downloaded / view.total_size)
            self._progress.set_value(fraction)
            self._percent.setText(f"{int(fraction * 100)}%")
            self._size.setText(f"{human_bytes(downloaded)} of {human_bytes(view.total_size)}")
        elif view.status is JobStatus.DOWNLOADING:
            self._progress.set_indeterminate(True)
            self._percent.setText("")
            if view.downloaded:
                # Bytes are flowing but yt-dlp has not given a total yet
                # (common early in a DASH merge). Prefer that over a stuck
                # "Fetching metadata…" label that looked like a hang.
                self._size.setText(t("{size} downloaded", size=human_bytes(view.downloaded)))
            else:
                self._size.setText(t("Starting…"))
        else:
            self._progress.set_value(1.0 if view.status is JobStatus.COMPLETED else 0.0)
            self._percent.setText("")
            self._size.setText("")

    def _connection_lane_count(self, view: JobView) -> int:
        """How many synthetic connection rows to show for non-segmented kinds."""
        if view.kind is JobKind.TORRENT:
            stats = self.manager.torrent_stats(view.id)
            if stats is not None:
                swarm = max(stats.seeds, 0) + max(stats.peers, 0)
                if swarm > 0:
                    return max(2, min(_MAX_CONN_BARS, swarm))
        pinned = 0
        if self._job is not None and self._job.id == view.id:
            pinned = int(self._job.options.get("connections") or 0)
        n = pinned or self.manager.connections
        return max(1, min(_MAX_CONN_BARS, n))

    def _update_connections(
        self, view: JobView, palette: design.Palette, speed_bps: float = 0.0
    ) -> None:
        """Per-connection bars under the main progress for every download kind.

        Direct jobs use real byte-range segments when present; Smart / HLS /
        torrent / cloud use a waterfall split of overall progress. Bars stay
        visible after the download finishes (full fill, no speeds).
        """
        if view.status in (JobStatus.QUEUED, JobStatus.CANCELLED, JobStatus.FAILED):
            self._conn_bars.clear()
            return
        color = design.status_color(palette, view.status.value)
        active_speed = speed_bps if view.status is JobStatus.DOWNLOADING else 0.0
        # Parked (paused) with bars already painted: segments won't move.
        if (
            view.status is JobStatus.PAUSED
            and self._conn_bars.shows(view.id)
        ):
            return
        self._conn_bars.note_job(view.id)

        if view.kind is JobKind.DIRECT:
            segments = self.manager.db.segments_for(view.id)
            if segments:
                self._conn_bars.update_segments(
                    segments,
                    status=view.status,
                    color=color,
                    overall_speed=active_speed,
                )
                return

        if view.status is JobStatus.COMPLETED:
            fraction = 1.0
        elif view.total_size and view.total_size > 0:
            fraction = min(1.0, view.downloaded / view.total_size)
        else:
            fraction = 0.0
        lanes = _waterfall_lanes(
            self._connection_lane_count(view), fraction, active_speed=active_speed
        )
        self._conn_bars.update_lanes(lanes, status=view.status, color=color)

    def _update_overview(self, view: JobView, speed_bps: float, new: bool) -> None:
        downloading = view.status is JobStatus.DOWNLOADING
        created, updated = self._times
        self._spark_card.setVisible(downloading)
        if downloading and not new:
            self._spark.push(speed_bps)
            self._samples += 1
            self._speed_total += speed_bps
            self._peak = max(self._peak, speed_bps)
        average = self._speed_total / self._samples if self._samples else 0.0
        peak = self._peak

        self._overview.clear()
        if downloading:
            self._spark_val.setText(motion.fmt_speed(speed_bps))
            self._overview.set("Speed", motion.fmt_speed(speed_bps))
            self._overview.set("Average", motion.fmt_speed(average) if average > 0 else "")
            self._overview.set("Peak", motion.fmt_speed(peak) if peak > 0 else "")
            eta = ""
            if speed_bps > 1 and view.total_size:
                eta = motion.fmt_eta((view.total_size - view.downloaded) / speed_bps)
            self._overview.set("ETA", eta)
            self._overview.set("Downloaded", human_bytes(view.downloaded))
            if view.total_size:
                self._overview.set(
                    "Remaining", human_bytes(max(0, view.total_size - view.downloaded))
                )
        else:
            # Header already shows size + percent; Overview covers timing and
            # the session speeds that Details does not.
            if view.status is JobStatus.COMPLETED:
                self._overview.set("Finished", _fmt_datetime(updated))
                self._overview.set("Elapsed", _elapsed(created, updated))
            if average > 0:
                self._overview.set("Average", motion.fmt_speed(average))
            if peak > 0:
                self._overview.set("Peak", motion.fmt_speed(peak))
            self._overview.set("Retries", str(view.retry_count) if view.retry_count else "")

        self._ov_error.setText(view.error or "")
        err_box = self._ov_error.parentWidget()
        if err_box is not None:
            err_box.setVisible(bool(view.error))
        self._ov_notes.setText(view.notes or "")
        notes_box = self._ov_notes.parentWidget()
        if notes_box is not None:
            notes_box.setVisible(bool(view.notes))
        done = view.status is JobStatus.COMPLETED
        self._security_btn.setVisible(done and (Path(view.dest_dir) / view.filename).exists())

    def _update_actions(self, view: JobView) -> None:
        # Cached per (file, status): this runs on every 500ms poll, and two
        # filesystem stats a tick is real cost on a network share or a spun-down
        # drive. Nothing here can change without the status or the name changing.
        path = Path(view.dest_dir) / view.filename
        key = (str(path), view.status)
        if key != self._actions_key:
            self._actions_key = key
            self._file_present = path.exists()
        self._act_btns["open"].setEnabled(view.status is JobStatus.COMPLETED and self._file_present)
        self._act_btns["rename"].setEnabled(self._file_present)

    # ------------------------------------------------- third tab: media/peers

    def _start_probe(self, view: JobView) -> None:
        """Set up the kind-adaptive third tab on a selection change: live swarm
        stats for a torrent, ffprobe for media, an entry list for an archive -
        or hidden. The torrent tab then refreshes each tick in _update."""
        self._media_grid.clear()
        self._peers_grid.clear()
        self._extract_btn.setVisible(False)
        self._media_status.setText("")
        self._media_grid.setVisible(True)
        self._peers_grid.setVisible(False)
        path = Path(view.dest_dir) / view.filename
        ext = path.suffix.lower().lstrip(".")
        completed = view.status is JobStatus.COMPLETED and path.exists()
        is_media = ext in _VIDEO_EXT or ext in _AUDIO_EXT
        is_arch = completed and archive.is_archive(path)

        if view.kind is JobKind.TORRENT:
            # Peers exist while downloading and while seeding; None means the
            # torrent isn't in the session (e.g. a finished one after restart).
            if self._update_peers(view):
                self._media_title.setText(t("PEERS"))
                self._media_grid.setVisible(False)
                self._peers_grid.setVisible(True)
                self._show_third_tab(t("Peers"))
            else:
                self._hide_third_tab()
        elif completed and is_media and self._ffmpeg:
            self._media_title.setText(t("MEDIA"))
            self._show_third_tab(t("Media"))
            self._media_status.setText(t("Reading…"))
            gen = self._probe_gen
            ffmpeg = self._ffmpeg
            self._run(lambda: read_media_info(path, ffmpeg), lambda r: self._fill_media(r, gen))
        elif is_arch:
            self._media_title.setText(t("CONTENTS"))
            self._show_third_tab(t("Contents"))
            self._media_status.setText(t("Reading…"))
            gen = self._probe_gen
            self._run(lambda: archive.list_entries(path), lambda r: self._fill_archive(r, gen))
        else:
            self._hide_third_tab()

    def _update_peers(self, view: JobView) -> bool:
        """Fill the Peers grid from the torrent's live swarm stats; return False
        (so the caller can hide the tab) when the torrent isn't in the session."""
        stats = self.manager.torrent_stats(view.id)
        if stats is None:
            return False
        self._peers_grid.set("Status", t("Seeding") if stats.seeding else t("Downloading"))
        self._peers_grid.set("Seeds", _swarm(stats.seeds, stats.swarm_seeds))
        self._peers_grid.set("Peers", _swarm(stats.peers, stats.swarm_peers))
        self._peers_grid.set(
            "Down speed", motion.fmt_speed(stats.down_rate) if stats.down_rate else ""
        )
        self._peers_grid.set("Up speed", motion.fmt_speed(stats.up_rate) if stats.up_rate else "")
        self._peers_grid.set(
            "Downloaded", human_bytes(stats.downloaded) if stats.downloaded else ""
        )
        self._peers_grid.set("Uploaded", human_bytes(stats.uploaded) if stats.uploaded else "")
        self._peers_grid.set("Ratio", f"{stats.ratio:.2f}")
        return True

    def _run(self, work: Callable[[], object], done: Callable[[object], None]) -> None:
        # Unparented and retained: an ffprobe/archive scan outlives the drawer
        # that asked for it, and a QThread destroyed while still running aborts
        # the process. threads.retain owns it until it genuinely stops, and
        # threads.shutdown waits for it on quit.
        thread = FileOpThread(work)

        def deliver(result: object, error: object) -> None:
            if error is None:
                done(result)
            else:
                log.info("detail probe failed: %s", error)
                self._media_status.setText("")

        thread.done.connect(deliver)
        threads.retain(thread)
        thread.start()

    def _fill_media(self, result: object, gen: int) -> None:
        if gen != self._probe_gen:
            return  # selection moved on; this is a stale file's answer
        if not isinstance(result, MediaSummary):
            self._hide_third_tab()
            return
        self._media_status.setText("")
        if result.width and result.height:
            self._media_grid.set("Resolution", f"{result.width}x{result.height}")
        self._media_grid.set("Duration", duration_text(result.duration) if result.duration else "")
        self._media_grid.set("FPS", f"{result.fps:g}" if result.fps else "")
        self._media_grid.set("Video", result.vcodec or "")
        self._media_grid.set("Audio", result.acodec or "")
        self._media_grid.set("Container", result.container or "")
        if not self._media_grid.any_visible():
            self._hide_third_tab()

    def _fill_archive(self, result: object, gen: int) -> None:
        if gen != self._probe_gen:
            return
        entries = result if isinstance(result, tuple) else ()
        files = [entry for entry in entries if not getattr(entry, "is_dir", False)]
        self._media_status.setText("")
        self._media_grid.set("Files", str(len(files)) if files else "")
        total = sum(getattr(entry, "size", 0) or 0 for entry in files)
        if total:
            self._media_grid.set("Uncompressed", human_bytes(total))
        self._extract_btn.setVisible(True)

    def _show_third_tab(self, label: str) -> None:
        self._tabs[2].setText(label)
        self._tabs[2].setVisible(True)

    def _hide_third_tab(self) -> None:
        self._tabs[2].setVisible(False)
        if self._pages.currentIndex() == 2:
            self._select_tab(0)

    # ------------------------------------------------------------- small bits

    def _disk_size(self, path: Path) -> str:
        try:
            return human_bytes(path.stat().st_size)
        except OSError:
            return ""

    def _segment_count(self, view: JobView) -> str:
        if view.kind is not JobKind.HLS or self._job is None:
            return ""
        work = self._job.part_path.parent / f".{self._job.part_path.name}.hls"
        if not work.is_dir():
            return ""
        count = sum(1 for _ in work.glob("*") if not _.name.endswith(".part"))
        return str(count) if count else ""

    def _priority_label(self) -> str:
        if self._job is None or self._job.priority == 0:
            return "Normal"
        return "High" if self._job.priority > 0 else "Low"

    def _rebuild_tags(self, view: JobView) -> None:
        while self._tags_layout.count():
            item = self._tags_layout.takeAt(0)
            if item is None:
                break
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        tags = [t.strip() for t in view.tags.split(",") if t.strip()] if view.tags else []
        for tag in tags:
            self._tags_layout.addWidget(components.Chip(tag))
        self._tags_layout.addStretch(1)
        self._tags_box.setVisible(bool(tags))

    def _queue_name(self, queue_id: int | None) -> str:
        if queue_id is None:
            return "Default"
        for queue in self.manager.list_queues():
            if queue.id == queue_id:
                return queue.name
        return "Default"
