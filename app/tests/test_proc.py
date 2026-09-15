from __future__ import annotations

import os
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
    root = os.sep.join(("", "tmp", ".mount_x"))
    bundle = os.sep.join((root, "usr", "bin", "_internal"))
    system = os.sep.join(("", "usr", "lib", "x86_64-linux-gnu"))
    monkeypatch.setattr(sys, "_MEIPASS", bundle, raising=False)
    monkeypatch.setenv("APPDIR", root)
    monkeypatch.setenv("LD_LIBRARY_PATH", os.pathsep.join((bundle, system)))
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", bundle)

    env = proc.clean_env()

    assert env is not None
    assert env.get("LD_LIBRARY_PATH") == system  # bundle gone, system kept
    assert root not in (env.get("LD_LIBRARY_PATH") or "")
    assert "LD_LIBRARY_PATH_ORIG" not in env  # was only the bundle, so dropped entirely


def test_clean_env_drops_ld_path_when_only_the_bundle(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    bundle = os.sep.join(("", "opt", "app", "_internal"))
    monkeypatch.setattr(sys, "_MEIPASS", bundle, raising=False)
    monkeypatch.delenv("APPDIR", raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", bundle)

    env = proc.clean_env()

    assert env is not None
    assert "LD_LIBRARY_PATH" not in env  # nothing legitimate left, so removed
