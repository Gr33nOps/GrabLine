# Packaging GrabLine

Turns the Python/PySide6 app into native desktop installers for Windows,
macOS, and Linux. Releases are built by
[`.github/workflows/release.yml`](../.github/workflows/release.yml) on native
GitHub runners, one per operating system, so each installer is built on the
platform it targets.

## What gets built

PyInstaller freezes **two** executables into one shared bundle
([`grabline.spec`](grabline.spec)):

- `grabline`: the windowed desktop GUI.
- `grabline-host`: the **console** Native Messaging host. It's separate
  because a windowed GUI exe has no working stdio on Windows, which the
  browser needs to talk to the host. The installed browser manifests point
  straight at this binary (`native_host.install.frozen_host_path()`).

Then, per OS:

| OS | Tool | Output |
|---|---|---|
| Windows | Inno Setup ([`windows/grabline.iss`](windows/grabline.iss)) | `Grabline-Setup-<ver>.exe` |
| macOS | `hdiutil` (from the `.app` the spec builds) | `Grabline-<ver>-applesilicon.dmg` |
| Linux | `appimagetool` ([`linux/build_appimage.sh`](linux/build_appimage.sh)) | `Grabline-<ver>-x86_64.AppImage` |

The app registers the Native Messaging host itself on first run of an
installed build (`_register_native_host_once` in `app/__main__.py`), so the
installers don't need to know install paths at build time.

## Cut a release

```bash
git tag v1.3.5
git push origin v1.3.5      # -> Actions builds all three and publishes a Release
```

Or trigger the workflow manually (Actions tab) to get installers as artifacts
without publishing.

## GitHub Packages

Each release also publishes a small linked image to
[GitHub Packages](https://github.com/Gr33nOps/GrabLine/pkgs/container/grabline-app)
(`ghcr.io/gr33nops/grabline-app`) via [`.github/workflows/packages.yml`](../.github/workflows/packages.yml).
That fills the Packages block on the repository page. Installers for end users
still come from the [Releases](https://github.com/Gr33nOps/GrabLine/releases) page.

## Build one locally

```bash
pip install -e . pyinstaller pillow
python packaging/make_icons.py                 # QT_QPA_PLATFORM=offscreen if headless
pyinstaller packaging/grabline.spec --noconfirm
# Linux only, to wrap it as an AppImage:
bash packaging/linux/build_appimage.sh 1.3.5
```

## Not signed (yet)

Installers are unsigned, so Windows SmartScreen and macOS Gatekeeper warn on
first launch. Signing needs paid certificates (Windows ~US$100-400/yr, Apple
Developer US$99/yr); add the signing steps to the workflow once those exist.
