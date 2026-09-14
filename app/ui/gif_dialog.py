"""GIF conversion dialog (F2.3), reached from a completed video's row menu."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QWidget,
)

from app.core import gif
from app.core.errors import DownloadError
from app.core.i18n import t
from app.ui import chrome, motion
from app.ui.quality_panel import parse_timestamp

#: Running conversions stay referenced until finished (QThread lifetime rule).
_ACTIVE_THREADS: set[_GifThread] = set()


class _GifThread(QThread):
    done = Signal(object, object)  # Path | None, error str | None

    def __init__(
        self,
        ffmpeg_path: str,
        source: Path,
        start: float | None,
        end: float | None,
        fps: int,
        width: int,
    ) -> None:
        super().__init__()
        self._args = (ffmpeg_path, source, start, end, fps, width)

    def run(self) -> None:
        ffmpeg_path, source, start, end, fps, width = self._args
        try:
            target = gif.make_gif(ffmpeg_path, source, start=start, end=end, fps=fps, width=width)
        except DownloadError as exc:
            self.done.emit(None, str(exc))
            return
        self.done.emit(target, None)


class GifDialog(chrome.Dialog):
    """Pick a clip range/size, convert in the background, report the result."""

    def __init__(self, ffmpeg_path: str, source: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ffmpeg_path = ffmpeg_path
        self._source = source
        self.setWindowTitle(t("Convert to GIF"))
        self.setMinimumWidth(360)

        form = QFormLayout(self)
        name = QLabel(source.name)
        name.setStyleSheet("font-weight: 600;")
        form.addRow(name)
        self.start_edit = QLineEdit()
        self.start_edit.setPlaceholderText(t("0:00 (whole video)"))
        self.end_edit = QLineEdit()
        self.end_edit.setPlaceholderText(t("e.g. 0:05"))
        form.addRow(t("Start:"), self.start_edit)
        form.addRow(t("End:"), self.end_edit)
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 120)
        self.fps_spin.setValue(gif.DEFAULT_FPS)
        form.addRow(t("Frames/s:"), self.fps_spin)
        self.width_spin = QSpinBox()
        self.width_spin.setRange(120, 1920)
        self.width_spin.setSingleStep(40)
        self.width_spin.setValue(gif.DEFAULT_WIDTH)
        self.width_spin.setSuffix(t(" px wide"))
        form.addRow(t("Size:"), self.width_spin)
        hint = QLabel(t("Tip: GIFs get huge fast, so keep clips short."))
        hint.setStyleSheet("color: gray; font-size: 11px;")
        form.addRow(hint)

        self._working_bar = motion.SmoothProgressBar()
        self._working_bar.hide()
        form.addRow(self._working_bar)
        self._working_note = QLabel(t("Converting… this runs in the background."))
        self._working_note.hide()
        form.addRow(self._working_note)
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText(t("Convert"))
        self._buttons.accepted.connect(self._convert)
        self._buttons.rejected.connect(self.reject)
        form.addRow(self._buttons)

    def _convert(self) -> None:
        try:
            start = parse_timestamp(self.start_edit.text())
            end = parse_timestamp(self.end_edit.text())
        except ValueError:
            QMessageBox.warning(self, "GrabLine", t("Timestamps look like 90, 1:30, or 1:02:03."))
            return
        if end is not None and end <= (start or 0.0):
            QMessageBox.warning(self, "GrabLine", t("The end timestamp must be after the start."))
            return
        self._buttons.setEnabled(False)
        self._working_bar.show()
        self._working_bar.set_indeterminate(True)
        self._working_note.show()
        thread = _GifThread(
            self._ffmpeg_path,
            self._source,
            start,
            end,
            self.fps_spin.value(),
            self.width_spin.value(),
        )
        _ACTIVE_THREADS.add(thread)

        def _cleanup(t: _GifThread = thread) -> None:
            _ACTIVE_THREADS.discard(t)
            t.deleteLater()

        thread.finished.connect(_cleanup)
        thread.done.connect(self._on_done)
        thread.start()

    def _on_done(self, target: object, error: object) -> None:
        self._buttons.setEnabled(True)
        self._working_bar.set_indeterminate(False)
        self._working_bar.hide()
        self._working_note.hide()
        if error is not None:
            QMessageBox.warning(self, "GrabLine", str(error))
            return
        QMessageBox.information(self, "GrabLine", t("Saved {name}", name=Path(str(target)).name))
        self.accept()
