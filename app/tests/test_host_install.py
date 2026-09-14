from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from app.native_host import CHROME_EXTENSION_IDS, FIREFOX_EXTENSION_IDS, HOST_NAME
from app.native_host.install import (
    browser_targets,
    check,
    frozen_host_path,
    install,
    is_store_python,
    write_launcher,
)


def test_linux_targets_cover_the_major_browsers(tmp_path: Path):
    targets = {t.browser: t for t in browser_targets("linux", home=tmp_path)}
    assert set(targets) == {"Chrome", "Chromium", "Edge", "Brave", "Vivaldi", "Firefox"}
    assert targets["Firefox"].kind == "firefox"
    assert targets["Chrome"].manifest_dir == (
        tmp_path / ".config" / "google-chrome" / "NativeMessagingHosts"
    )


def test_darwin_targets(tmp_path: Path):
    targets = {t.browser: t for t in browser_targets("darwin", home=tmp_path)}
    assert "Chrome" in targets and "Firefox" in targets
    assert "Library" in str(targets["Chrome"].manifest_dir)
    # Arc is macOS-first and keeps its manifests under its User Data dir.
    assert "Arc" in targets and "Vivaldi" in targets
    assert str(targets["Arc"].manifest_dir).endswith("Arc/User Data/NativeMessagingHosts")


def test_frozen_host_path_none_from_source():
    # Running under pytest is not a frozen build, so there is no sibling exe.
    assert frozen_host_path() is None


def test_frozen_host_path_points_at_sibling_exe(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/opt/Grabline/grabline")
    monkeypatch.setattr(sys, "platform", "linux")
    assert frozen_host_path() == Path("/opt/Grabline/grabline-host")


def test_frozen_install_points_manifests_at_the_host_exe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # A frozen build registers the sibling grabline-host exe directly - no
    # launcher wrapper script is written.
    exe = tmp_path / "grabline"
    exe.write_text("")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))

    written = install(platform="linux", home=tmp_path, bin_dir=tmp_path / "bin")

    assert len(written) == 6
    manifest = json.loads(written[0].read_text())
    assert manifest["path"] == str(tmp_path / "grabline-host")
    assert not (tmp_path / "bin" / "grabline-host").exists()  # no wrapper written


def test_install_writes_manifests_and_launcher(tmp_path: Path):
    written = install(platform="linux", home=tmp_path, bin_dir=tmp_path / "bin")

    assert len(written) == 6
    launcher = tmp_path / "bin" / "grabline-host"
    assert launcher.exists()
    assert os.access(launcher, os.X_OK)
    assert sys.executable in launcher.read_text()

    chrome_manifest = json.loads(
        (
            tmp_path / ".config" / "google-chrome" / "NativeMessagingHosts" / f"{HOST_NAME}.json"
        ).read_text()
    )
    assert chrome_manifest["name"] == HOST_NAME
    assert chrome_manifest["type"] == "stdio"
    assert chrome_manifest["path"] == str(launcher)
    assert chrome_manifest["allowed_origins"] == [
        f"chrome-extension://{ext_id}/" for ext_id in CHROME_EXTENSION_IDS
    ]
    assert "allowed_extensions" not in chrome_manifest

    firefox_manifest = json.loads(
        (tmp_path / ".mozilla" / "native-messaging-hosts" / f"{HOST_NAME}.json").read_text()
    )
    assert firefox_manifest["allowed_extensions"] == list(FIREFOX_EXTENSION_IDS)
    assert "allowed_origins" not in firefox_manifest


@pytest.mark.skipif(sys.platform == "win32", reason="posix launcher script")
def test_launcher_script_end_to_end(tmp_path: Path):
    """The launcher must work exactly as a browser runs it: from a foreign
    working directory, without inheriting our PYTHONPATH. This is the test
    that catches 'pairs on the dev box, dead on a user machine' bugs."""
    launcher = write_launcher(tmp_path / "bin")
    message = json.dumps({"type": "ping"}).encode()
    env = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "PWD")}
    env["XDG_DATA_HOME"] = str(tmp_path / "data")  # isolated db + log
    result = subprocess.run(
        [str(launcher)],
        input=struct.pack("<I", len(message)) + message,
        capture_output=True,
        timeout=60,
        cwd="/",
        env=env,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    (length,) = struct.unpack("<I", result.stdout[:4])
    reply = json.loads(result.stdout[4 : 4 + length])
    assert reply["type"] == "pong"


def test_launcher_pins_pythonpath(tmp_path: Path):
    content = write_launcher(tmp_path / "bin").read_text()
    assert "-m app.native_host" in content
    assert "PYTHONPATH" in content  # so the browser's own cwd can't hide the package


@pytest.mark.skipif(sys.platform == "win32", reason="posix launcher script")
def test_check_reports_healthy_after_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The --check doctor: full pairing = OK lines and a live pong."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    install(platform="linux", home=tmp_path)
    healthy, lines = check(platform="linux", home=tmp_path)
    assert healthy, "\n".join(lines)
    assert any("pong" in line for line in lines)


def test_check_flags_missing_pairing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    healthy, lines = check(platform="linux", home=tmp_path)
    assert not healthy
    assert any(line.startswith("FAIL") for line in lines)


def test_store_python_is_recognized():
    """The Microsoft Store Python sandboxes file writes - pairing made with
    it is invisible to browsers, so it must be detected and named."""
    assert is_store_python(r"C:\Users\GREEN\AppData\Local\Microsoft\WindowsApps\python.exe")
    assert is_store_python(
        r"C:\Program Files\WindowsApps"
        r"\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe"
    )
    assert not is_store_python(r"C:\Python313\python.exe")
    assert not is_store_python(r"C:\Users\GREEN\proj\.venv\Scripts\python.exe")
    assert not is_store_python("/usr/bin/python3")


def test_dry_run_writes_nothing(tmp_path: Path, capsys):
    written = install(dry_run=True, platform="linux", home=tmp_path, bin_dir=tmp_path / "bin")
    assert written == []
    assert not (tmp_path / ".config").exists()
    assert not (tmp_path / "bin").exists()
    assert "would write" in capsys.readouterr().out


def test_extension_manifest_pins_match_host_pins():
    """The IDs pinned in the host manifests must match extension/manifest.json."""
    manifest = json.loads(
        (Path(__file__).resolve().parents[2] / "extension" / "manifest.json").read_text()
    )
    assert manifest["browser_specific_settings"]["gecko"]["id"] in FIREFOX_EXTENSION_IDS
    # The Chrome ID is derived from the "key" field: sha256 of the DER key,
    # first 32 hex chars mapped onto a-p.
    import base64
    import hashlib

    der = base64.b64decode(manifest["key"])
    digest = hashlib.sha256(der).hexdigest()[:32]
    derived_id = "".join(chr(ord("a") + int(c, 16)) for c in digest)
    assert derived_id in CHROME_EXTENSION_IDS


def test_extension_requests_blocking_webrequest():
    """Proactive interception cancels a download at the network layer, before
    the browser ever shows it, instead of letting it flash then cancelling via
    downloads.onCreated. That needs blocking webRequest, which Firefox honours
    in MV3; without the permission the listener can't register."""
    manifest = json.loads(
        (Path(__file__).resolve().parents[2] / "extension" / "manifest.json").read_text()
    )
    assert "webRequestBlocking" in manifest["permissions"]
