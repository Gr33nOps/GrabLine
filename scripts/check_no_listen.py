"""Fail if any code outside app/remote/ listens for network connections.

The phone-remote listener is the only code allowed to accept connections, and
it lives in app/remote/. Everything else in app/ must stay client-only.

    python scripts/check_no_listen.py [ROOT]

ROOT is the directory to scan (default: the repository's app/ folder). The
top-level ``remote`` and ``tests`` folders directly under ROOT are skipped;
a folder of the same name deeper down is still scanned. Exit status is 1 if
any offending line is found.

Allowlist: QLocalServer is a local named pipe / unix socket (single-instance
IPC), not a network port, so it is not flagged. Tests may run servers.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Top-level folders under the scan root that may listen or are exempt.
EXEMPT_DIRS = frozenset({"remote", "tests"})

# (pattern, description). QLocalServer is deliberately absent (allowlisted).
PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\.listen\s*\("), "socket/server .listen()"),
    (re.compile(r"\.bind\s*\(\s*[(\"']"), "socket .bind(address)"),
    (re.compile(r"\bsocketserver\b"), "socketserver"),
    (re.compile(r"\bhttp\.server\b|\bfrom\s+http\s+import\s+server\b"), "http.server"),
    (re.compile(r"\b(?:HTTPServer|ThreadingHTTPServer)\b"), "http.server class"),
    (re.compile(r"\bstart_server\s*\("), "asyncio start_server"),
    (re.compile(r"\bcreate_server\s*\("), "asyncio create_server"),
    (re.compile(r"\bQTcpServer\b"), "QTcpServer"),
    (re.compile(r"\bQWebSocketServer\b"), "QWebSocketServer"),
    (re.compile(r"\bQSslServer\b"), "QSslServer"),
    (re.compile(r"\bQUdpSocket\b"), "QUdpSocket"),
)

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "app"


def scan(root: Path) -> list[str]:
    """Return 'file:line: reason: text' for every violation under *root*."""
    problems: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if len(rel.parts) > 1 and rel.parts[0] in EXEMPT_DIRS:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"{path}:0: unreadable: {exc}")
            continue
        for number, line in enumerate(lines, start=1):
            code = line.split("#", 1)[0]
            for pattern, reason in PATTERNS:
                if pattern.search(code):
                    problems.append(f"{path}:{number}: {reason}: {line.strip()}")
                    break
    return problems


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else DEFAULT_ROOT
    problems = scan(root)
    if problems:
        print("Listening for connections is only allowed in app/remote/:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print("check_no_listen: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
