"""Cloud account manager (add/edit/remove SFTP, FTP, WebDAV and S3 accounts)
and the folder-download picker.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
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
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.credentials import CloudAccount, CredentialStore
from app.core.i18n import t
from app.engines.cloud import RemoteFile
from app.ui import chrome
from app.ui.format import human_bytes

_SERVICES = ("sftp", "ftp", "ftps", "scp", "webdav", "s3")


class CloudAccountsDialog(chrome.Dialog):
    """Manage saved cloud credentials. Secrets go straight to the keyring;
    this dialog only ever shows the account list, never the stored secret."""

    def __init__(self, store: CredentialStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self.setWindowTitle(t("Cloud accounts"))
        self.setMinimumSize(480, 340)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                t(
                    "Saved logins for SFTP / FTP / WebDAV / S3. Passwords and key "
                    "passphrases are kept in your system keychain, never in GrabLine."
                )
            )
        )
        self.list = QListWidget()
        layout.addWidget(self.list)

        buttons = QHBoxLayout()
        add = QPushButton(t("Add…"))
        add.clicked.connect(self._add)
        remove = QPushButton(t("Remove"))
        remove.clicked.connect(self._remove)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        close = QPushButton(t("Close"))
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self._reload()

    def _reload(self) -> None:
        self.list.clear()
        for account in self.store.list_accounts():
            label = account.label or f"{account.username}@{account.host}"
            item = QListWidgetItem(f"[{account.service}]  {label}")
            item.setData(Qt.ItemDataRole.UserRole, account)
            self.list.addItem(item)

    def _add(self) -> None:
        dialog = _AccountEditor(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        account, secret = dialog.result_account()
        if not account.host:
            QMessageBox.warning(self, "GrabLine", t("A host is required."))
            return
        self.store.save_account(account, secret)
        self._reload()

    def _remove(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        item = self.list.item(row)
        account = item.data(Qt.ItemDataRole.UserRole)
        self.store.delete_account(account)
        self._reload()


class _AccountEditor(chrome.Dialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("Cloud account"))
        self.setMinimumWidth(380)
        form = QFormLayout(self)
        self.service = QComboBox()
        self.service.addItems(_SERVICES)
        form.addRow(t("Service:"), self.service)
        self.host = QLineEdit()
        self.host.setPlaceholderText(t("host, or the S3 endpoint (e.g. s3.us-west.amazonaws.com)"))
        form.addRow(t("Host:"), self.host)
        self.username = QLineEdit()
        self.username.setPlaceholderText(t("username, or the S3 access key"))
        form.addRow(t("Username:"), self.username)
        self.port = QLineEdit()
        self.port.setPlaceholderText(t("blank = default"))
        form.addRow(t("Port:"), self.port)
        self.secret = QLineEdit()
        self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret.setPlaceholderText(t("password, key passphrase, or S3 secret key"))
        form.addRow(t("Secret:"), self.secret)
        self.key_file = QLineEdit()
        self.key_file.setPlaceholderText(t("SFTP/SCP private-key file (optional)"))
        form.addRow(t("Key file:"), self.key_file)
        self.label = QLineEdit()
        self.label.setPlaceholderText(t("a name for this account (optional)"))
        form.addRow(t("Label:"), self.label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def result_account(self) -> tuple[CloudAccount, str | None]:
        port_text = self.port.text().strip()
        account = CloudAccount(
            service=self.service.currentText(),
            host=self.host.text().strip(),
            username=self.username.text().strip(),
            port=int(port_text) if port_text.isdigit() else 0,
            key_file=self.key_file.text().strip(),
            label=self.label.text().strip(),
        )
        secret = self.secret.text()
        return account, secret or None


class CloudFolderDialog(chrome.Dialog):
    """Pick which files from a remote folder to download."""

    def __init__(
        self, folder_url: str, files: list[RemoteFile], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.files = files
        self.setWindowTitle(t("Remote folder"))
        self.setMinimumSize(520, 380)
        layout = QVBoxLayout(self)
        total = sum(f.size or 0 for f in files)
        layout.addWidget(
            QLabel(
                t(
                    "{count} file(s) in {folder}, {size}",
                    count=len(files),
                    folder=folder_url,
                    size=human_bytes(total),
                )
            )
        )
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([t("File"), t("Size")])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 380)
        for entry in files:
            item = QTreeWidgetItem([entry.name, human_bytes(entry.size) if entry.size else ""])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked)
            item.setData(0, Qt.ItemDataRole.UserRole, entry.url)
            self.tree.addTopLevelItem(item)
        layout.addWidget(self.tree)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(t("Download"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_urls(self) -> list[str]:
        urls: list[str] = []
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            if item is not None and item.checkState(0) == Qt.CheckState.Checked:
                urls.append(str(item.data(0, Qt.ItemDataRole.UserRole)))
        return urls


def prompt_cloud_url(parent: QWidget | None) -> str | None:
    """Ask for a cloud address (a small helper for the File menu action)."""
    url, ok = QInputDialog.getText(
        parent,
        "Add cloud download",
        "Address (sftp:// ftp:// s3:// webdav://, or a Drive/Dropbox share link):",
    )
    return url.strip() if ok and url.strip() else None
