"""PyInstaller entry for the Grabline desktop GUI executable (``grabline``)."""

from __future__ import annotations

import multiprocessing
import sys

from app.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()  # harmless if unused; safe in frozen builds
    sys.exit(main())
