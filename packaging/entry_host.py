"""PyInstaller entry for the Grabline Native Messaging host (``grabline-host``).

A separate *console* executable on purpose: browsers speak length-prefixed
JSON to the host over stdio, and a windowed GUI binary has no working
stdin/stdout on Windows. The installed host manifests point at this binary.
"""

from __future__ import annotations

import multiprocessing
import sys

from app.native_host.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
