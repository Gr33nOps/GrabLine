"""Additional real DASH, image conversion and installed scanner checks."""

from __future__ import annotations

import json
import shutil
import subprocess

from common import ROOT, case, digest, record
from PIL import Image

from app.core import convert, virusscan
from app.core.models import JobKind, JobStatus
from app.db.database import Database
from app.engines.smart import SmartDownload
from app.tests.media_server import MediaServer

out = ROOT / "media-security"
out.mkdir(exist_ok=True)
ffmpeg = shutil.which("ffmpeg")
assert ffmpeg

for fmt in convert.IMAGE_TARGETS:
    with case("MEDIA-IMAGE-" + fmt.upper(), "real FFmpeg + decoded image validation") as evidence:
        source = out / "source.png"
        Image.new("RGB", (480, 270), (25, 180, 60)).save(source)
        output = convert.convert(ffmpeg, source, fmt)
        with Image.open(output) as image:
            image.load()
            assert image.size == (480, 270)
            color = image.convert("RGB").getpixel((240, 135))
            assert abs(color[1] - 180) < 20 and abs(color[2] - 60) < 20, color
        evidence.update(sha256=digest(output), size=output.stat().st_size, decoded_color=color)

with case("MEDIA-FINITE-DASH", "real local DASH manifest + yt-dlp/FFmpeg transfer") as evidence:
    dash = out / "dash"
    dash.mkdir(exist_ok=True)
    source = ROOT / "fixtures/video.mp4"
    r = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-c",
            "copy",
            "-f",
            "dash",
            "-seg_duration",
            "1",
            "index.mpd",
        ],
        cwd=dash,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert r.returncode == 0, r.stderr
    server = MediaServer()
    server.start()
    db = Database(out / "dash.db")
    try:
        for p in dash.iterdir():
            server.add(
                "/" + p.name,
                p.read_bytes(),
                content_type="application/dash+xml" if p.suffix == ".mpd" else "video/mp4",
            )
        job = db.create_job(
            server.url("/index.mpd"),
            str(out / "download"),
            "dash.mp4",
            kind=JobKind.SMART,
            options={"format_spec": "bv+ba/b"},
        )
        status = SmartDownload(db, job, ffmpeg_path=ffmpeg).run()
        final = db.get_job(job.id)
        assert status is JobStatus.COMPLETED, final.error
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type,codec_name",
                "-of",
                "json",
                str(final.dest_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        assert probe.returncode == 0, probe.stderr
        media = json.loads(probe.stdout)
        assert {s["codec_type"] for s in media["streams"]} >= {"video", "audio"}
        assert float(media["format"]["duration"]) >= 3.8
        evidence.update(job_id=final.id, sha256=digest(final.dest_path), media=media)
    finally:
        db.close()
        server.stop()

benign = out / "benign-scanner-fixture.txt"
benign.write_text("GrabLine synthetic benign scanner integration fixture\n", encoding="utf-8")
scanner = virusscan.find_scanner("auto")
if scanner is None:
    record(
        "SECURITY-INSTALLED-SCANNER",
        "BLOCKED",
        "Windows local scanner CLI",
        "No supported scanner executable found; antivirus settings were not changed",
    )
else:
    with case(
        "SECURITY-INSTALLED-SCANNER", "installed Windows scanner CLI + benign synthetic file"
    ) as evidence:
        try:
            scan = virusscan.scan(benign)
        except Exception as exc:
            # An installed but disabled third-party/Defender service is an infrastructure limit.
            evidence["scanner_unavailable"] = str(exc)
            raise
        assert scan.clean
        evidence.update(scanner=scan.scanner, sha256=digest(benign))
