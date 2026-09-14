#!/usr/bin/env python3
"""Time smart-engine inspect (+ optional download-ready prefetch) for the URL suite.

Usage:
    .venv/bin/python scripts/bench_smart_urls.py
    .venv/bin/python scripts/bench_smart_urls.py --prefetch yt-regular yt-4k-hdr
    .venv/bin/python scripts/bench_smart_urls.py --only yt-regular,vimeo
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.engines.smart import (  # noqa: E402
    MediaInfo,
    PlaylistInfo,
    SmartEngine,
    prefetch_download_ready,
    take_download_ready,
)
from app.tests.smart_url_suite import SUITE  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        help="Comma-separated suite names to run (default: all)",
    )
    parser.add_argument(
        "--prefetch",
        nargs="*",
        metavar="NAME",
        help="Also time download-ready prefetch for these names (or all if empty list)",
    )
    args = parser.parse_args()

    wanted = {n.strip() for n in (args.only or "").split(",") if n.strip()}
    entries = [e for e in SUITE if not wanted or e.name in wanted]
    if not entries:
        print("no matching URLs", file=sys.stderr)
        return 2

    prefetch_names: set[str] | None
    if args.prefetch is None:
        prefetch_names = set()
    elif args.prefetch == []:
        prefetch_names = {e.name for e in entries}
    else:
        prefetch_names = set(args.prefetch)

    engine = SmartEngine()
    print("warming extractors…")
    t0 = time.perf_counter()
    engine.warm_up()
    print(f"warm_up {time.perf_counter() - t0:5.1f}s\n")
    print(f"{'name':16} {'inspect':>8} {'prefetch':>9}  status")
    print("-" * 56)

    failures = 0
    for entry in entries:
        status = "ok"
        inspect_s = 0.0
        prefetch_s = "-"
        failed = False
        try:
            started = time.perf_counter()
            result = engine.inspect(entry.url)
            inspect_s = time.perf_counter() - started
            if not entry.playlist and not entry.live_smoke:
                if not isinstance(result, MediaInfo) or not result.options:
                    status = "no-formats"
                    failed = True
            elif not isinstance(result, MediaInfo | PlaylistInfo):
                status = "bad-type"
                failed = True
            if inspect_s >= entry.inspect_budget:
                status = f"slow>{entry.inspect_budget:.0f}s"
                failed = True
        except Exception as exc:
            status = f"ERR {exc}"[:40]
            failed = True
            inspect_s = time.perf_counter() - started

        if failed and entry.optional:
            status = f"optional {status}"
            failed = False

        if entry.name in prefetch_names and status == "ok":
            try:
                p0 = time.perf_counter()
                prefetch_download_ready(entry.url)
                ready = take_download_ready(entry.url, wait=120.0)
                prefetch_s = f"{time.perf_counter() - p0:7.1f}s"
                if ready is None or not ready.get("formats"):
                    status = "prefetch-empty"
                    failed = True
            except Exception as exc:
                prefetch_s = "  fail"
                status = f"prefetch {exc}"[:40]
                failed = True

        if failed:
            failures += 1
        print(f"{entry.name:16} {inspect_s:7.1f}s {prefetch_s:>9}  {status}")

    print("-" * 56)
    print(f"{failures} failure(s)" if failures else "all good")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
