"""Disposable loopback browser fixtures; publishes its actual port and hashes."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import threading

from common import ROOT, save
from PIL import Image

from app.tests.media_server import MediaServer, _Handler, payload, sha256

media = ROOT / "fixtures"
media.mkdir(exist_ok=True)
ffmpeg = shutil.which("ffmpeg")
assert ffmpeg, "FFmpeg missing"
video = media / "video.mp4"
subprocess.run(
    [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=320x180:r=25:d=4",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=4",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(video),
    ],
    check=True,
    creationflags=subprocess.CREATE_NO_WINDOW,
)
subprocess.run(
    [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video),
        "-c",
        "copy",
        "-f",
        "hls",
        "-hls_time",
        "1",
        "-hls_list_size",
        "0",
        "index.m3u8",
    ],
    cwd=media,
    check=True,
    creationflags=subprocess.CREATE_NO_WINDOW,
)
subprocess.run(
    [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video),
        "-vn",
        "-c:a",
        "libmp3lame",
        str(media / "audio.mp3"),
    ],
    check=True,
    creationflags=subprocess.CREATE_NO_WINDOW,
)
server = MediaServer()
server.start()
manifest = {"title": "GrabLine complete QA", "base": server.url(""), "resources": {}}


def add(path: str, data: bytes, content_type: str, **kwargs: object) -> str:
    url = server.add(path, data, content_type=content_type, **kwargs)
    manifest["resources"][path] = {"url": url, "bytes": len(data), "sha256": sha256(data)}
    return url


for name, size, delay in [("file.bin", 1024**2, 0), ("slow.bin", 16 * 1024**2, 0.12)]:
    add(
        "/" + name,
        payload(size, 81),
        "application/octet-stream",
        delay_per_chunk=delay,
        content_disposition=f'attachment; filename="{name}"',
    )
for path in media.iterdir():
    content_type = {
        ".mp4": "video/mp4",
        ".mp3": "audio/mpeg",
        ".m3u8": "application/vnd.apple.mpegurl",
        ".ts": "video/mp2t",
    }.get(path.suffix)
    if content_type:
        add("/" + path.name, path.read_bytes(), content_type)
for name, size, color in [
    ("blue", (480, 270), "blue"),
    ("green", (480, 270), "green"),
    ("small", (32, 32), "gray"),
]:
    image = io.BytesIO()
    Image.new("RGB", size, color).save(image, format="PNG")
    add(f"/{name}.png", image.getvalue(), "image/png")
add(
    "/subtitles.en.vtt",
    b"WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nGrabLine QA caption\n",
    "text/vtt",
)
add("/favicon.ico", b"", "image/x-icon")


def post_attachment(self: _Handler) -> None:
    size = int(self.headers.get("Content-Length", "0"))
    self.rfile.read(size)
    data = b"test-only POST export\n"
    self.send_response(200)
    self.send_header("Content-Type", "application/octet-stream")
    self.send_header("Content-Disposition", 'attachment; filename="post-export.txt"')
    self.send_header("Content-Length", str(len(data)))
    self.end_headers()
    self.wfile.write(data)


_Handler.do_POST = post_attachment
html = (
    '<!doctype html><meta charset="utf-8"><title>GrabLine complete '
    "QA</title>\n"
    "<style>body{font:16px Segoe UI;max-width:1000px;margin:20px "
    "auto}section{padding:16px;margin:12px;border:1px solid "
    "#888}video{width:480px}img{width:240px}button{padding:10px}</styl"
    "e>\n"
    '<h1>GrabLine complete QA</h1><section><h2>Files</h2><a id="file" '
    'href="/file.bin">Download test file</a><br><a id="slow" '
    'href="/slow.bin">Download slow test file</a></section>\n'
    '<section><h2>Media</h2><video id="video" controls '
    'preload="metadata" src="/video.mp4"><track default '
    'kind="subtitles" srclang="en" src="/subtitles.en.vtt"></video><br'
    '><audio id="audio" controls src="/audio.mp3"></audio></section>\n'
    '<section><h2>Images</h2><a href="/blue.png"><img id="blue" '
    'alt="Large blue image" src="/blue.png"></a><img id="green" '
    'alt="Large green image" src="/green.png"><img id="small" '
    'alt="Small filtered image" style="width:32px" '
    'src="/small.png"><a href="/blue.png">Duplicate blue image '
    "link</a></section>\n"
    '<section><h2>Selections</h2><p id="selection">BASE/file.bin?case='
    "selection-a<br>BASE/video.mp4?case=selection-b</p></section>\n"
    "<section><h2>Dynamic, frames and shadow players</h2><button "
    'id="insert" onclick="const v=document.createElement(\'video\');v.id'
    "='dynamic';v.controls=true;v.src='/video.mp4?case=dynamic';docume"
    "nt.querySelector('#dynamic-host').append(v)\">Insert "
    'video</button><div id="dynamic-host"></div><iframe '
    'title="Fixture frame" src="/frame.html" width="540" '
    'height="260"></iframe><div id="shadow"></div></section>\n'
    '<section><h2>Browser-only downloads</h2><button id="blob" '
    "onclick=\"const a=document.createElement('a');a.href=window.URL.cr"
    "eateObjectURL(new Blob(['test-only generated "
    "blob']));a.download='blob-export.txt';a.click()\">Download "
    'generated blob</button><form action="/post" '
    'method="post"><button id="post" type="submit">Download POST '
    "export</button></form></section>\n"
    "<script>fetch('/index.m3u8');const "
    "r=document.querySelector('#shadow').attachShadow({mode:'open'});r"
    '.innerHTML=\'<video id="shadow-video" controls width="480" '
    'src="/video.mp4?case=shadow"></video>\';</script>\n'
).replace("BASE", server.url(""))
manifest["page"] = add("/index.html", html.encode(), "text/html")
add(
    "/frame.html",
    (
        b"<!doctype html><title>GrabLine frame</title>"
        b'<video controls width="480" src="/video.mp4?case=frame"></video>'
    ),
    "text/html",
)
add(
    "/health.json",
    json.dumps({"title": manifest["title"], "ready": True}).encode(),
    "application/json",
)
save("browser-manifest.json", manifest)
print(json.dumps(manifest), flush=True)
try:
    threading.Event().wait()
finally:
    server.stop()
