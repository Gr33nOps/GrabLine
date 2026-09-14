"""The archive manager: preview, extraction (full or selected files),
password handling, and single-file decompression.

Zip and tar (.tar/.tar.gz/.tgz/.tar.xz/.tar.bz2) are handled by the standard
library with a path-traversal guard so a malicious member cannot escape the
destination. Bare .gz/.bz2/.xz files decompress to a sibling file. .rar and
.7z are handled only if a suitable tool is on PATH (7z can also open AES
zips the standard library cannot).

Encrypted archives: ``extract`` tries no password first, then each entry of
``passwords`` in order, and raises PasswordRequired when none opens it - the
UI turns that into a prompt and remembers what worked.
"""

from __future__ import annotations

import bz2
import gzip
import lzma
import shutil
import struct
import subprocess
import tarfile
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from app.core import naming, proc
from app.core.errors import DownloadError

_ZIP_SUFFIXES = (".zip",)
_TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tbz2")
_EXTERNAL_SUFFIXES = (".rar", ".7z")
#: Bare compressed files (one file, no directory) - .tar.gz is a tarball, not this.
_SINGLE_SUFFIXES = (".gz", ".bz2", ".xz")

_SINGLE_MODULES = {".gz": gzip, ".bz2": bz2, ".xz": lzma}

#: Decompression-bomb guard (CWE-409). A 50 KB zip can declare (or stream)
#: 50 GB of zeros; without a ceiling, extraction fills the disk. The cap is
#: generous - real archives, even large game or dataset bundles, land far
#: under it - so it never rejects a legitimate download, only a deliberate
#: bomb. Both the declared total (cheap, pre-extraction) and the actually
#: streamed bytes (for archives that lie about their size) are checked.
_MAX_EXTRACTED_BYTES = 20 * 1024 * 1024 * 1024  # 20 GiB
_BOMB_MESSAGE = (
    "refusing to extract this archive: it expands to more than 20 GB, which "
    "is characteristic of a decompression bomb"
)


class _LimitedWriter:
    """A file object that raises once more than ``limit`` bytes are written,
    so a lying archive can't stream past the declared-size check."""

    def __init__(self, sink: IO[bytes], limit: int) -> None:
        self._sink = sink
        self._remaining = limit

    def write(self, data: bytes) -> int:
        self._remaining -= len(data)
        if self._remaining < 0:
            raise DownloadError(_BOMB_MESSAGE)
        return self._sink.write(data)


class PasswordRequired(DownloadError):
    """The archive is encrypted and none of the offered passwords opened it."""


@dataclass(frozen=True)
class ArchiveEntry:
    """One file (or directory) inside an archive, for the preview panel."""

    name: str
    size: int | None = None
    is_dir: bool = False


