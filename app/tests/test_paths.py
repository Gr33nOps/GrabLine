"""Data-folder and database privacy (security finding F6, CWE-732)."""

from __future__ import annotations

import os
import stat

import pytest

from app.core import paths
from app.db.database import Database

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")


@posix_only
def test_data_dir_is_private(tmp_path):
    """The data folder is locked to the owner, so no other local user can read
    the API keys and session cookies the DB inside it holds."""
    target = tmp_path / "grabline-data"
    paths.ensure_private_dir(target)
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o700
    # Group and other have no access at all.
    assert not mode & (stat.S_IRWXG | stat.S_IRWXO)


@posix_only
def test_database_file_is_owner_only(tmp_path):
    db_path = tmp_path / "grabline.db"
    db = Database(db_path)
    try:
        mode = stat.S_IMODE(db_path.stat().st_mode)
        assert mode == 0o600
        assert not mode & (stat.S_IRWXG | stat.S_IRWXO)
    finally:
        db.close()


@posix_only
def test_ensure_private_dir_survives_a_chmod_failure(tmp_path, monkeypatch):
    """A filesystem that rejects chmod must not crash startup."""
    from pathlib import Path

    def boom(self, mode):
        raise OSError("no chmod here")

    monkeypatch.setattr(Path, "chmod", boom)
    target = tmp_path / "d"
    # Must return normally despite the chmod failure.
    assert paths.ensure_private_dir(target) == target
    assert target.is_dir()


def test_ensure_private_dir_noop_permissions_on_windows(tmp_path, monkeypatch):
    """On Windows the profile is ACL-protected; chmod is skipped, dir created."""
    monkeypatch.setattr(os, "name", "nt")
    target = tmp_path / "win"
    assert paths.ensure_private_dir(target).is_dir()
