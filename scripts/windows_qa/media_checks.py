"""Real HTML5 media discovery, subtitles, chapters and transformed outputs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import httpx
from common import ROOT, case, digest, save
from PIL import Image

from app.core import convert, gif
from app.core.models import JobKind, JobStatus
from app.db.database import Database
from app.engines.smart import SmartDownload
from app.tests.media_server import MediaServer

root = ROOT / "media-expanded"
root.mkdir(exist_ok=True)
manifest = json.loads((ROOT / "browser-manifest.json").read_text())
server = MediaServer()
server.start()
for name, content_type in [
    ("video.mp4", "video/mp4"),
    ("blue.png", "image/png"),
    ("subtitles.en.vtt", "text/vtt"),
]:
    response = httpx.get(manifest["base"] + "/" + name)
    response.raise_for_status()
    server.add("/" + name, response.content, content_type=content_type)
schema = {
    "@context": "https://schema.org",
    "@type": "VideoObject",
    "name": "QA fixture title",
    "description": "Synthetic metadata",
    "duration": "PT4S",
    "thumbnailUrl": server.url("/blue.png"),
    "hasPart": [
        {"@type": "Clip", "name": "First half", "startOffset": 0, "endOffset": 2},
        {"@type": "Clip", "name": "Second half", "startOffset": 2, "endOffset": 4},
    ],
}
html = (
    (
        "<html><title>QA fixture title</title><meta "
        'property="og:description" content="Synthetic metadata">'
    )
    + '<script type="application/ld+json">'
    + json.dumps(schema)
    + "</script>"
    '<video controls poster="/blue.png" src="/video.mp4"><track kind="subtitles" srclang="en" '
    'label="English" src="/subtitles.en.vtt"></video></html>'
)
url = server.add("/media.html", html.encode(), content_type="text/html")
schema["contentUrl"] = server.url("/video.mp4")
chapter_html = (
    '<html><title>QA fixture title</title><script type="application/ld+json">'
    + json.dumps(schema)
    + "</script></html>"
)
chapter_url = server.add("/chapters.html", chapter_html.encode(), content_type="text/html")
ffmpeg = shutil.which("ffmpeg")
assert ffmpeg
db = Database(root / "jobs.db")


def inspect(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-show_chapters",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


try:
    for name, options in [
        ("video", {}),
        ("mp3", {"audio_format": "mp3"}),
        ("m4a", {"audio_format": "m4a"}),
        ("flac", {"audio_format": "flac"}),
        ("subtitles", {"subtitles": {"lang": "en", "auto": False, "embed": True}}),
        ("chapters", {"chapters": True}),
        ("sidecars", {"save_metadata": True, "save_thumbnail": True}),
        ("trim", {"trim": [1, 3]}),
    ]:
        if (
            os.environ.get("GRABLINE_QA_MEDIA_ONLY")
            and name != os.environ["GRABLINE_QA_MEDIA_ONLY"]
        ):
            continue
        with case("MEDIA-REAL-" + name.upper(), "local media integration") as evidence:
            directory = root / name
            directory.mkdir(exist_ok=True)
            job = db.create_job(
                chapter_url if name == "chapters" else url,
                str(directory),
                "fixture.mp4",
                kind=JobKind.SMART,
                options={"format_spec": "b", **options},
            )
            status = SmartDownload(db, job, ffmpeg_path=ffmpeg).run()
            fresh = db.get_job(job.id)
            assert status is JobStatus.COMPLETED, fresh.error if fresh else None
            assert fresh
            target = fresh.dest_path
            media = inspect(target)
            streams = media["streams"]
            if name in {"mp3", "m4a", "flac"}:
                expected = {"mp3": "mp3", "m4a": "aac", "flac": "flac"}[name]
                assert any(s["codec_name"] == expected for s in streams)
                assert not any(
                    s["codec_type"] == "video" and not s["disposition"]["attached_pic"]
                    for s in streams
                )
                if name == "mp3":
                    assert any(s["disposition"]["attached_pic"] for s in streams)
            else:
                assert any(s["codec_type"] == "video" for s in streams)
            duration = float(media["format"]["duration"])
            assert abs(duration - (2 if name == "trim" else 4)) < 0.35, duration
            if name == "subtitles":
                assert any(s["codec_type"] == "subtitle" for s in streams)
                text = subprocess.check_output(
                    [ffmpeg, "-v", "error", "-i", str(target), "-map", "0:s:0", "-f", "srt", "-"],
                    text=True,
                    encoding="utf-8",
                )
                assert "GrabLine QA caption" in text
                evidence["subtitle_text"] = text
            if name == "chapters":
                assert [c["tags"]["title"] for c in media["chapters"]] == [
                    "First half",
                    "Second half",
                ], media["chapters"]
            if name == "sidecars":
                assert list(directory.glob("*.info.json")) and list(directory.glob("*.png"))
            assert "QA fixture title" in media["format"].get("tags", {}).get("title", "")
            save(f"media-expanded/{name}/ffprobe.json", media)
            evidence.update(
                job_id=job.id,
                file=str(target),
                sha256=digest(target),
                bytes=target.stat().st_size,
                duration=duration,
            )
    if os.environ.get("GRABLINE_QA_MEDIA_ONLY"):
        raise SystemExit(0)
    source = root / "video/fixture.mp4"
    with case("MEDIA-REAL-GIF", "local media integration") as evidence:
        target = gif.make_gif(ffmpeg, source, start=1, end=3, fps=8, width=160)
        with Image.open(target) as image:
            assert image.width == 160 and image.n_frames >= 15
            evidence.update(width=image.width, frames=image.n_frames, sha256=digest(target))
    for target_format in convert.VIDEO_TARGETS + convert.AUDIO_TARGETS:
        with case("MEDIA-CONVERT-" + target_format.upper(), "local media integration") as evidence:
            target = convert.convert(ffmpeg, source, target_format)
            media = inspect(target)
            assert media.get("streams") and float(media["format"]["duration"]) > 3.5
            evidence.update(file=str(target), sha256=digest(target))
finally:
    server.stop()
    db.close()
