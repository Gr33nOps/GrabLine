# Install and update GrabLine

Download packages from the [latest release](https://github.com/GrabLine/GrabLine/releases/latest).
The [download website](https://grabline.github.io/#download) selects packages for
each supported desktop platform. Older website links redirect to this address.
Packaged builds include Python and application libraries. FFmpeg and Deno are
provisioned separately when needed; RAR/7Z extraction needs an installed 7-Zip.
For source installations, see [Contributing](../CONTRIBUTING.md).

- [Verify your download](#verify-your-download)
- [Windows](#windows)
- [macOS](#macos)
- [Linux](#linux)
- [Browser extension](#browser-extension)
- [Updates](#updates)
- [Data and backups](#data-and-backups)
- [Uninstall](#uninstall)

## Verify your download

Each release includes `SHA256SUMS.txt`. Compare your package's SHA-256 with its
matching filename in that file. The checksum list includes build-directory
prefixes; use the package basename to locate the correct entry.

```powershell
# Windows: replace the placeholder with your downloaded package's actual name.
Get-FileHash '.\Grabline-Setup-<version>.exe' -Algorithm SHA256
```

```sh
# Linux
sha256sum 'Grabline-<version>-x86_64.AppImage'
# macOS
shasum -a 256 'Grabline-<version>-applesilicon.dmg'
```

The installers are currently **unsigned**. A matching hash checks integrity
against the published file, not independent publisher signing. Windows/macOS
may show reputation or signing prompts; review the source and your OS's normal
trust controls. Do not disable protection to get a blocked package to run.

## Windows

Windows packages target **x64**.

| File | Use |
| --- | --- |
| `Grabline-Setup-<version>.exe` | Installed app, Start menu and installer-selected integration tasks |
| `Grabline-<version>-windows-portable.zip` | Extract and run without an installer |

Run the installer, choose the destination and review the offered tasks: desktop
shortcut, startup, magnet association and `.torrent` association. Select the
tasks you want rather than assuming their defaults. The installer offers to
close a running copy during an upgrade; keep a backup of important state first.

For the portable package, extract the entire ZIP and run `grabline\grabline.exe`.
Keep the executable with its bundled files. It skips installer-created shortcuts
and file associations. It still uses the normal per-user data folder and may
register its native browser host on first launch; it is not a fully self-contained
profile on a USB drive. Keep its path stable if you pair a browser to it.

## macOS

The published DMG targets **Apple Silicon** Macs. Open
`Grabline-<version>-applesilicon.dmg`, drag the app into Applications, eject the
image and launch it from Applications. Unsigned-app prompts depend on macOS
version and policy. Use the OS's normal app-review controls; managed machines
may require administrator approval. Intel Macs need a compatible source build;
the Apple Silicon artifact is not an Intel package.

## Linux

Published Linux packages target **x86-64** and are built on Ubuntu 22.04.

| Package | Use |
| --- | --- |
| `grabline_<version>_amd64.deb` | Debian/Ubuntu-compatible system, menu entry and package-manager removal |
| `Grabline-<version>-x86_64.AppImage` | Single-file executable on compatible systems with FUSE support |
| `Grabline-<version>-linux-x86_64.tar.gz` | Extracted bundle, including systems where AppImage/FUSE is unavailable |

Replace `<version>` with the actual downloaded version:

```sh
sudo apt install ./grabline_<version>_amd64.deb
```

```sh
chmod +x Grabline-<version>-x86_64.AppImage
./Grabline-<version>-x86_64.AppImage
```

```sh
tar xzf Grabline-<version>-linux-x86_64.tar.gz
./grabline/grabline
```

If the AppImage reports a missing FUSE library, use your distribution's compatible
FUSE package or the tarball. A packaged build still depends on compatible OS
system libraries; see [Troubleshooting](troubleshooting.md) for source/Qt issues.

## Browser extension

Launch the desktop app first, then open **Browser Setup** or Settings → Browser
Integration → **Pair browsers**.

| Browser | Install |
| --- | --- |
| Firefox | [GrabLine Connect on Mozilla Add-ons](https://addons.mozilla.org/en-US/firefox/addon/grabline-connect/) for a signed install |
| Chrome / Edge / Chromium family | Follow Browser Setup, open that browser's extensions manager, enable Developer mode and load the extension folder it provides |

For source development, load the repo's `extension/` folder. Published extension
ZIPs are store-targeted packages, not signed Firefox add-ons or a guarantee of
the pinned unpacked Chromium identity. See the
[extension guide](../extension/README.md) for exact development pairing.

Open the popup and verify the connection. If the app was moved or you switched
between an installed build and source checkout, pair the intended copy again.
Do not remove another application's browser registration while troubleshooting.

## Updates

Use Settings → About to check for updates; startup checks are configurable in
General. Update discovery uses the latest GitHub release. The updater checks a
download against the asset's published SHA-256 digest when GitHub provides it.
Keep normal OS prompts in place during installer handoff.

You can also download the latest package and update manually. Close a portable
copy and extract the new bundle into a separate folder; keep old files until the
new copy starts successfully. Pair the browser again if its executable path
changed. Back up state before a major change or moving between operating systems.

## Data and backups

The data folder contains `grabline.db` (settings, queue, history and statistics),
logs, downloaded tools and other app state:

| System | Default data folder |
| --- | --- |
| Windows | `%LOCALAPPDATA%\Grabline` |
| macOS | `~/Library/Application Support/Grabline` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/Grabline` |

Downloads default to `~/Downloads/Grabline`, optionally sorted by category.
Change the destination in Settings → Downloads. Advanced shows the actual
data folder. Cloud account secrets live separately in the OS keyring; API keys
and browser request headers may be in the local database.

For preferences, use Advanced → Export settings. For a full local backup, quit
the app and close its browser/native-host connections before copying the entire
data folder, including any SQLite sidecars still present. Preserve download and
partial files separately. Restore to a compatible app version, update absolute
paths, and configure OS keyring secrets again on the new computer. Simply copying
the database between Windows and Linux does not rewrite paths or migrate secrets.
Treat full backups as sensitive; [Privacy](../PRIVACY.md) explains their contents.

## Uninstall

| System | Remove the program |
| --- | --- |
| Windows installer | Settings → Apps → GrabLine → Uninstall |
| Windows portable | Quit GrabLine and remove its extracted program folder |
| macOS | Quit and remove the app from Applications |
| Linux DEB | `sudo apt remove grabline` |
| Linux AppImage / tarball | Quit and remove the executable/bundle |

The Windows installer includes native-host registration cleanup. Removing a
portable bundle or dragging an app to the trash does not guarantee removal of
browser registration, startup items or keyring entries. Unpair/remove those
integrations deliberately if retiring the app, and remove GrabLine Connect in
the browser's extension manager.

Program removal and user-data removal are separate. Keep your downloads and
data folder unless you intentionally want to delete them. Remove saved cloud
accounts through the app before uninstalling if their keyring entries should
also be removed. Never delete an entire user profile to reset GrabLine.
