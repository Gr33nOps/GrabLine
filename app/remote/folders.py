"""Folder ids for phones (PROTOCOL.md section 12, `queues`).

A phone never sends a path. It picks a folder by an opaque id that the PC
derived from one of its own folders: the default download folder and the
favourite folders that exist. The id is
``"f" + base64url(HMAC-SHA256(folderSecret, normalised path)[:10])``, so it is
stable across restarts, reveals nothing about the path, and cannot be forged
for a folder the PC did not list. Resolving an id re-derives the list, so a
folder removed from favourites stops resolving at once (QA-08).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.settings import Settings
    from app.db.database import Database

SECRET_KEY = "remote_folder_secret"
MAX_FOLDERS = 50


@dataclass(frozen=True)
class RemoteFolder:
    id: str
    name: str
    path: Path
    is_default: bool


def _secret(db: Database) -> bytes:
    stored = db.get_setting(SECRET_KEY)
    if stored:
        try:
            raw = bytes.fromhex(stored)
            if len(raw) == 32:
                return raw
        except ValueError:
            pass
    raw = secrets.token_bytes(32)
    db.set_setting(SECRET_KEY, raw.hex())
    return raw


def _normalise(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.expanduser().resolve(strict=False))))


def folder_id(secret: bytes, path: Path) -> str:
    digest = hmac.new(secret, _normalise(path).encode("utf-8"), hashlib.sha256).digest()
    return "f" + base64.urlsafe_b64encode(digest[:10]).rstrip(b"=").decode("ascii")


def list_folders(settings: Settings, db: Database) -> list[RemoteFolder]:
    """The default download folder, then the favourites that exist."""
    secret = _secret(db)
    candidates: list[tuple[Path, bool]] = [(Path(settings.download_dir), True)]
    candidates += [(Path(p), False) for p in settings.favorite_folders]
    seen: set[str] = set()
    picked: list[tuple[Path, bool]] = []
    for path, is_default in candidates:
        key = _normalise(path)
        if key in seen or not (is_default or path.is_dir()):
            continue
        seen.add(key)
        picked.append((path, is_default))
        if len(picked) >= MAX_FOLDERS:
            break
    names = _display_names([p for p, _ in picked])
    return [
        RemoteFolder(folder_id(secret, path), name, path, is_default)
        for (path, is_default), name in zip(picked, names, strict=True)
    ]


def resolve_folder(settings: Settings, db: Database, wanted: str) -> Path | None:
    """The path for a folder id the PC listed, or None. Constant-time compare."""
    found: Path | None = None
    for folder in list_folders(settings, db):
        if hmac.compare_digest(folder.id.encode("ascii"), wanted.encode("ascii", "replace")):
            found = folder.path
    return found


def _display_names(paths: list[Path]) -> list[str]:
    """Last path component; with the parent's name when two would collide.
    Never a full path."""
    base = [p.name or p.anchor.strip("\\/:") or "folder" for p in paths]
    out: list[str] = []
    for path, name in zip(paths, base, strict=True):
        if base.count(name) > 1 and path.parent.name:
            out.append(f"{path.parent.name}/{name}")
        else:
            out.append(name)
    return out
