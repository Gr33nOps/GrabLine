"""A wall-clock soak with verified outputs, bounded retention and live metrics."""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import psutil
from common import ROOT, digest, record, save
from torrent_probe import transfer

from app.core import rss, torrentwatch
from app.core.manager import DownloadManager
from app.core.models import JobKind, JobStatus
from app.core.settings import Settings
from app.db.database import Database
from app.engines.torrent import SESSION, create_torrent_file
from app.tests.media_server import MediaServer, payload, sha256


def batch(number: int, stage: int, server: MediaServer, media_base: str) -> dict:
    directory = ROOT / "soak" / f"batch-{number:06d}"
    directory.mkdir(parents=True, exist_ok=True)
    db = Database(directory / "jobs.db")
    settings = Settings(db)
    settings.check_updates = False
    settings.download_dir = directory / "downloads"
    settings.auto_retry = True
    if stage == 2:
        settings.connections = 1
        settings.speed_limit_kbps = 512
    manager = DownloadManager(db, settings=settings, max_concurrent=4)
    verified = False
    expected = sha256(server.resources["/payload.bin"].data)
    try:
        jobs = [manager.add_url(server.url("/payload.bin") + f"?batch={number}")]
        cloud_url = server.url("/protected.bin").replace("http://", "webdav://qa:qa-only@")
        jobs.append(manager.add_cloud(cloud_url))
        if stage in (0, 3):
            smart = db.create_job(
                media_base + "/video.mp4",
                str(directory / "downloads"),
                "smart.mp4",
                kind=JobKind.SMART,
                options={"format_spec": "b"},
            )
            manager.resume(smart.id)
            jobs.append(smart)
            jobs.append(manager.add_hls(media_base + "/index.m3u8", title="Finite HLS"))
        if stage == 2:
            jobs.append(manager.add_url(server.url("/flaky.bin") + f"?batch={number}"))
            jobs.append(manager.add_url(server.url("/pause.bin") + f"?batch={number}"))
        resumed = stage != 2
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            current = [db.get_job(j.id) for j in jobs]
            progress = {view.id: view.downloaded for view in manager.snapshot()}
            if (
                not resumed
                and current[-1]
                and 0 < progress[jobs[-1].id] < len(server.resources["/pause.bin"].data)
            ):
                manager.pause(jobs[-1].id)
                pause_deadline = time.monotonic() + 8
                while (
                    time.monotonic() < pause_deadline
                    and db.get_job(jobs[-1].id).status is not JobStatus.PAUSED
                ):
                    time.sleep(0.05)
                assert db.get_job(jobs[-1].id).status is JobStatus.PAUSED
                manager.resume(jobs[-1].id)
                resumed = True
            if any(j and j.status is JobStatus.FAILED for j in current):
                raise AssertionError([(j.status.value, j.error) for j in current if j])
            if all(j and j.status is JobStatus.COMPLETED for j in current):
                break
            time.sleep(0.1)
        outputs = []
        assert resumed, "Pause/resume path was not exercised"
        for original in jobs:
            job = db.get_job(original.id)
            assert job and job.status is JobStatus.COMPLETED, job
            file = Path(job.dest_dir) / job.filename
            actual = digest(file)
            if job.kind in (JobKind.DIRECT, JobKind.CLOUD):
                assert actual == expected, (job.kind, actual, expected)
            else:
                result = subprocess.run(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration:stream=codec_type",
                        "-of",
                        "json",
                        str(file),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                assert result.returncode == 0, result.stderr
                media = json.loads(result.stdout)
                assert float(media["format"]["duration"]) >= 3.8
                assert any(s["codec_type"] == "video" for s in media["streams"])
            outputs.append({"kind": job.kind.value, "bytes": file.stat().st_size, "sha256": actual})
        verified = True
        return {"batch": number, "stage": stage, "outputs": outputs}
    finally:
        manager.shutdown()
        db.close()
        target = directory.resolve()
        assert target.is_relative_to((ROOT / "soak").resolve()) and target.name.startswith("batch-")
        if verified:
            shutil.rmtree(target)


def run(seconds: int, build: bool) -> None:
    manifest = json.loads((ROOT / "browser-manifest.json").read_text())
    server = MediaServer()
    server.start()
    data = payload(2 * 1024**2, 777)
    server.add("/payload.bin", data, delay_per_chunk=0.002)
    token = base64.b64encode(b"qa:qa-only").decode()
    server.add("/protected.bin", data, required_headers={"Authorization": "Basic " + token})
    server.add("/flaky.bin", data, cut_after=128 * 1024, cut_from=2, cut_until=3)
    server.add("/pause.bin", data, chunk_size=32768, delay_per_chunk=0.04)
    started = time.monotonic()
    started_utc = datetime.now(UTC).isoformat()
    save(
        "soak-start.json",
        {
            "started_utc": started_utc,
            "seconds": seconds,
            "torrent_engine_sha256": digest(
                Path(__file__).resolve().parents[2] / "app/engines/torrent.py"
            ),
        },
    )
    stage_duration = seconds / 4
    process = psutil.Process()
    build_process = None
    completed = 0
    trace_file = (ROOT / "soak-events.jsonl").open("a", encoding="utf-8")
    try:
        while time.monotonic() - started < seconds:
            elapsed = time.monotonic() - started
            stage = min(3, int(elapsed // stage_duration))
            assert shutil.disk_usage(ROOT).free > 4 * 1024**3, "Less than 4 GiB free; soak stopped"
            if stage == 1:
                if build and build_process is None:
                    environment = {
                        **os.environ,
                        "QT_QPA_PLATFORM": "offscreen",
                        "LOCALAPPDATA": str(ROOT / "build-profile"),
                    }
                    environment["PATH"] = os.pathsep.join(
                        [os.environ["SYSTEMROOT"] + r"\System32", os.environ["SYSTEMROOT"]]
                    )
                    source = Path(__file__).resolve().parents[2]
                    build_process = subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "PyInstaller",
                            "packaging/grabline.spec",
                            "--noconfirm",
                            "--clean",
                            "--workpath",
                            str(ROOT / "stress-build"),
                            "--distpath",
                            str(ROOT / "stress-dist"),
                        ],
                        cwd=source,
                        env=environment,
                        stdout=(ROOT / "stress-build.log").open("w"),
                        stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                outcome = transfer(
                    1000 + completed,
                    magnet=bool(completed % 2),
                    seed_after=bool(completed % 4 // 2),
                )
            else:
                outcome = batch(completed, stage, server, manifest["base"])
                if stage == 3:
                    watch_dir = ROOT / "soak-watch"
                    watch_dir.mkdir(exist_ok=True)
                    sample = watch_dir / "payload.bin"
                    sample.write_bytes(data[:65536])
                    torrent = watch_dir / "sample.torrent"
                    torrent.write_bytes(create_torrent_file(sample))
                    watcher = torrentwatch.TorrentWatcher()
                    assert not watcher.scan(watch_dir).ready
                    assert watcher.scan(watch_dir).ready == (torrent,)
                    xml = f'<rss><channel><item><title>QA batch {completed}</title><enclosure url="{
                        server.url("/payload.bin")
                    }"/></item></channel></rss>'
                    server.add("/rss.xml", xml.encode(), content_type="application/rss+xml")
                    assert (
                        rss.fetch_feed(server.url("/rss.xml"))[0].title == f"QA batch {completed}"
                    )
            completed += 1
            metrics = {
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "stage": stage,
                "rss_bytes": process.memory_info().rss,
                "handles": process.num_handles(),
                "cpu_percent": process.cpu_percent(),
                "free_bytes": shutil.disk_usage(ROOT).free,
                "outcome": outcome,
            }
            trace_file.write(json.dumps(metrics, default=str) + "\n")
            trace_file.flush()
            save(
                "soak-progress.json",
                {
                    "started_utc": started_utc,
                    "target_seconds": seconds,
                    "completed_batches": completed,
                    **metrics,
                },
            )
            time.sleep(min(1, max(0, seconds - (time.monotonic() - started))))
        if build_process:
            assert build_process.poll() == 0, "Overlapping Windows build failed or did not finish"
        record(
            "SOAK-ONE-HOUR" if seconds == 3600 else "SOAK-SMOKE",
            "PASS",
            "sustained local integration",
            "Every completed output verified",
            duration_seconds=round(time.monotonic() - started, 2),
            target_seconds=seconds,
            batches=completed,
            overlapping_build=build,
        )
    except Exception as exc:
        record(
            "SOAK-ONE-HOUR" if seconds == 3600 else "SOAK-SMOKE",
            "FAIL",
            "sustained local integration",
            str(exc),
            elapsed_seconds=time.monotonic() - started,
            batches=completed,
        )
        raise
    finally:
        server.stop()
        trace_file.close()
        SESSION.detach()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=3600)
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()
    run(args.seconds, args.build)
