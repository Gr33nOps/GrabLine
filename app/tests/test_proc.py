from __future__ import annotations

import sys

import pytest

from app.core import proc


def test_clean_env_is_none_when_not_frozen(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert proc.clean_env() is None  # nothing to strip; inherit the environment


def test_clean_env_strips_the_bundled_lib_path(monkeypatch: pytest.MonkeyPatch):
    """A frozen AppImage/PyInstaller build leaks its bundled LD_LIBRARY_PATH into
    children, which broke system tools (Open folder launched a browser). The
    bundle paths are removed while a genuine system path survives."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", "/tmp/.mount_x/usr/bin/_internal", raising=False)
    monkeypatch.setenv("APPDIR", "/tmp/.mount_x")
    monkeypatch.setenv(
        "LD_LIBRARY_PATH", "/tmp/.mount_x/usr/bin/_internal:/usr/lib/x86_64-linux-gnu"
    )
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/tmp/.mount_x/usr/bin/_internal")

    env = proc.clean_env()

    assert env is not None
    assert env.get("LD_LIBRARY_PATH") == "/usr/lib/x86_64-linux-gnu"  # bundle gone, system kept
    assert "/tmp/.mount_x" not in (env.get("LD_LIBRARY_PATH") or "")
    assert "LD_LIBRARY_PATH_ORIG" not in env  # was only the bundle, so dropped entirely


def test_clean_env_drops_ld_path_when_only_the_bundle(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", "/opt/app/_internal", raising=False)
    monkeypatch.delenv("APPDIR", raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/app/_internal")

    env = proc.clean_env()

    assert env is not None
    assert "LD_LIBRARY_PATH" not in env  # nothing legitimate left, so removed
