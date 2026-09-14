"""The security report dialog: an advisory summary of a finished download -
risk level, findings, checksums (MD5/SHA-1/SHA-256/SHA-512/CRC32), the local
virus-scan result, and the VirusTotal lookup when a key is set.

Nothing here blocks or deletes. It informs; the user decides.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import t
from app.core.security import Risk, SecurityReport, check_file
from app.core.settings import Settings
from app.ui import chrome, motion, threads

_COLORS = {Risk.OK: "#2ea043", Risk.CAUTION: "#d29922", Risk.WARNING: "#cf222e"}


def _render(report: SecurityReport) -> str:
    lines = [t("Verdict: {label}", label=report.level.label), ""]
    for finding in report.findings:
        lines.append(f"• {finding}")
    if report.virustotal is not None and report.virustotal.known:
        vt = report.virustotal
        lines.append("")
        lines.append(
            t(
                "VirusTotal: {malicious} malicious / {suspicious} suspicious of {total} engines",
                malicious=vt.malicious,
                suspicious=vt.suspicious,
                total=vt.total,
            )
        )
        if vt.permalink:
            lines.append(f"  {vt.permalink}")
    elif report.virustotal is not None and not report.virustotal.known:
        lines.append("")
        lines.append(t("VirusTotal: this file is not in their database yet."))
    if report.checksums:
        lines.append("")
        lines.append(t("── Checksums ──"))
        for algorithm, digest in report.checksums.items():
            lines.append(f"{algorithm.upper():<7} {digest}")
    return "\n".join(lines)


class SecurityDialog(chrome.Dialog):
    def __init__(
        self,
        path: Path,
        url: str,
        settings: Settings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("Security check"))
        self.setMinimumSize(560, 460)
        layout = QVBoxLayout(self)
        self._verdict = QLabel(t("Checking {name} …", name=path.name))
        self._verdict.setStyleSheet("font-size: 15px; font-weight: 600;")
        layout.addWidget(self._verdict)
        note = QLabel(
            t(
                "This is advice, not a verdict. A flagged file is kept and stays "
                "usable. Antivirus false positives are common; judge by where the "
                "file came from."
            )
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        layout.addWidget(note)

        self._loading_bar = motion.SmoothProgressBar()
        self._loading_bar.set_indeterminate(True)
        layout.addWidget(self._loading_bar)
        self._loading_note = QLabel(t("Checking the file…"))
        layout.addWidget(self._loading_note)
        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setStyleSheet("font-family: monospace;")
        self._text.hide()  # shown when the report lands
        layout.addWidget(self._text)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self._copy = buttons.addButton(t("Copy SHA-256"), QDialogButtonBox.ButtonRole.ActionRole)
        self._copy.setEnabled(False)  # until the report (and its checksums) lands
        self._copy.setToolTip(t("Copy just the SHA-256 checksum"))
        self._copy.clicked.connect(self._copy_checksum)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
        self._report: SecurityReport | None = None

        self._thread = threads.CallableThread(
            lambda: check_file(
                path,
                url=url,
                virustotal_key=settings.virustotal_key,
                run_local_scan=True,
                proxy=settings.proxy,
                scanner_pref=settings.scanner_pref,
            )
        )
        self._thread.done.connect(self._show)
        threads.retain(self._thread)  # survives dialog close; see app/ui/threads
        self._thread.start()

    def _show(self, report: object) -> None:
        assert isinstance(report, SecurityReport)
        self._report = report
        self._loading_bar.set_indeterminate(False)
        self._loading_bar.hide()
        self._loading_note.hide()
        self._text.show()
        color = _COLORS[report.level]
        self._verdict.setText(report.level.label)
        self._verdict.setStyleSheet(f"font-size: 15px; font-weight: 600; color: {color};")
        self._text.setPlainText(_render(report))
        self._copy.setEnabled(bool(report.checksums))

    def _copy_checksum(self) -> None:
        """Copy only the checksum - not the whole report."""
        if self._report is None or not self._report.checksums:
            return
        digest = self._report.checksums.get("sha256") or next(iter(self._report.checksums.values()))
        QGuiApplication.clipboard().setText(digest)
