"""Build store-ready extension zips (F2.5).

Chrome Web Store and AMO each want a slightly different manifest than the
cross-browser one we develop with:

- **chrome**: no ``key`` (the store assigns identity and rejects manifests
  that carry one: unpacked dev installs keep using the in-repo manifest),
  no ``browser_specific_settings``, background as ``service_worker`` only.
- **firefox**: keeps ``browser_specific_settings`` (the pinned gecko id),
  background as ``scripts`` (event page) only, no ``key``.

Usage: python scripts/package_extension.py [--out dist]
"""

from __future__ import annotations

import argparse
import copy
import json
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
EXTENSION_DIR = ROOT / "extension"

#: Everything shipped to the stores; README and dev files stay out.
_SHIPPED_GLOBS = (
    "background.js",
    "content/**/*.js",
    "popup/**/*",
    "icons/*.png",
)


def _shipped_files() -> list[Path]:
    files: list[Path] = []
    for pattern in _SHIPPED_GLOBS:
        files.extend(p for p in EXTENSION_DIR.glob(pattern) if p.is_file())
    return sorted(set(files))


def _manifest_for(target: str, manifest: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(manifest)
    result.pop("key", None)
    if target == "chrome":
        result.pop("browser_specific_settings", None)
        result["background"] = {"service_worker": manifest["background"]["service_worker"]}
    elif target == "firefox":
        result["background"] = {"scripts": manifest["background"]["scripts"]}
        # Chromium-only permissions (download-shelf hiding); Firefox has no
        # such API and AMO validation should see a clean permission list.
        result["permissions"] = [
            p
            for p in manifest.get("permissions", [])
            if p not in ("downloads.shelf", "downloads.ui")
        ]
    else:  # pragma: no cover - argparse restricts choices
        raise ValueError(f"unknown target: {target}")
    return result


def build_zip(target: str, out_dir: Path) -> Path:
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text())
    version = manifest["version"]
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"grabline-connect-{target}-{version}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(
            "manifest.json", json.dumps(_manifest_for(target, manifest), indent=2) + "\n"
        )
        for path in _shipped_files():
            bundle.write(path, path.relative_to(EXTENSION_DIR).as_posix())
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    parser.add_argument("--target", choices=("chrome", "firefox"), action="append", default=None)
    args = parser.parse_args()
    for target in args.target or ("chrome", "firefox"):
        archive = build_zip(target, args.out)
        contents = zipfile.ZipFile(archive).namelist()
        print(f"{archive}  ({len(contents)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
