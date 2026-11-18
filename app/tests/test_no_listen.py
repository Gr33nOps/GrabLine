"""The listener gate: only app/remote/ may listen on a port."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check_no_listen.py"

LISTENER = (
    "from PySide6.QtNetwork import QHostAddress, QTcpServer\n"
    "server = QTcpServer()\n"
    "server.listen(QHostAddress.Any, 0)\n"
)


def _run(root: Path | None = None) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(SCRIPT)]
    if root is not None:
        cmd.append(str(root))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)


def test_repository_passes() -> None:
    result = _run()
    assert result.returncode == 0, result.stdout + result.stderr


def test_listener_outside_remote_fails(tmp_path: Path) -> None:
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "bad.py").write_text(LISTENER, encoding="utf-8")
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "bad.py:" in result.stdout


def test_listener_under_remote_passes(tmp_path: Path) -> None:
    (tmp_path / "remote").mkdir()
    (tmp_path / "remote" / "server.py").write_text(LISTENER, encoding="utf-8")
    assert _run(tmp_path).returncode == 0


def test_local_server_is_allowlisted(tmp_path: Path) -> None:
    (tmp_path / "ipc.py").write_text(
        "from PySide6.QtNetwork import QLocalServer\ns = QLocalServer()\n", encoding="utf-8"
    )
    assert _run(tmp_path).returncode == 0