def is_archive(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(_ZIP_SUFFIXES + _TAR_SUFFIXES + _EXTERNAL_SUFFIXES + _SINGLE_SUFFIXES)


def _kind(name: str) -> str | None:
    name = name.lower()
    if name.endswith(_TAR_SUFFIXES):  # before _SINGLE: .tar.gz is a tarball
        return "tar"
    if name.endswith(_ZIP_SUFFIXES):
        return "zip"
    if name.endswith(_EXTERNAL_SUFFIXES):
        return "external"
    if name.endswith(_SINGLE_SUFFIXES):
        return "single"
    return None


def _is_within(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _strip_suffix(name: str) -> str:
    lowered = name.lower()
    for suffix in _TAR_SUFFIXES + _ZIP_SUFFIXES + _EXTERNAL_SUFFIXES + _SINGLE_SUFFIXES:
        if lowered.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _dest_dir(path: Path, dest: Path | None) -> Path:
    if dest is not None:
        return dest
    # A folder next to the archive, named after it, never clobbering.
    return naming.unique_path(path.parent / (_strip_suffix(path.name) or "extracted"))


def _wanted(name: str, members: Sequence[str] | None) -> bool:
    """Is this entry selected? A selected directory takes everything under it."""
    if members is None:
        return True
    clean = name.rstrip("/")
    for member in members:
        chosen = member.rstrip("/")
        if clean == chosen or clean.startswith(chosen + "/"):
            return True
    return False


# ------------------------------------------------------------------ preview


def list_entries(path: Path) -> tuple[ArchiveEntry, ...]:
    """The archive's contents, without extracting anything.

    Listing works without a password for every supported format (encryption
    covers file data, not the table of contents).
    """
    if not path.is_file():
        raise DownloadError(f"archive not found: {path}")
    kind = _kind(path.name)
    try:
        if kind == "zip":
            with zipfile.ZipFile(path) as bundle:
                return tuple(
                    ArchiveEntry(info.filename, info.file_size, info.is_dir())
                    for info in bundle.infolist()
                )
        if kind == "tar":
            with tarfile.open(path) as bundle:
                return tuple(
                    ArchiveEntry(m.name, m.size if m.isfile() else None, m.isdir())
                    for m in bundle.getmembers()
                )
        if kind == "single":
            return (ArchiveEntry(_strip_suffix(path.name), _gzip_size(path)),)
        if kind == "external":
            return _list_external(path)
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as exc:
        raise DownloadError(f"the archive could not be read ({exc})") from exc
    raise DownloadError("GrabLine does not know how to read this file type.")


def _gzip_size(path: Path) -> int | None:
    """A .gz footer records the original size (mod 4 GiB); .bz2/.xz record none."""
    if not path.name.lower().endswith(".gz"):
        return None
    try:
        with open(path, "rb") as handle:
            handle.seek(-4, 2)
            return int(struct.unpack("<I", handle.read(4))[0])
    except (OSError, struct.error):
        return None


def _list_external(path: Path) -> tuple[ArchiveEntry, ...]:
    tool = _external_tool(path, listing=True)
    name = Path(tool).name
    if name in ("7z", "7za"):
        result = _run_tool([tool, "l", "-slt", "-p", str(path)])
        return _parse_7z_listing(result.stdout)
    if name == "lsar":
        result = _run_tool([tool, str(path)])
        # First line is "archive.rar: RAR"; the rest are entry paths.
        lines = result.stdout.splitlines()[1:]
        return tuple(ArchiveEntry(line.strip()) for line in lines if line.strip())
    result = _run_tool([tool, "lb", "-p-", str(path)])  # unrar: bare name list
    return tuple(ArchiveEntry(line.strip()) for line in result.stdout.splitlines() if line.strip())


def _parse_7z_listing(output: str) -> tuple[ArchiveEntry, ...]:
    entries: list[ArchiveEntry] = []
    block: dict[str, str] = {}
    in_entries = False
    for line in [*output.splitlines(), ""]:
        if line.startswith("----------"):
            in_entries = True  # everything before this describes the archive itself
            continue
        if not in_entries:
            continue
        if not line.strip():
            if "Path" in block:
                size = int(block["Size"]) if block.get("Size", "").isdigit() else None
                is_dir = block.get("Attributes", "").startswith("D")
                entries.append(ArchiveEntry(block["Path"], None if is_dir else size, is_dir))
            block = {}
            continue
        key, _, value = line.partition(" = ")
        block[key.strip()] = value.strip()
    return tuple(entries)


# --------------------------------------------------------------- extraction


def extract(
    path: Path,
    dest: Path | None = None,
    *,
    passwords: Sequence[str] = (),
    members: Sequence[str] | None = None,
) -> Path:
    """Extract ``path`` into a folder (created next to it by default).

    ``members`` limits extraction to those entries (a directory name takes
    everything under it). Returns the destination folder - or, for a bare
    .gz/.bz2/.xz, the decompressed file. Raises PasswordRequired when the
    archive is encrypted and nothing in ``passwords`` opens it, and
    DownloadError for anything unsupported or unsafe.
    """
    if not path.is_file():
        raise DownloadError(f"archive not found: {path}")
    kind = _kind(path.name)
    if kind == "single":
        return _extract_single(path, dest)
    target = _dest_dir(path, dest)
    target.mkdir(parents=True, exist_ok=True)
    try:
        if kind == "zip":
            _extract_zip(path, target, passwords, members)
        elif kind == "tar":
            _extract_tar(path, target, members)
        elif kind == "external":
            _extract_external(path, target, passwords, members)
        else:
            raise DownloadError("GrabLine does not know how to extract this file type.")
    except (zipfile.BadZipFile, tarfile.TarError) as exc:
        raise DownloadError(f"the archive could not be read ({exc})") from exc
    return target


def _check_declared_size(total: int) -> None:
    """Refuse before extracting when the archive's own table of contents adds
    up to more than the bomb ceiling. Cheap - no bytes written yet."""
    if total > _MAX_EXTRACTED_BYTES:
        raise DownloadError(_BOMB_MESSAGE)


def _extract_single(path: Path, dest: Path | None) -> Path:
    """Decompress a bare .gz/.bz2/.xz next to itself (data.csv.gz -> data.csv)."""
    name = _strip_suffix(path.name) or path.name + ".out"
    target = naming.unique_path((dest or path.parent) / name)
    target.parent.mkdir(parents=True, exist_ok=True)
    module = _SINGLE_MODULES[Path(path.name.lower()).suffix]
    try:
        # .bz2/.xz declare no size, so the ceiling is enforced on the stream
        # itself: the writer raises the moment output passes the cap.
        with module.open(path, "rb") as source, open(target, "wb") as sink:
            shutil.copyfileobj(source, _LimitedWriter(sink, _MAX_EXTRACTED_BYTES))
    except (OSError, EOFError, lzma.LZMAError) as exc:
        target.unlink(missing_ok=True)
        raise DownloadError(f"the file could not be decompressed ({exc})") from exc
    except DownloadError:
        target.unlink(missing_ok=True)
        raise
    return target


def _extract_zip(
    path: Path, target: Path, passwords: Sequence[str], members: Sequence[str] | None
) -> None:
    with zipfile.ZipFile(path) as bundle:
        selected = [n for n in bundle.namelist() if _wanted(n, members)]
        for member in selected:
            if not _is_within(target, target / member):
                raise DownloadError("refusing to extract an archive with unsafe paths")
        chosen = frozenset(selected)
        _check_declared_size(
            sum(info.file_size for info in bundle.infolist() if info.filename in chosen)
        )
        encrypted = any(
            info.flag_bits & 0x1 for info in bundle.infolist() if _wanted(info.filename, members)
        )
        if not encrypted:
            bundle.extractall(target, members=selected)
            return
        for password in passwords:
            try:
                bundle.extractall(target, members=selected, pwd=password.encode())
                return
            except RuntimeError:
                continue  # wrong password - try the next saved one
            except NotImplementedError:
                # AES zip: beyond the standard library; 7z handles it.
                _extract_external(path, target, passwords, members)
                return
    raise PasswordRequired("this archive is password-protected")


def _extract_tar(path: Path, target: Path, members: Sequence[str] | None) -> None:
    with tarfile.open(path) as bundle:
        selected = [m for m in bundle.getmembers() if _wanted(m.name, members)]
        for member in selected:
            if not _is_within(target, target / member.name):
                raise DownloadError("refusing to extract an archive with unsafe paths")
        _check_declared_size(sum(m.size for m in selected if m.isfile()))
        # data filter (Python 3.12+) strips setuid bits, device nodes, etc.
        bundle.extractall(target, members=selected, filter="data")


def _external_tool(path: Path, *, listing: bool = False) -> str:
    """The best available tool for a .rar/.7z (or an AES .zip via 7z)."""
    rar = path.name.lower().endswith(".rar")
    if listing:
        candidates = ("7z", "7za", "lsar", "unrar") if rar else ("7z", "7za", "lsar")
    else:
        candidates = ("7z", "7za", "unar", "unrar") if rar else ("7z", "7za", "unar")
    for name in candidates:
        tool = shutil.which(name)
        if tool:
            return tool
    raise DownloadError(
        f"working with {path.suffix} files needs an external tool "
        "(install '7z' or 'unar') that GrabLine could not find."
    )


def _run_tool(command: list[str]) -> subprocess.CompletedProcess[str]:
    # stdin closed so a tool that wants to prompt for a password fails fast
    # instead of hanging. Arg list only, never a shell string (S1).
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=600,
        **proc.hidden(),
    )


def _guard_external_entries(path: Path, target: Path, members: Sequence[str] | None) -> None:
    """Refuse a .rar/.7z whose listing shows a member escaping the target, or
    summing past the bomb ceiling - before the external tool runs (CWE-22 /
    CWE-409). zip and tar are guarded in-process; the external tools are
    trusted to sanitize paths themselves, and old unrar/7z builds did not
    (e.g. CVE-2022-30333). Listing costs one cheap tool invocation.
    """
    total = 0
    for entry in _list_external(path):
        if not _wanted(entry.name, members):
            continue
        if not _is_within(target, target / entry.name):
            raise DownloadError("refusing to extract an archive with unsafe paths")
        total += entry.size or 0
    _check_declared_size(total)


def _extract_external(
    path: Path, target: Path, passwords: Sequence[str], members: Sequence[str] | None
) -> None:
    _guard_external_entries(path, target, members)
    tool = _external_tool(path)
    name = Path(tool).name
    selected = list(members or ())
    last_output = ""
    for password in ["", *passwords]:
        if name in ("7z", "7za"):
            # -p always present so 7z never prompts; empty means "no password".
            command = [tool, "x", "-y", f"-o{target}", f"-p{password}", str(path), *selected]
        elif name == "unrar":
            command = [tool, "x", "-y", f"-p{password or '-'}", str(path), *selected]
            command.append(str(target) + "/")
        else:  # unar
            command = [tool, "-force-overwrite", "-output-directory", str(target)]
            if password:
                command += ["-password", password]
            command += [str(path), *selected]
        result = _run_tool(command)
        if result.returncode == 0:
            return
        last_output = (result.stderr or result.stdout or "").strip()
        if "password" not in last_output.lower():
            break  # a real error, not a password mismatch - don't keep guessing
    if "password" in last_output.lower():
        raise PasswordRequired("this archive is password-protected")
    tail = last_output.splitlines()[-1:] or ["extraction failed"]
    raise DownloadError(f"could not extract the archive ({tail[0]})")
