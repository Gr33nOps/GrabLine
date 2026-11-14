"""Bounded public downloads: real engines, no private cookies or sessions."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import time

from common import ROOT, digest, record, save

from app.core.models import JobKind, JobStatus
from app.db.database import Database
from app.engines.smart import SmartDownload

SAMPLES = {
    "youtube": ["https://www.youtube.com/watch?v=jNQXAC9IVRw"],
    "vimeo": ["https://vimeo.com/54469442", "https://player.vimeo.com/video/76979871"],
    "x": ["https://twitter.com/starwars/status/665052190608723968"],
    "soundcloud": ["https://soundcloud.com/officialbillieeilish/ocean-eyes"],
    "reddit": ["https://www.reddit.com/r/videos/comments/6rrwyj/that_small_heart_attack/"],
    "instagram": ["https://www.instagram.com/reel/Chunk8-jurw/"],
    "dailymotion": ["https://www.dailymotion.com/video/x84sh87"],
    "twitch": ["https://clips.twitch.tv/FaintLightGullWholeWheat"],
    "bilibili": ["https://www.bilibili.com/video/BV1xx411c7mu"],
    "tiktok": ["https://www.tiktok.com/@scout2015/video/6718335390845095173"],
}


class PublicDownload(SmartDownload):
    def _cookie_browser(self):
        return None

    def _cookie_browser_fallbacks(self, primary):
        return []

    def _build_options(self, **kwargs):
        options = super()._build_options(**kwargs)
        assert not options.get("cookiesfrombrowser") and not options.get("cookiefile")
        options.update(socket_timeout=15, retries=1, fragment_retries=1, max_filesize=8 * 1024**2)
        return options


def worker(name: str, url: str):
    directory = ROOT / "public-media" / name
    directory.mkdir(parents=True, exist_ok=True)
    os.environ["LOCALAPPDATA"] = str(directory / "profile")
    db = Database(directory / "jobs.db")
    started = time.monotonic()
    completed = [
        j
        for j in db.list_jobs()
        if j.url == url and j.status is JobStatus.COMPLETED and j.dest_path.is_file()
    ]
    if completed:
        job, status = completed[-1], JobStatus.COMPLETED
    else:
        job = db.create_job(
            url,
            str(directory),
            "sample.mp4",
            kind=JobKind.SMART,
            options={"format_spec": "worst[ext=mp4]/worst", "trim": [0, 5], "use_session": False},
        )
        status = PublicDownload(db, job, ffmpeg_path=shutil.which("ffmpeg")).run()
    fresh = db.get_job(job.id)
    result = {
        "url": url,
        "job_status": status.value,
        "seconds": round(time.monotonic() - started, 2),
        "reason": fresh.error if fresh else "Job disappeared",
    }
    if status is JobStatus.COMPLETED:
        assert fresh
        target = directory / fresh.filename
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        assert probe.returncode == 0, probe.stderr
        media = json.loads(probe.stdout)
        assert media.get("streams") and float(media["format"]["duration"]) > 0
        result.update(
            status="PASS", sha256=digest(target), bytes=target.stat().st_size, inspection=media
        )
    else:
        result["status"] = "BLOCKED"
    save(f"public-media/{name}/attempt.json", result)
    print(json.dumps(result))
    db.close()


def run(name: str, samples: list[str]):
    attempts = []
    for index, url in enumerate(samples):
        directory = ROOT / "public-media" / f"{name}-{index}"
        directory.mkdir(parents=True, exist_ok=True)
        try:
            process = subprocess.run(
                [sys.executable, __file__, "--worker", f"{name}-{index}", "--url", url],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            (directory / "engine.log").write_text(process.stdout + process.stderr, encoding="utf-8")
            attempt_path = directory / "attempt.json"
            if not attempt_path.exists():
                raise RuntimeError(
                    f"No result, child exit {process.returncode}: {process.stderr[-500:]}"
                )
            result = json.loads(attempt_path.read_text())
        except subprocess.TimeoutExpired:
            result = {"url": url, "status": "BLOCKED", "reason": "120-second public-service bound"}
        except Exception as exc:
            result = {"url": url, "status": "FAIL", "reason": str(exc)}
        attempts.append(result)
        if result["status"] == "PASS":
            break
    status = attempts[-1]["status"]
    record(
        "PUBLIC-" + name.upper(),
        status,
        "external service",
        attempts[-1].get("reason") or "Downloaded media validated by FFprobe",
        attempts=attempts,
        private_sessions_used=False,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker")
    parser.add_argument("--url")
    args = parser.parse_args()
    if args.worker:
        worker(args.worker, args.url)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda item: run(*item), SAMPLES.items()))
