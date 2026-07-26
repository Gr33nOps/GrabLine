"""JavaScript runtime (Deno) discovery and fetch-on-first-need (S5).

YouTube's current extraction path solves an "n challenge" that needs a real
JavaScript runtime. Without one, the signed-in (cookie) web client returns no
downloadable formats - which is why turning on 'browser session' used to make
videos fail with "Requested format is not available". yt-dlp already bundles
the solver scripts and can drive Deno; GrabLine provisions Deno exactly the
way it does FFmpeg: the pinned per-platform archive is downloaded over HTTPS,
hashed while streaming, rejected on any mismatch, and only then extracted -
never a blanket archive extract, never executed before it is verified.

The runtime is only fetched when the user has opted into browser session, so
plain public videos (which need no JS runtime) never trigger the download.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import httpx

from app.core import net, paths, proc
from app.core.errors import DownloadError
from app.core.ffmpeg import platform_key
from app.core.ffmpeg_pins import PinnedArchive

log = logging.getLogger(__name__)

#: Serialize provisioning: concurrent YouTube jobs on first run must not each
#: download Deno into the same dir (they'd corrupt each other / lock the file
#: on Windows). The first installs; the rest wait and reuse it.
_install_lock = threading.Lock()

#: Pinned Deno release. Bump with scripts (hashes are the official per-target
#: zip SHA-256). Deno >= 2.3.0 is what yt-dlp requires; this is well past that.
DENO_VERSION = "v2.9.2"

#: platform key (app.core.ffmpeg.platform_key) -> Deno release target triple.
_DENO_TARGETS: dict[str, str] = {
    "linux-x86_64": "x86_64-unknown-linux-gnu",
    "linux-aarch64": "aarch64-unknown-linux-gnu",
    "windows-x86_64": "x86_64-pc-windows-msvc",
    "darwin-x86_64": "x86_64-apple-darwin",
    "darwin-arm64": "aarch64-apple-darwin",
}

#: SHA-256 of each target's release zip for DENO_VERSION.
_DENO_SHA256: dict[str, str] = {
    "linux-x86_64": "934d1bd5cb09eaed7f2e4a4fc58208d04a3c5c0fcde9f319d93d735265c67a4a",
    "linux-aarch64": "310b8f48e59964ff18890d35e64f64fb90e8b1cc5d9ebff8c818327d5afb16d2",
    "windows-x86_64": "5fe194d26ac5ef77fcc5288c2c438c7a0465f3b6180440ebf04092714bf2dcdf",
    "darwin-x86_64": "c953379e5a85a0a30e99aa51b807633e380e809a1181f53e4904d5fa73785bff",
    "darwin-arm64": "687ae485168ba73a4f1ee3a954eb4f077eca82f2fefd236a6a83a3889287876c",
}


def deno_pin(key: str | None = None) -> PinnedArchive | None:
    """The verified archive for this platform, or None if unsupported."""
    key = key or platform_key()
    target = _DENO_TARGETS.get(key)
    sha = _DENO_SHA256.get(key)
    if target is None or sha is None:
        return None
    return PinnedArchive(
        url=f"https://dl.deno.land/release/{DENO_VERSION}/deno-{target}.zip",
        sha256=sha,
        format="zip",
    )


def _deno_name() -> str:
    return "deno.exe" if sys.platform == "win32" else "deno"


def managed_deno(bin_dir: Path | None = None) -> Path:
    return (bin_dir or paths.bin_dir()) / _deno_name()


def find_deno(bin_dir: Path | None = None) -> str | None:
    """Resolution order: managed install > any Deno already on PATH."""
    managed = managed_deno(bin_dir)
    if managed.is_file():
        return str(managed)
    return shutil.which("deno")


#: JS runtimes yt-dlp can drive, as (yt-dlp name, executable). Deno first -
#: it's what yt-dlp recommends and what we can auto-install; then whatever the
#: user already has. yt-dlp does NOT auto-enable node/bun/quickjs even when
#: installed, so we must detect them and pass them in explicitly.
_JS_RUNTIMES: tuple[tuple[str, str], ...] = (
    ("deno", "deno"),
    ("node", "node"),
    ("bun", "bun"),
    ("quickjs", "qjs"),
)


def detect_js_runtime(bin_dir: Path | None = None) -> tuple[str, str] | None:
    """The (yt-dlp name, path) of a JavaScript runtime already available -
    our managed Deno first, then any Deno/Node/Bun/QuickJS on PATH. None when
    nothing is installed (the caller then downloads Deno)."""
    managed = managed_deno(bin_dir)
    if managed.is_file():
        return ("deno", str(managed))
    for name, exe in _JS_RUNTIMES:
        found = shutil.which(exe)
        if found:
            return (name, found)
    return None


def ensure_deno(
    *,
    bin_dir: Path | None = None,
    pin: PinnedArchive | None = None,
    progress: Callable[[int, int | None], None] | None = None,
    verify_run: bool = True,
    proxy: str | None = None,
) -> Path:
    """Return a usable Deno binary, downloading and verifying it on first need.
    Raises DownloadError (with a user-facing message) on any failure - most
    importantly on a SHA-256 mismatch, where nothing is installed."""
    existing = find_deno(bin_dir)
    if existing:
        return Path(existing)
    with _install_lock:
        # Another job may have installed it while we waited for the lock.
        existing = find_deno(bin_dir)
        if existing:
            return Path(existing)
        target_dir = bin_dir or paths.bin_dir()
        archive = pin or deno_pin()
        if archive is None:
            raise DownloadError(
                f"no pinned Deno build for this platform ({platform_key()}); install "
                "Deno yourself and GrabLine will pick it up from PATH"
            )
        target_dir.mkdir(parents=True, exist_ok=True)
        with net.build_client(
            proxy=proxy,
            follow_redirects=True,
            timeout=httpx.Timeout(60.0, connect=15.0),
        ) as client:
            _install_archive(client, archive, target_dir, progress)
    deno_path = managed_deno(target_dir)
    if not deno_path.is_file():
        raise DownloadError("the verified Deno archive did not contain a deno binary")
    if verify_run:
        result = subprocess.run(  # argument list only - no shell, ever (S1)
            [str(deno_path), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            **proc.hidden(),
        )
        if result.returncode != 0:
            raise DownloadError("the installed Deno binary failed to run")
        log.info("installed %s", result.stdout.splitlines()[0] if result.stdout else "deno")
    return deno_path


def _install_archive(
    client: httpx.Client,
    archive: PinnedArchive,
    target_dir: Path,
    progress: Callable[[int, int | None], None] | None,
) -> Path:
    digest = hashlib.sha256()
    received = 0
    # Close the spool handle before reopening for extraction: Windows refuses
    # a second handle (WinError 32) while ours is still open.
    spool_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as spool:
            spool_path = Path(spool.name)
            with client.stream("GET", archive.url) as response:
                if response.status_code != 200:
                    raise DownloadError(f"Deno download failed (HTTP {response.status_code})")
                length = response.headers.get("content-length")
                total = int(length) if length and length.isdigit() else None
                for chunk in response.iter_bytes(1 << 20):
                    digest.update(chunk)
                    spool.write(chunk)
                    received += len(chunk)
                    if progress is not None:
                        progress(received, total)
        if digest.hexdigest() != archive.sha256:
            raise DownloadError(
                "Deno download failed its integrity check (SHA-256 mismatch) - "
                "refusing to install. Try again later."
            )
        return _extract_deno(spool_path, target_dir)
    except httpx.HTTPError as exc:
        raise DownloadError(f"could not download Deno: {exc}") from exc
    finally:
        if spool_path is not None:
            spool_path.unlink(missing_ok=True)


def _extract_deno(archive_path: Path, target_dir: Path) -> Path:
    """Pull only the deno binary out by name - never a blanket extract."""
    name = _deno_name()
    with zipfile.ZipFile(archive_path) as bundle:
        for member in bundle.namelist():
            if PurePosixPath(member).name == name and not member.endswith("/"):
                destination = target_dir / name
                with bundle.open(member) as src, open(destination, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                destination.chmod(0o755)
                return destination
    raise DownloadError("the Deno archive did not contain the expected binary")
