# Packaging GrabLine

Native packages are built on their target OS by the
[Release workflow](../.github/workflows/release.yml). Use
[RELEASING.md](../RELEASING.md) for versioning/publication; do not create a tag
just to try a local build.

## Bundle and outputs

The [PyInstaller spec](grabline.spec) builds two executables in a shared bundle:

- `grabline`: the desktop GUI, windowed on Windows.
- `grabline-host`: the console Native Messaging host, preserving stdin/stdout
  framing even where the GUI executable has no console streams.

The app registers the native host against its actual runtime path. Keep both
executables and the shared support files together.

| Target | Release outputs | Packaging tool |
| --- | --- | --- |
| Windows x64 | Setup EXE and portable ZIP | Inno Setup plus PyInstaller bundle |
| macOS Apple Silicon | DMG containing the app | `iconutil`, `hdiutil` |
| Linux x86-64 | AppImage, tarball and DEB | AppImage and Debian scripts under `linux/` |
| Browser extension | Chrome and Firefox ZIPs | `scripts/package_extension.py` |

Each OS also publishes a `dist-requirements-<runner>.txt` dependency inventory.
The publish job adds `SHA256SUMS.txt`. The current release has 12 assets.

## Build locally

Use Python 3.12 on the target OS. The current release workflow pins
PyInstaller **6.22.3** and Pillow **12.3.0**; check the workflow when updating
build tools. Dependencies are recorded but not all transitively locked, so
record `pip freeze --all` for each build rather than assuming bit-identical output.

With the environment active, from the source root:

```sh
python -m pip install -e . "pyinstaller==6.22.3" "pillow==12.3.0"
python packaging/make_icons.py
python -m PyInstaller packaging/grabline.spec --noconfirm --distpath dist --workpath build
```

For headless icon generation, set `QT_QPA_PLATFORM=offscreen` for that process.
On Windows, the environment's `python.exe` can be called directly instead of
activation. The frozen Windows bundle is under `dist/grabline`.

### Windows installer

Install Inno Setup, then call its actual installed `ISCC.exe` path from PowerShell:

```powershell
$buildVersion = .\.venv\Scripts\python.exe -c 'import app; print(app.__version__)'
# Adjust this example path to the installed Inno Setup compiler.
& 'C:\Program Files\Inno Setup 7\ISCC.exe' "/DAppVersion=$buildVersion" packaging/windows/grabline.iss
```

The workflow searches both Program Files layouts rather than assuming a compiler
version. Copy `windows/PORTABLE-README.txt` into the bundle before creating a
portable ZIP. Test installation, upgrade and uninstall only in a disposable guest.

### Linux wrappers

```sh
build_version=$(python -c 'import app; print(app.__version__)')
bash packaging/linux/build_appimage.sh "$build_version"
tar -C dist -czf "dist/Grabline-$build_version-linux-x86_64.tar.gz" grabline
bash packaging/linux/build_deb.sh "$build_version"
```

Run the AppImage step before the tarball; it also stages required Qt/xcb support.
See the workflow for native Linux prerequisites and macOS ICNS/DMG staging.

### Extension packages

```sh
python scripts/package_extension.py --out dist
```

The ZIPs are store-targeted and unminified. They are not an AMO-signed XPI or a
replacement for the pinned source manifest used in unpacked development.
See [GrabLine Connect](../extension/README.md) and the
[store listing kit](../docs/store-listing.md).

## Verify the shipped package

Run the GUI and native host from the actual frozen bundle, check version and
browser pairing, and complete a deterministic download with its expected hash.
Inspect the final published files as well as local build output. Use the
[audit harness](../scripts/windows_qa/README.md) for evidence; its packaged
smoke tests and guest integration are distinct scopes.

Builds are unsigned. FFmpeg/Deno are fetched separately with checksum checks.
GitHub Packages contains an informational image at `ghcr.io/grabline/grabline-app`
(releases before the move to the GrabLine organization: `ghcr.io/gr33nops/grabline-app`);
it is not a runnable desktop container. End users install from
[the latest release](https://github.com/GrabLine/GrabLine/releases/latest).
The public [download website](https://grabline.github.io/#download) is maintained
in [its own repository](https://github.com/GrabLine/grabline.github.io); packaging
does not bundle or deploy the site. The browser extension remains in this repo
and in the desktop bundle.
