"""Credential storage for the cloud protocol engine (SFTP/FTP/WebDAV/S3).

Secrets go in the OS keyring (Windows Credential Manager, macOS Keychain,
Linux Secret Service). The *list* of accounts - service, host, port,
username, an optional key-file path and a label - lives in the settings DB
so the UI can show it without unlocking every secret. Several accounts per
host are supported (multiple accounts); the engine picks one automatically
by matching the URL's host and, if given, username (automatic authentication).
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import asdict, dataclass

from app.db.database import Database

log = logging.getLogger(__name__)
_KEYRING_SERVICE = "grabline-cloud"


@dataclass(frozen=True)
class CloudAccount:
    service: str  # "sftp" | "ftp" | "ftps" | "webdav" | "s3" | "scp"
    host: str  # host, or the S3 region/endpoint
    username: str = ""
    port: int = 0  # 0 = the protocol default
    key_file: str = ""  # SFTP/SCP private-key path (secret is its passphrase)
    label: str = ""  # a friendly name for the account picker

    def token(self) -> str:
        """The keyring key that holds this account's secret."""
        return f"{self.service}://{self.username}@{self.host}:{self.port}"


def _keyring() -> object | None:
    try:
        import keyring

        return keyring
    except Exception:  # pragma: no cover - keyring backend problems
        log.warning("keyring unavailable; cloud secrets will not be stored", exc_info=True)
        return None


class CredentialStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------- accounts

    def list_accounts(self) -> list[CloudAccount]:
        raw = self._db.get_setting("cloud_accounts")
        if not raw:
            return []
        try:
            rows = json.loads(raw)
        except ValueError:
            return []
        accounts: list[CloudAccount] = []
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("service") and row.get("host"):
                accounts.append(
                    CloudAccount(
                        service=str(row["service"]),
                        host=str(row["host"]),
                        username=str(row.get("username", "")),
                        port=int(row.get("port", 0) or 0),
                        key_file=str(row.get("key_file", "")),
                        label=str(row.get("label", "")),
                    )
                )
        return accounts

    def _save_accounts(self, accounts: list[CloudAccount]) -> None:
        self._db.set_setting("cloud_accounts", json.dumps([asdict(a) for a in accounts]))

    def save_account(self, account: CloudAccount, secret: str | None) -> None:
        """Add or update an account (matched by service+host+username). A
        non-None ``secret`` (password or key passphrase) goes to the keyring."""
        accounts = [
            a
            for a in self.list_accounts()
            if not (
                a.service == account.service
                and a.host == account.host
                and a.username == account.username
            )
        ]
        accounts.append(account)
        self._save_accounts(accounts)
        if secret is not None:
            keyring = _keyring()
            if keyring is not None:
                keyring.set_password(_KEYRING_SERVICE, account.token(), secret)  # type: ignore[attr-defined]

    def delete_account(self, account: CloudAccount) -> None:
        remaining = [
            a
            for a in self.list_accounts()
            if not (
                a.service == account.service
                and a.host == account.host
                and a.username == account.username
            )
        ]
        self._save_accounts(remaining)
        keyring = _keyring()
        if keyring is not None:
            with contextlib.suppress(Exception):  # an absent secret is fine
                keyring.delete_password(_KEYRING_SERVICE, account.token())  # type: ignore[attr-defined]

    def secret_for(self, account: CloudAccount) -> str | None:
        keyring = _keyring()
        if keyring is None:
            return None
        try:
            return keyring.get_password(_KEYRING_SERVICE, account.token())  # type: ignore[attr-defined,no-any-return]
        except Exception:  # pragma: no cover
            return None

    # ------------------------------------------------------ automatic lookup

    def account_for(self, service: str, host: str, username: str = "") -> CloudAccount | None:
        """The stored account that best fits a URL: an exact username match
        wins, otherwise the first account saved for that service+host."""
        candidates = [a for a in self.list_accounts() if a.service == service and a.host == host]
        if not candidates:
            return None
        if username:
            for account in candidates:
                if account.username == username:
                    return account
        return candidates[0]
