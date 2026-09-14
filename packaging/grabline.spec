# PyInstaller spec: builds two executables into one shared onedir bundle.
#
#   grabline       - the windowed desktop GUI
#   grabline-host  - the console Native Messaging host (needs real stdio)
#
# Sharing one bundle (via MERGE) means PySide6/yt-dlp are packed once, not
# twice. Build:  pyinstaller packaging/grabline.spec --noconfirm
#
# Cross-platform: this same spec runs on the Windows/macOS/Linux GitHub
# runners; per-OS installers are assembled from the resulting bundle.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

SPEC_DIR = Path(SPECPATH).resolve()
ROOT = SPEC_DIR.parent


def _icon(name: str) -> str | None:
    """Use a platform icon only if it's been generated; None keeps the build
    working without it (PyInstaller errors on a missing icon path)."""
    path = SPEC_DIR / name
    return str(path) if path.exists() else None

# yt-dlp imports its 1000+ site extractors lazily; curl_cffi ships a native
# libcurl. Both must be collected explicitly or the frozen app can't resolve
# YouTube et al. PySide6's own PyInstaller hook handles Qt plugins. The h2
# stack is imported lazily by httpx only when http2=True, so it must be
# collected too or frozen builds silently lose HTTP/2.
ytdlp_datas, ytdlp_bins, ytdlp_hidden = collect_all("yt_dlp")
curl_datas, curl_bins, curl_hidden = collect_all("curl_cffi")

hidden = ytdlp_hidden + curl_hidden + collect_submodules("app")
for h2_pkg in ("h2", "hpack", "hyperframe"):
    hidden += collect_submodules(h2_pkg)
# libtorrent is imported lazily by the torrent engine; collect its binary
# extension explicitly or frozen builds lose the torrent client.
hidden += ["libtorrent", "psutil"]
# The cloud engine imports paramiko/boto3/keyring lazily; collect them (and
# boto3's bundled service data) so SFTP/S3/credential storage work when frozen.
for cloud_pkg in ("paramiko", "boto3", "botocore", "keyring", "keyring.backends"):
    hidden += collect_submodules(cloud_pkg)
boto_datas, boto_bins, boto_hidden = collect_all("botocore")
hidden += boto_hidden
# Ship the browser extension inside the app so the Browser Setup wizard can
# stage it (Load unpacked / temporary add-on) - an installed build has no repo
# checkout to copy it from. browser_setup._source_extension_dir() reads it back
# from sys._MEIPASS/extension.
datas = (
    ytdlp_datas
    + curl_datas
    + boto_datas
    + [(str(ROOT / "extension"), "extension")]
    # The brand logo, read back from sys._MEIPASS/app/ui/assets (app.ui.icon).
    + [(str(ROOT / "app" / "ui" / "assets"), "app/ui/assets")]
    # Translation catalogs, read back from sys._MEIPASS/app/i18n (app.core.i18n).
    + [(str(ROOT / "app" / "i18n"), "app/i18n")]
)
binaries = ytdlp_bins + curl_bins + boto_bins

# Trim Qt modules the app never touches - keeps the bundle from ballooning.
qt_excludes = [
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.Qt3DCore",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebView",
    "PySide6.QtMultimedia",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtPdf",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtSensors",
    "PySide6.QtPositioning",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtDesigner",
]

a_gui = Analysis(
    [str(SPEC_DIR / "entry_gui.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=qt_excludes + ["tkinter"],
)
a_host = Analysis(
    [str(SPEC_DIR / "entry_host.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=collect_submodules("app.native_host") + collect_submodules("app.core"),
    excludes=["PySide6", "tkinter"],  # the host never touches Qt
)

# MERGE dedupes shared dependencies: grabline-host reuses grabline's copies.
MERGE((a_gui, "grabline", "grabline"), (a_host, "grabline-host", "grabline-host"))

pyz_gui = PYZ(a_gui.pure)
pyz_host = PYZ(a_host.pure)

exe_gui = EXE(
    pyz_gui,
    a_gui.scripts,
    [],
    exclude_binaries=True,
    name="grabline",
    console=False,  # windowed: no console flashes on launch
    icon=_icon("grabline.ico"),
)
exe_host = EXE(
    pyz_host,
    a_host.scripts,
    [],
    exclude_binaries=True,
    name="grabline-host",
    console=True,  # console subsystem: real stdio for Native Messaging
)

coll = COLLECT(
    exe_gui,
    a_gui.binaries,
    a_gui.datas,
    exe_host,
    a_host.binaries,
    a_host.datas,
    name="grabline",
)

# macOS: wrap the GUI in a .app bundle (Spotlight-searchable, Dock icon).
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Grabline.app",
        icon=_icon("grabline.icns"),
        bundle_identifier="dev.grabline.app",
        info_plist={
            "CFBundleName": "Grabline",
            "CFBundleDisplayName": "GrabLine",
            "CFBundleExecutable": "grabline",
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
        },
    )
