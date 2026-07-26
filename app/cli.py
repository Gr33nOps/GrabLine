"""Headless single-download CLI, routed through the same resolver as the app.

Usage:
    python -m app.cli <url> <dest_dir> [--db PATH] [--connections N]
                      [--quality LABEL] [--list-formats] [--session BROWSER]
                      [--playlist] [--limit N]

Smart Engine URLs get the curated quality list (--list-formats to see it,
--quality to pick: best / 1080p / 720p / mp3 / m4a ...). Playlist URLs are
listed and refused unless --playlist is given (downloads up to --limit
entries at the chosen quality). Direct files use the segmented downloader;
HLS manifests are reassembled by FFmpeg.

Exit code 0 means the file completed and was verified/renamed into place.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

from app.core import naming
from app.core.downloader import DEFAULT_CONNECTIONS, SegmentedDownload
from app.core.ffmpeg import find_ffmpeg
from app.core.manager import DownloadTask
from app.core.models import Job, JobKind, JobStatus
from app.core.resolver import Resolution, Resolver
from app.core.settings import SESSION_BROWSERS, Settings
from app.db.database import Database
from app.engines.hls import HlsDownload
from app.engines.smart import (
    MediaInfo,
    QualityOption,
    SmartDownload,
    generic_quality_options,
)


def _pick_option(media: MediaInfo, quality: str) -> QualityOption | None:
    wanted = quality.strip().lower()
    for option in media.options:
        if option.label.lower() == wanted:
            return option
    if wanted == "best" and media.options:
        return media.options[0]
    return None


def _print_formats(media: MediaInfo) -> None:
    print(f"TITLE {media.title}")
    for option in media.options:
        size = f"~{option.estimated_size / 1024 / 1024:.1f} MB" if option.estimated_size else "?"
        suffix = " (audio only)" if option.kind == "audio" else ""
        print(f"  {option.label:<8} {size}{suffix}")


def _run_playlist(
    db: Database, resolution: Resolution, dest_dir: Path, args: argparse.Namespace
) -> int:
    assert resolution.playlist is not None
    playlist = resolution.playlist
    entries = playlist.entries[: args.limit]
    option = next(
        (o for o in generic_quality_options() if o.label.lower() == args.quality.lower()),
        generic_quality_options()[0],
    )
    print(f"PLAYLIST {playlist.title} - downloading {len(entries)} item(s)", flush=True)
    failures = 0
    for entry in entries:
        extension = option.audio_format if option.kind == "audio" else "mp4"
        filename = f"{naming.sanitize_filename(entry.title)}.{extension}"
        job = db.create_job(
            entry.url,
            str(dest_dir),
            filename,
            kind=JobKind.SMART,
            title=entry.title,
            options={
                "format_spec": option.format_spec,
                "quality_label": option.label,
                "audio_format": option.audio_format,
                "use_session": bool(args.session),
                "session_browser": args.session or "chrome",
            },
        )
        status = _create_task(db, job, args).run()
        print(f"[{entry.index}/{len(playlist.entries)}] {entry.title}: {status.value}", flush=True)
        if status is not JobStatus.COMPLETED:
            failures += 1
    return 1 if failures else 0


def _create_job(
    db: Database, resolution: Resolution, dest_dir: Path, args: argparse.Namespace
) -> Job | None:
    if resolution.kind is JobKind.SMART:
        assert resolution.media is not None
        if args.list_formats:
            _print_formats(resolution.media)
            return None
        option = _pick_option(resolution.media, args.quality)
        if option is None:
            print(f"ERROR unknown quality '{args.quality}' - try --list-formats", flush=True)
            raise SystemExit(2)
        extension = option.audio_format if option.kind == "audio" else "mp4"
        filename = f"{naming.sanitize_filename(resolution.media.title)}.{extension}"
        return db.create_job(
            resolution.url,
            str(dest_dir),
            filename,
            kind=JobKind.SMART,
            title=resolution.media.title,
            options={
                "format_spec": option.format_spec,
                "quality_label": option.label,
                "audio_format": option.audio_format,
                "use_session": bool(args.session),
                "session_browser": args.session or "chrome",
            },
        )
    if resolution.kind is JobKind.HLS:
        if args.list_formats:
            for variant in resolution.variants:
                print(f"  {variant.description}")
            if not resolution.variants:
                print("  (single-quality stream)")
            return None
        stem = Path(naming.filename_from_url(resolution.url)).stem or "stream"
        options: dict[str, Any] = {}
        if resolution.variants:
            wanted = args.quality.strip().lower()
            variant = next(
                (v for v in resolution.variants if v.label.lower() == wanted),
                resolution.variants[0],
            )
            options = {
                "variant_url": variant.url,
                "audio_url": variant.audio_url,
                "quality_label": variant.label,
            }
        return db.create_job(
            resolution.url, str(dest_dir), f"{stem}.mp4", kind=JobKind.HLS, options=options
        )
    filename = (
        naming.sanitize_filename(resolution.probe.filename)
        if resolution.probe is not None and resolution.probe.filename
        else naming.filename_from_url(resolution.url)
    )
    job = db.create_job(resolution.url, str(dest_dir), filename)
    probe = resolution.probe
    if probe is not None:
        job.final_url = probe.final_url
        job.total_size = probe.total_size
        job.resumable = probe.resumable
        job.etag = probe.etag
        job.last_modified = probe.last_modified
        db.update_job_probe(job)
        job = db.get_job(job.id) or job
    return job


def _create_task(db: Database, job: Job, args: argparse.Namespace) -> DownloadTask:
    settings = Settings(db)
    proxy = settings.proxy
    if job.kind is JobKind.SMART:
        return SmartDownload(db, job, ffmpeg_path=find_ffmpeg(settings), proxy=proxy)
    if job.kind is JobKind.HLS:
        return HlsDownload(db, job, ffmpeg_path=find_ffmpeg(settings), proxy=proxy)
    return SegmentedDownload(db, job, connections=args.connections, proxy=proxy)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grabline-cli", description=__doc__)
    parser.add_argument("url", help="URL to download")
    parser.add_argument("dest", help="destination directory")
    parser.add_argument("--db", type=Path, default=None, help="database path")
    parser.add_argument("--connections", type=int, default=DEFAULT_CONNECTIONS)
    parser.add_argument("--quality", default="best", help="quality label for Smart Engine sites")
    parser.add_argument(
        "--list-formats", action="store_true", help="show the quality list and exit"
    )
    parser.add_argument(
        "--session",
        choices=SESSION_BROWSERS,
        default=None,
        help="use this browser's login session (your own accounts only)",
    )
    parser.add_argument(
        "--playlist",
        action="store_true",
        help="download every playlist entry (up to --limit) instead of refusing",
    )
    parser.add_argument(
        "--limit", type=int, default=30, help="maximum playlist entries (default 30)"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    dest_dir = Path(args.dest).expanduser()
    dest_dir.mkdir(parents=True, exist_ok=True)
    db = Database(args.db if args.db is not None else dest_dir / "grabline-cli.db")
    try:
        db.mark_interrupted()
        job = db.find_resumable_job(args.url, str(dest_dir))
        if job is not None and not args.list_formats:
            already = db.stored_progress(job)
            print(f"RESUMING job {job.id} ({already} bytes already downloaded)", flush=True)
        else:
            resolution = Resolver().resolve(
                args.url,
                use_session=bool(args.session),
                session_browser=args.session or "chrome",
                proxy=Settings(db).proxy,
            )
            if resolution.kind is None:
                print(f"ERROR {resolution.message}", flush=True)
                return 2
            if resolution.playlist is not None:
                if args.list_formats:
                    print(f"PLAYLIST {resolution.playlist.title}")
                    for entry in resolution.playlist.entries:
                        print(f"  {entry.index:>3}. {entry.title}")
                    return 0
                if not args.playlist:
                    count = len(resolution.playlist.entries)
                    print(
                        f"PLAYLIST detected ({count} items) - rerun with --playlist "
                        "to download them",
                        flush=True,
                    )
                    return 2
                return _run_playlist(db, resolution, dest_dir, args)
            maybe_job = _create_job(db, resolution, dest_dir, args)
            if maybe_job is None:  # --list-formats
                return 0
            job = maybe_job
            print(f"NEW job {job.id} [{job.kind.value}]", flush=True)

        task = _create_task(db, job, args)
        status = task.run()
        print(f"STATUS {status.value}", flush=True)
        if status is JobStatus.COMPLETED:
            fresh = db.get_job(job.id)
            print(f"SAVED {(fresh or job).dest_path}", flush=True)
            return 0
        fresh = db.get_job(job.id)
        if fresh is not None and fresh.error:
            print(f"ERROR {fresh.error}", flush=True)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
