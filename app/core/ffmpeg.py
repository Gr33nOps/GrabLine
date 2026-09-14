"""FFmpeg discovery and fetch-on-first-run with pinned SHA-256 verification (S5).

The app never bundles FFmpeg. When it's needed and not present, the pinned
archive for this platform is downloaded over HTTPS, hashed while streaming,
rejected outright on any mismatch, and only then are the ffmpeg/ffprobe
binaries extracted (by explicit member name - never a blanket archive extract).
"""

from __future__ import annotations

import hashlib
import logging
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import httpx

from app.core import net, paths, proc
from app.core.errors import DownloadError
from app.core.ffmpeg_pins import PINS, PinnedArchive
from app.core.settings import Settings

log = logging.getLogger(__name__)

_BINARY_NAMES = {"ffmpeg", "ffprobe", "ffmpeg.exe", "ffprobe.exe"}


def platform_key() -> str:
    system = {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "aarch64": "aarch64", "arm64": "arm64"}.get(machine, machine)
    if system == "linux" and machine == "arm64":
        machine = "aarch64"
    if system == "darwin" and machine == "aarch64":
        machine = "arm64"
    return f"{system}-{machine}"


def _executable(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


def managed_binary(name: str, bin_dir: Path | None = None) -> Path:
    return (bin_dir or paths.bin_dir()) / _executable(name)


def find_ffmpeg(settings: Settings | None = None) -> str | None:
    """Resolution order: explicit setting > managed install > PATH."""
    if settings is not None and settings.ffmpeg_path:
        candidate = Path(settings.ffmpeg_path)
        if candidate.is_file():
            return str(candidate)
    managed = managed_binary("ffmpeg")
    if managed.is_file():
        return str(managed)
    return shutil.which("ffmpeg")


def ensure_ffmpeg(
    *,
    bin_dir: Path | None = None,
    pins: dict[str, tuple[PinnedArchive, ...]] | None = None,
    progress: Callable[[int, int | None], None] | None = None,
    verify_run: bool = True,
    proxy: str | None = None,
) -> Path:
    """Download, verify, and install FFmpeg for this platform. Returns the
    ffmpeg binary path. Raises DownloadError with a user-facing message on
    any failure - most importantly on a checksum mismatch.
    """
    target_dir = bin_dir or paths.bin_dir()
    # Serialized because the archive unpacks straight over the target: two
    # videos queued together both find no FFmpeg, both fetch ~30 MB, and the
    # second unpack rewrites the binary the first one is already running -
    # which surfaces as a merge failing on a half-written executable.
    with _install_lock:
        return _install_ffmpeg(target_dir, pins, progress, verify_run, proxy)


_install_lock = threading.Lock()


def _install_ffmpeg(
    target_dir: Path,
    pins: dict[str, tuple[PinnedArchive, ...]] | None,
    progress: Callable[[int, int | None], None] | None,
    verify_run: bool,
    proxy: str | None,
) -> Path:
    key = platform_key()
    # `pins is None` check, not truthiness: an empty mapping must mean
    # "no pinned builds", never "fall back to the real ones".
    archives = (PINS if pins is None else pins).get(key)
    if not archives:
        raise DownloadError(
            f"no pinned FFmpeg build for this platform ({key}); "
            "install FFmpeg manually and set its path in Settings"
        )
    target_dir.mkdir(parents=True, exist_ok=True)
    with net.build_client(
        proxy=proxy,
        follow_redirects=True,
        timeout=httpx.Timeout(60.0, connect=15.0),
    ) as client:
        for archive in archives:
            _install_archive(client, archive, target_dir, progress)
    ffmpeg_path = managed_binary("ffmpeg", target_dir)
    if not ffmpeg_path.is_file():
        raise DownloadError("the verified FFmpeg archive did not contain an ffmpeg binary")
    if verify_run:
        result = subprocess.run(  # argument list only - no shell, ever (S1)
            [str(ffmpeg_path), "-version"],
            capture_output=True,
            text=True,
            timeout=30,
            **proc.hidden(),
        )
        if result.returncode != 0:
            raise DownloadError("the installed FFmpeg binary failed to run")
        log.info("installed %s", result.stdout.splitlines()[0] if result.stdout else "ffmpeg")
    return ffmpeg_path


def _install_archive(
    client: httpx.Client,
    archive: PinnedArchive,
    target_dir: Path,
    progress: Callable[[int, int | None], None] | None,
) -> list[Path]:
    digest = hashlib.sha256()
    received = 0
    # The spool handle must be CLOSED before the archive is reopened for
    # extraction and before the unlink: Windows refuses both while another
    # handle is open (WinError 32), even our own.
    spool_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=f".{archive.format}", delete=False) as spool:
            spool_path = Path(spool.name)
            with client.stream("GET", archive.url) as response:
                if response.status_code != 200:
                    raise DownloadError(f"FFmpeg download failed (HTTP {response.status_code})")
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
                "FFmpeg download failed its integrity check (SHA-256 mismatch) - "
                "refusing to install. Try again later; if this persists the pins "
                "may need updating."
            )
        return _extract_binaries(spool_path, archive.format, target_dir)
    except httpx.HTTPError as exc:
        raise DownloadError(f"could not download FFmpeg: {exc}") from exc
    finally:
        if spool_path is not None:
            spool_path.unlink(missing_ok=True)


def _extract_binaries(archive_path: Path, archive_format: str, target_dir: Path) -> list[Path]:
    """Pull out only ffmpeg/ffprobe by name; everything else stays packed."""
    written: list[Path] = []
    if archive_format == "zip":
        with zipfile.ZipFile(archive_path) as bundle:
            for member in bundle.namelist():
                name = PurePosixPath(member).name
                if name in _BINARY_NAMES and not member.endswith("/"):
                    destination = target_dir / name
                    with bundle.open(member) as src, open(destination, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    written.append(destination)
    else:
        with tarfile.open(archive_path, "r:xz") as bundle:
            for entry in bundle.getmembers():
                name = PurePosixPath(entry.name).name
                if name in _BINARY_NAMES and entry.isfile():
                    source = bundle.extractfile(entry)
                    if source is None:  # pragma: no cover - isfile() guards this
                        continue
                    destination = target_dir / name
                    with source, open(destination, "wb") as dst:
                        shutil.copyfileobj(source, dst)
                    written.append(destination)
    for binary in written:
        binary.chmod(0o755)
    return written
