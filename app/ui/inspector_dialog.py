"""The Download Inspector dialog: a read-only report of everything GrabLine
learned about a URL - server/IP/CDN, TLS, MIME, headers, cookies, the redirect
chain, mirrors, and the file's checksum.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import t
from app.core.inspector import InspectionReport, inspect_url
from app.ui import chrome, motion, threads
from app.ui.format import human_bytes


def _render(report: InspectionReport) -> str:
    lines: list[str] = []

    def section(title: str) -> None:
        lines.append("")
        lines.append(f"── {title} ──")

    if not report.reachable:
        return t("Could not reach the server.\n\n{error}", error=report.error)

    section(t("Overview"))
    lines.append(t("URL:          {url}", url=report.url))
    if report.final_url != report.url:
        lines.append(t("Final URL:    {url}", url=report.final_url))
    if report.status is not None:
        lines.append(t("Status:       HTTP {status}", status=report.status))
    if report.response_ms is not None:
        lines.append(t("Response time: {ms} ms", ms=report.response_ms))
    if report.mime_type:
        lines.append(t("MIME type:    {mime}", mime=report.mime_type))
    if report.content_length is not None:
        lines.append(t("Size:         {size}", size=human_bytes(report.content_length)))

    section(t("Server & location"))
    if report.ip_addresses:
        lines.append(t("IP:           {ips}", ips=", ".join(report.ip_addresses)))
    if report.reverse_dns:
        lines.append(t("Reverse DNS:  {dns}", dns=report.reverse_dns))
    if report.cdn:
        lines.append(t("CDN:          {cdn}", cdn=report.cdn))
    if report.server:
        lines.append(t("Server:       {server}", server=report.server))
    if not (report.ip_addresses or report.cdn or report.server):
        lines.append(t("(no server details available)"))
    lines.append(
        t("(GrabLine shows IP/DNS/CDN only. It never sends the address to a geo service.)")
    )

    if report.tls is not None:
        section(t("TLS / SSL"))
        lines.append(t("Protocol:     {version}", version=report.tls.version))
        lines.append(t("Cipher:       {cipher}", cipher=report.tls.cipher))
        lines.append(t("Subject:      {subject}", subject=report.tls.subject))
        lines.append(t("Issuer:       {issuer}", issuer=report.tls.issuer))
        lines.append(t("Valid from:   {when}", when=report.tls.valid_from))
        lines.append(t("Valid until:  {when}", when=report.tls.valid_until))

    if report.redirect_chain:
        section(t("Redirect chain"))
        for status, location in report.redirect_chain:
            lines.append(f"  {status} to {location}")
        lines.append(f"  {report.status} to {report.final_url}")

    if report.mirrors:
        section(t("Mirrors"))
        for mirror in report.mirrors:
            lines.append(f"  {mirror}")

    if report.checksum:
        section(t("Checksum"))
        lines.append(t("SHA-256:      {checksum}", checksum=report.checksum))

    if report.cookies:
        section(t("Cookies ({count})", count=len(report.cookies)))
        for cookie in report.cookies:
            lines.append(f"  {cookie}")

    section(t("HTTP headers ({count})", count=len(report.headers)))
    for name, value in report.headers:
        lines.append(f"  {name}: {value}")

    return "\n".join(lines).strip()


class InspectorDialog(chrome.Dialog):
    def __init__(
        self,
        url: str,
        *,
        mirrors: tuple[str, ...] = (),
        checksum_work: Callable[[], str] | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._mirrors = mirrors
        self._checksum_work = checksum_work
        self.setWindowTitle(t("Download inspector"))
        self.setMinimumSize(620, 480)
        layout = QVBoxLayout(self)
        self._status = QLabel(t("Inspecting {url} …", url=url))
        layout.addWidget(self._status)
        # A visible loading state while the probe runs - never a blank window.
        self._loading_bar = motion.SmoothProgressBar()
        self._loading_bar.set_indeterminate(True)
        layout.addWidget(self._loading_bar)
        self._loading_note = QLabel(t("Checking the link…"))
        layout.addWidget(self._loading_note)
        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._text.setStyleSheet("font-family: monospace;")
        self._text.hide()  # shown when the report lands
        layout.addWidget(self._text)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy = buttons.addButton(t("Copy"), QDialogButtonBox.ButtonRole.ActionRole)
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self._text.toPlainText()))
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self._thread = threads.CallableThread(lambda: self._gather(url, headers, proxy))
        self._thread.done.connect(self._show)
        threads.retain(self._thread)  # survives dialog close; see app/ui/threads
        self._thread.start()

    def _gather(
        self, url: str, headers: dict[str, str] | None, proxy: str | None
    ) -> InspectionReport:
        """Runs on the worker thread: the network probe, then the (possibly
        slow) file checksum and the stored mirrors, all off the UI thread."""
        report = inspect_url(url, headers=headers, proxy=proxy)
        checksum = ""
        if self._checksum_work is not None:
            try:
                checksum = self._checksum_work()
            except OSError:
                checksum = ""
        return InspectionReport(
            **{**report.__dict__, "mirrors": self._mirrors, "checksum": checksum}
        )

    def _show(self, report: object) -> None:
        assert isinstance(report, InspectionReport)
        self._loading_bar.set_indeterminate(False)
        self._loading_bar.hide()
        self._loading_note.hide()
        self._text.show()
        self._status.setText(
            t("Unreachable")
            if not report.reachable
            else t("HTTP {status} · done", status=report.status)
        )
        self._text.setPlainText(_render(report))
