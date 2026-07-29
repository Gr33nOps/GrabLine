"""First-run Browser Setup wizard: pick a language, pair the native host, put
the extension at a stable path, and give each browser the shortest free install
path."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core import browser_setup, i18n
from app.core.i18n import t
from app.core.settings import Settings
from app.native_host import install as host_install
from app.ui import chrome


class SetupDialog(chrome.Dialog):
    def __init__(self, parent: QWidget | None = None, *, settings: Settings | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle(t("Browser setup"))
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)

        # Language (first, so a user in another language can switch before
        # reading the rest). Applies on restart, like Settings.
        lang_row = QHBoxLayout()
        lang_row.addWidget(QLabel(t("Language")))
        self._lang_combo = QComboBox()
        for code, name, native in i18n.available_languages():
            self._lang_combo.addItem(native if native == name else f"{native} ({name})", code)
        index = self._lang_combo.findData(i18n.current_language())
        if index >= 0:
            self._lang_combo.setCurrentIndex(index)
        self._lang_combo.currentIndexChanged.connect(self._on_language_changed)
        lang_row.addWidget(self._lang_combo, 1)
        layout.addLayout(lang_row)
        self._lang_note = QLabel("")
        self._lang_note.setProperty("role", "muted")
        layout.addWidget(self._lang_note)

        intro = QLabel(
            t(
                "Two steps connect GrabLine to your browser, so a download "
                "button appears on videos and links."
            )
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # Step 1 - pair the native host.
        layout.addWidget(self._heading(t("1. Pair GrabLine with your browsers")))
        pair_row = QHBoxLayout()
        self._pair_status = QLabel(t("Not paired yet."))
        pair_button = QPushButton(t("Pair now"))
        pair_button.clicked.connect(self._pair)
        pair_row.addWidget(self._pair_status, 1)
        pair_row.addWidget(pair_button)
        layout.addLayout(pair_row)

        # Step 2 - the extension.
        layout.addWidget(self._heading(t("2. Add the extension")))

        # A prominent action for the detected default browser. No app can
        # install an extension itself - the browser only accepts one from its
        # own store (one click) or a manual developer load. So: if that browser
        # has a live store listing, the button opens it; otherwise it opens the
        # extension folder and the extensions page to Load unpacked in one go.
        browser = browser_setup.default_browser()
        self._store_url = browser_setup.extension_install_url()
        self._add_hint: QLabel | None = None
        if browser is not None:
            add_button = QPushButton(t("Add GrabLine to {browser}", browser=browser[1]))
            add_button.setProperty("accent", "true")
            add_button.clicked.connect(self._add_to_browser)
            layout.addWidget(add_button)
            self._add_hint = QLabel(
                t("Opens the store page in {browser}. Click <b>Add</b> there.", browser=browser[1])
                if self._store_url
                else t(
                    "{browser} has no store install yet, so this opens the "
                    "extension folder and the extensions page, then turn on "
                    "<b>Developer mode</b> and <b>Load unpacked</b> it (one time).",
                    browser=browser[1],
                )
            )
            self._add_hint.setWordWrap(True)
            self._add_hint.setProperty("role", "muted")
            layout.addWidget(self._add_hint)

        folder_row = QHBoxLayout()
        self._folder_edit = QLineEdit()
        self._folder_edit.setReadOnly(True)
        open_folder = QPushButton(t("Open folder"))
        open_folder.clicked.connect(self._open_folder)
        copy_path = QPushButton(t("Copy path"))
        copy_path.clicked.connect(self._copy_path)
        folder_row.addWidget(self._folder_edit, 1)
        folder_row.addWidget(open_folder)
        folder_row.addWidget(copy_path)
        layout.addLayout(folder_row)

        chrome_row = QHBoxLayout()
        chrome_hint = QLabel(
            t(
                "Chrome / Edge / Brave: open the extensions page, enable "
                "Developer mode, click Load unpacked, and choose the folder above."
            )
        )
        chrome_hint.setWordWrap(True)
        copy_url = QPushButton(t("Copy chrome://extensions"))
        copy_url.clicked.connect(lambda: QGuiApplication.clipboard().setText("chrome://extensions"))
        chrome_row.addWidget(chrome_hint, 1)
        chrome_row.addWidget(copy_url)
        layout.addLayout(chrome_row)

        firefox_hint = QLabel(
            t(
                "Firefox: install GrabLine Connect from addons.mozilla.org. It "
                "is reviewed and signed by Mozilla, so it survives restarts."
            )
        )
        firefox_hint.setWordWrap(True)
        firefox_hint.setProperty("role", "muted")
        layout.addWidget(firefox_hint)

        # Detected browsers, for reassurance.
        detected = [b.name for b in browser_setup.detect_browsers() if b.installed]
        if detected:
            found = QLabel(t("Detected: {names}", names=", ".join(detected)))
            found.setProperty("role", "muted")
            layout.addWidget(found)

        # Verify.
        verify_row = QHBoxLayout()
        self._verify_status = QLabel("")
        verify_button = QPushButton(t("Check connection"))
        verify_button.clicked.connect(self._verify)
        verify_row.addWidget(self._verify_status, 1)
        verify_row.addWidget(verify_button)
        layout.addLayout(verify_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self._prepare_extension()

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _heading(text: str) -> QLabel:
        from app.ui import components, design

        label = components.role_label(text, "strong", size=design.FONT["h2"], bold=True)
        label.setContentsMargins(0, 8, 0, 0)
        return label

    def _on_language_changed(self) -> None:
        code = str(self._lang_combo.currentData())
        if self._settings is not None:
            self._settings.language = code
        # The already-built UI stays in the current language; the new one takes
        # effect on the next launch (or immediately via Restart now).
        if code == i18n.current_language():
            self._lang_note.setText("")
            return
        self._lang_note.setText(t("Restart GrabLine to apply the language."))
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("GrabLine")
        box.setText(t("Restart GrabLine to apply the new language."))
        restart_btn = box.addButton(t("Restart now"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(t("Later"), QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is restart_btn:
            from PySide6.QtWidgets import QApplication

            from app.core import launcher

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

    def _prepare_extension(self) -> None:
        try:
            path = browser_setup.install_extension_files()
            self._folder_edit.setText(str(path))
        except (OSError, FileNotFoundError) as exc:
            self._folder_edit.setText(t("(could not prepare extension: {error})", error=exc))

    def _add_to_browser(self) -> None:
        if self._store_url:
            QDesktopServices.openUrl(QUrl(self._store_url))
            return
        # No store listing yet (Chromium before the Web Store): open the staged
        # folder and copy the extensions URL so Load unpacked is a paste away.
        self._open_folder()
        QGuiApplication.clipboard().setText("chrome://extensions")
        if self._add_hint is not None:
            self._add_hint.setText(
                t(
                    "Opened the extension folder and copied <b>chrome://extensions</b>. "
                    "Paste it in a new tab, turn on <b>Developer mode</b>, click "
                    "<b>Load unpacked</b>, and pick that folder."
                )
            )

    def _pair(self) -> None:
        try:
            written = host_install.install()
        except OSError as exc:
            self._pair_status.setText(t("Pairing failed: {error}", error=exc))
            return
        self._pair_status.setText(t("Paired with {count} browser location(s).", count=len(written)))

    def _open_folder(self) -> None:
        path = self._folder_edit.text()
        if path and Path(path).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _copy_path(self) -> None:
        QGuiApplication.clipboard().setText(self._folder_edit.text())

    def _verify(self) -> None:
        healthy, lines = host_install.check()
        if healthy:
            self._verify_status.setText(t("Connected. The app and browser can talk."))
        else:
            fail = next((line for line in lines if line.startswith("FAIL")), "not paired yet")
            self._verify_status.setText(fail.removeprefix("FAIL ").strip() or t("not paired yet"))
