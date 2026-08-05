"""Register the Native Messaging host with installed browsers (F1.1).

Writes the host manifests browsers look up by name, each pinning the allowed
extension IDs (S3), plus a small launcher script the manifests point at.

Usage:
    python -m app.native_host.install            # register for this user
    python -m app.native_host.install --dry-run  # show what would be written
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from app.core import paths
from app.native_host import CHROME_EXTENSION_IDS, FIREFOX_EXTENSION_IDS, HOST_NAME

#: Windows registry roots browsers search for host manifests (HKCU). Opera and
#: Arc on Windows read Chrome's key, so registering Chrome covers them too.
_WINDOWS_REGISTRY_ROOTS: tuple[tuple[str, str, str], ...] = (
    ("Chrome", "chromium", r"Software\Google\Chrome\NativeMessagingHosts"),
    ("Chromium", "chromium", r"Software\Chromium\NativeMessagingHosts"),
    ("Edge", "chromium", r"Software\Microsoft\Edge\NativeMessagingHosts"),
    ("Brave", "chromium", r"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts"),
    ("Vivaldi", "chromium", r"Software\Vivaldi\NativeMessagingHosts"),
    ("Firefox", "firefox", r"Software\Mozilla\NativeMessagingHosts"),
)


@dataclass(frozen=True)
class BrowserTarget:
    browser: str
    manifest_dir: Path
    kind: str  # "chromium" | "firefox"


def _linux_targets(home: Path, *, use_env: bool) -> list[BrowserTarget]:
    # XDG_CONFIG_HOME only applies to the real home: an explicitly passed
    # ``home`` must fully determine the result (callers isolating a fake
    # home would otherwise leak into the real one).
    if use_env:
        config = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
    else:
        config = home / ".config"
    # Opera reads Chrome's manifest directory, so it needs no entry of its own.
    return [
        BrowserTarget("Chrome", config / "google-chrome" / "NativeMessagingHosts", "chromium"),
        BrowserTarget("Chromium", config / "chromium" / "NativeMessagingHosts", "chromium"),
        BrowserTarget("Edge", config / "microsoft-edge" / "NativeMessagingHosts", "chromium"),
        BrowserTarget(
            "Brave",
            config / "BraveSoftware" / "Brave-Browser" / "NativeMessagingHosts",
            "chromium",
        ),
        BrowserTarget("Vivaldi", config / "vivaldi" / "NativeMessagingHosts", "chromium"),
        BrowserTarget("Firefox", home / ".mozilla" / "native-messaging-hosts", "firefox"),
    ]


def _darwin_targets(home: Path) -> list[BrowserTarget]:
    support = home / "Library" / "Application Support"
    # Opera reads Chrome's manifest directory, so it needs no entry of its own.
    return [
        BrowserTarget("Chrome", support / "Google" / "Chrome" / "NativeMessagingHosts", "chromium"),
        BrowserTarget("Chromium", support / "Chromium" / "NativeMessagingHosts", "chromium"),
        BrowserTarget("Edge", support / "Microsoft Edge" / "NativeMessagingHosts", "chromium"),
        BrowserTarget(
            "Brave",
            support / "BraveSoftware" / "Brave-Browser" / "NativeMessagingHosts",
            "chromium",
        ),
        BrowserTarget("Vivaldi", support / "Vivaldi" / "NativeMessagingHosts", "chromium"),
        BrowserTarget("Arc", support / "Arc" / "User Data" / "NativeMessagingHosts", "chromium"),
        BrowserTarget("Firefox", support / "Mozilla" / "NativeMessagingHosts", "firefox"),
    ]


def browser_targets(platform: str | None = None, home: Path | None = None) -> list[BrowserTarget]:
    platform = platform or sys.platform
    use_env = home is None
    home = home or Path.home()
    if platform == "darwin":
        return _darwin_targets(home)
    if platform == "win32":
        # Windows resolves manifests via registry keys; handled in install().
        return []
    return _linux_targets(home, use_env=use_env)


def host_manifest(kind: str, launcher: Path) -> dict[str, object]:
    manifest: dict[str, object] = {
        "name": HOST_NAME,
        "description": "GrabLine download manager connector",
        "path": str(launcher),
        "type": "stdio",
    }
    if kind == "firefox":
        manifest["allowed_extensions"] = list(FIREFOX_EXTENSION_IDS)
    else:
        manifest["allowed_origins"] = [
            f"chrome-extension://{ext_id}/" for ext_id in CHROME_EXTENSION_IDS
        ]
    return manifest


def is_store_python(executable: str | None = None) -> bool:
    """The Microsoft Store build of Python runs sandboxed: its writes to
    %LOCALAPPDATA% and HKCU are silently redirected into a private container,
    so the manifests/launcher it 'writes' are invisible to browsers and
    pairing can never work. Detect it and say so."""
    exe = (executable or sys.executable).lower().replace("/", "\\")
    return "\\windowsapps\\" in exe or "pythonsoftwarefoundation" in exe


_STORE_PYTHON_MESSAGE = (
    "this is the Microsoft Store build of Python - its sandbox hides every "
    "file it writes from your browsers, so pairing cannot work. Install "
    "Python from python.org, disable the Store aliases (Settings > Apps > "
    "Advanced app settings > App execution aliases > turn off python.exe), "
    "reinstall GrabLine with it, and pair again."
)


def _package_root() -> Path:
    """The directory containing the ``app`` package."""
    return Path(paths.__file__).resolve().parents[2]


def frozen_host_path() -> Path | None:
    """In a PyInstaller build the Native Messaging host is a sibling console
    executable (``grabline-host``); the browser manifests point straight at it,
    so no launcher script is written. None when running from source."""
    if not getattr(sys, "frozen", False):
        return None
    exe = "grabline-host.exe" if sys.platform == "win32" else "grabline-host"
    return Path(sys.executable).with_name(exe)


def write_launcher(bin_dir: Path | None = None) -> Path:
    """A tiny script that runs ``python -m app.native_host``; the manifests
    point at it. PYTHONPATH is pinned to where the ``app`` package lives,
    because browsers spawn the host from their own working directory."""
    target_dir = bin_dir or paths.bin_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    executable = sys.executable
    if sys.platform == "win32":  # pragma: no cover - windows-only branch
        # pythonw.exe avoids a console window flashing up when the browser
        # spawns the host. newline="\r\n" with \n content - text mode would
        # double the \r and the stray char corrupts the command line (the bug
        # that broke Windows pairing entirely).
        windowless = Path(sys.executable).with_name("pythonw.exe")
        if windowless.exists():
            executable = str(windowless)
        launcher = target_dir / "grabline-host.bat"
        lines = [
            "@echo off",
            f'set "PYTHONPATH={_package_root()};%PYTHONPATH%"',
            f'"{executable}" -m app.native_host %*',
        ]
        with open(launcher, "w", newline="\r\n") as handle:
            handle.write("\n".join(lines) + "\n")
    else:
        launcher = target_dir / "grabline-host"
        lines = [
            "#!/bin/sh",
            f'PYTHONPATH="{_package_root()}${{PYTHONPATH:+:$PYTHONPATH}}"',
            "export PYTHONPATH",
            f'exec "{executable}" -m app.native_host "$@"',
        ]
        launcher.write_text("\n".join(lines) + "\n")
        launcher.chmod(0o755)
    return launcher


def install(
    *,
    dry_run: bool = False,
    platform: str | None = None,
    home: Path | None = None,
    bin_dir: Path | None = None,
) -> list[Path]:
    """Write manifests for every known browser location. Returns paths written."""
    platform = platform or sys.platform
    frozen = frozen_host_path()
    if frozen is not None:
        launcher = frozen  # installed grabline-host exe; no wrapper script needed
    elif not dry_run:
        launcher = write_launcher(bin_dir)
    else:
        launcher = paths.bin_dir() / "grabline-host"
    written: list[Path] = []
    if platform == "win32":  # pragma: no cover - registry path, windows-only
        return _install_windows_registry(launcher, dry_run=dry_run)
    for target in browser_targets(platform, home):
        manifest_path = target.manifest_dir / f"{HOST_NAME}.json"
        payload = json.dumps(host_manifest(target.kind, launcher), indent=2) + "\n"
        if dry_run:
            print(f"would write {manifest_path}")
            continue
        target.manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(payload, encoding="utf-8")
        written.append(manifest_path)
    return written


def _install_windows_registry(
    launcher: Path, *, dry_run: bool
) -> list[Path]:  # pragma: no cover - windows-only
    if sys.platform == "win32":
        import winreg

        manifest_dir = paths.data_dir() / "native_host"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for _browser, kind, registry_root in _WINDOWS_REGISTRY_ROOTS:
            manifest_path = manifest_dir / f"{HOST_NAME}.{kind}.json"
            if dry_run:
                print(f"would write {manifest_path} and HKCU\\{registry_root}\\{HOST_NAME}")
                continue
            manifest_path.write_text(
                json.dumps(host_manifest(kind, launcher), indent=2) + "\n", encoding="utf-8"
            )
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{registry_root}\{HOST_NAME}") as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(manifest_path))
            written.append(manifest_path)
        return written
    raise RuntimeError("registry registration is Windows-only")


# ------------------------------------------------------------- the doctor


def _launcher_path() -> Path:
    frozen = frozen_host_path()
    if frozen is not None:
        return frozen
    name = "grabline-host.bat" if sys.platform == "win32" else "grabline-host"
    return paths.bin_dir() / name


def _check_manifest(browser: str, manifest_path: Path, lines: list[str]) -> bool:
    if not manifest_path.exists():
        lines.append(f"FAIL {browser}: manifest missing at {manifest_path}")
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        lines.append(f"FAIL {browser}: manifest unreadable ({exc})")
        return False
    host_path = Path(str(manifest.get("path", "")))
    if not host_path.exists():
        lines.append(f"FAIL {browser}: manifest points at a missing launcher: {host_path}")
        return False
    lines.append(f"OK   {browser}: manifest -> {host_path}")
    return True


def _check_ping(launcher: Path, lines: list[str]) -> bool:
    """Spawn the real launcher exactly like a browser would and expect a pong."""
    payload = json.dumps({"type": "ping"}).encode()
    try:
        result = subprocess.run(  # argument list only - no shell (S1)
            [str(launcher)],
            input=struct.pack("<I", len(payload)) + payload,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        lines.append(f"FAIL live test: could not run the launcher ({exc})")
        return False
    if len(result.stdout) >= 4:
        (length,) = struct.unpack("<I", result.stdout[:4])
        try:
            reply = json.loads(result.stdout[4 : 4 + length])
        except (json.JSONDecodeError, UnicodeDecodeError):
            reply = None
        if isinstance(reply, dict) and reply.get("type") == "pong":
            lines.append(
                f"OK   live test: host replied pong (appRunning={reply.get('appRunning')})"
            )
            return True
    stderr_tail = result.stderr.decode(errors="replace").strip().splitlines()[-3:]
    detail = " | ".join(stderr_tail) if stderr_tail else f"exit code {result.returncode}"
    lines.append(f"FAIL live test: no pong from the host - {detail}")
    return False


def check(platform: str | None = None, home: Path | None = None) -> tuple[bool, list[str]]:
    """Verify the whole pairing chain; returns (healthy, report lines)."""
    platform = platform or sys.platform
    lines: list[str] = []
    healthy = True

    if platform == "win32" and not getattr(sys, "frozen", False) and is_store_python():
        lines.append(f"FAIL {_STORE_PYTHON_MESSAGE}")
        healthy = False

    launcher = _launcher_path()
    if launcher.exists():
        lines.append(f"OK   launcher exists: {launcher}")
    else:
        lines.append(f"FAIL launcher missing: {launcher} - run pairing (Settings -> Pair browsers)")
        healthy = False

    if platform == "win32" and sys.platform == "win32":  # pragma: no cover - windows-only
        import winreg

        for browser, _kind, registry_root in _WINDOWS_REGISTRY_ROOTS:
            try:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, rf"{registry_root}\{HOST_NAME}"
                ) as key:
                    value, _type = winreg.QueryValueEx(key, "")
            except OSError:
                lines.append(f"--   {browser}: not registered (fine if not installed)")
                continue
            healthy &= _check_manifest(browser, Path(str(value)), lines)
    else:
        for target in browser_targets(platform, home):
            manifest_path = target.manifest_dir / f"{HOST_NAME}.json"
            if not manifest_path.exists():
                lines.append(f"--   {target.browser}: not registered (fine if not installed)")
                continue
            healthy &= _check_manifest(target.browser, manifest_path, lines)

    if launcher.exists():
        healthy &= _check_ping(launcher, lines)
    return healthy, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print targets, write nothing")
    parser.add_argument(
        "--check", action="store_true", help="diagnose an existing pairing, write nothing"
    )
    args = parser.parse_args(argv)
    if args.check:
        healthy, lines = check()
        print("\n".join(lines))
        print("pairing looks healthy" if healthy else "pairing is broken - see FAIL lines above")
        return 0 if healthy else 1
    if sys.platform == "win32" and not getattr(sys, "frozen", False) and is_store_python():
        print(f"ERROR: {_STORE_PYTHON_MESSAGE}")
        return 1
    written = install(dry_run=args.dry_run)
    for path in written:
        print(f"registered {path}")
    if written:
        print(
            "Pairing complete. Load the extension (see extension/README.md), "
            "then the right-click menu and toolbar button will reach GrabLine."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
