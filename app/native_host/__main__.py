"""Entry point the browser launches: ``python -m app.native_host``.

Anything written to stdout that isn't a framed message corrupts the channel,
so logging goes to a file in the data directory, never to the console.
"""

from __future__ import annotations

import logging
import sys

from app.core import paths
from app.db.database import Database
from app.native_host.host import serve


def main() -> int:
    data_dir = paths.data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(data_dir / "native-host.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    db = Database(data_dir / "grabline.db")
    try:
        serve(sys.stdin.buffer, sys.stdout.buffer, db)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
