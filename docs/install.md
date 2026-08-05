# Installing GrabLine

Packaged builds include everything: no Python, no extra dependencies.
Pick your system below, or grab files from the
[latest release](https://github.com/Gr33nOps/GrabLine/releases/latest).

The builds are **not code-signed**. Signing certificates cost money per year,
and GrabLine is free, so Windows and macOS warn the first time you run it.
Exact clicks are under each section.

- [Windows](#windows)
- [macOS](#macos)
- [Linux](#linux)
- [The browser extension](#the-browser-extension)
- [Where your data lives](#where-your-data-lives)
- [Uninstalling](#uninstalling)

---

## Windows

| File | Use it when |
|---|---|
| `Grabline-Setup-<version>.exe` | Normal install. Start menu entry, magnet and `.torrent` handling, optional start-with-Windows. |
| `Grabline-<version>-windows-portable.zip` | No administrator rights, or you want it on a USB stick. |

### Installer

1. Run `Grabline-Setup-<version>.exe`.
2. SmartScreen says **"Windows protected your PC"**. Click **More info**, then
   **Run anyway**. This is the unsigned-build warning, and it appears once per
   version.
3. The wizard offers four optional tasks, all off unless you tick them:
   desktop shortcut, start with Windows (minimized to the tray), and handling
   magnet links and `.torrent` files.
4. Finish. GrabLine is in the Start menu and searchable from the taskbar.

Installing over an existing copy is fine: the installer closes a running
GrabLine first and keeps your settings and download list.

### Portable

Unzip anywhere and run `grabline.exe`. Nothing is written outside the folder
except your data (see [below](#where-your-data-lives)). A portable copy does
not create shortcuts, does not register magnet links, and cannot start with
Windows; use the installer if you want those.

---

## macOS

`Grabline-<version>-applesilicon.dmg`, for Apple Silicon Macs (M1 and later).

1. Open the `.dmg` and drag **GrabLine** onto the **Applications** shortcut.
2. Eject the disk image and open GrabLine from Applications.
3. macOS says **"GrabLine cannot be opened because the developer cannot be
   verified"**. Click **Cancel**: not "Move to Bin".
4. **Right-click** (or Control-click) GrabLine in Applications → **Open** →
   **Open** in the dialog that follows.

   On macOS 15 and later you may instead need System Settings → **Privacy &
   Security**, scroll to the bottom, and click **Open Anyway** next to the
   message about GrabLine.
5. That is once per version. Afterwards it launches normally, and Spotlight
   finds it.

Running the wrong architecture's build is the usual cause of "the app quit
unexpectedly" on first launch: check the chip and download the matching file.

---

## Linux

| File | Use it when |
|---|---|
| `grabline_<version>_amd64.deb` | Debian, Ubuntu, Mint, Pop!\_OS: anything with `apt`. |
| `Grabline-<version>-x86_64.AppImage` | Any distribution, no install, one file. |
| `Grabline-<version>-linux-x86_64.tar.gz` | Your system has no FUSE, so the AppImage will not start. |

### .deb (recommended on Debian/Ubuntu)

```sh
sudo apt install ./grabline_<version>_amd64.deb
```

GrabLine lands in your applications menu, `grabline` works from a terminal,
and magnet links and `.torrent` files open in it. Update by installing a newer
`.deb` the same way.

### AppImage

```sh
chmod +x Grabline-<version>-x86_64.AppImage
./Grabline-<version>-x86_64.AppImage
```

It offers to add itself to your applications menu on first run. If it exits
with `dlopen(): error loading libfuse.so.2`, your system ships FUSE 3 only.
Either install FUSE 2 (`sudo apt install libfuse2`) or use the tarball.

### Tarball

```sh
tar xzf Grabline-<version>-linux-x86_64.tar.gz
./grabline/grabline
```

Nothing is installed; move the folder wherever you like.

---

## The browser extension

GrabLine downloads fine on its own, but the extension is what puts a GrabLine
button on videos and hands your browser's downloads over.

| Browser | How |
|---|---|
| **Firefox** | Install [GrabLine Connect](https://addons.mozilla.org/en-US/firefox/addon/grabline-connect/): one click, reviewed and signed by Mozilla. |
| **Chrome, Edge, Brave, other Chromium** | In GrabLine: sidebar **⋯** → **Browser Setup** → **Add GrabLine to \<browser\>**. It opens the extension folder and `chrome://extensions`; turn on **Developer mode** and click **Load unpacked**. |

GrabLine registers the connector between app and browser on first launch. If
the extension says it is not paired, open **Settings → Browser Integration →
Pair browsers**, then restart the browser.

---

## Where your data lives

Settings, the download list and statistics are one SQLite file, `grabline.db`:

| System | Path |
|---|---|
| Windows | `%LOCALAPPDATA%\Grabline` |
| macOS | `~/Library/Application Support/Grabline` |
| Linux | `~/.local/share/Grabline` |

Downloads themselves default to `~/Downloads/Grabline`, sorted into Video,
Music, Images, Documents, Archives, Programs, Games and Torrents. Change that
in **Settings → Downloads**.

Copying that folder to another machine moves your queue and settings with it.
Nothing is uploaded anywhere: there is no account and no telemetry.

---

## Uninstalling

| System | How |
|---|---|
| **Windows** | Settings → Apps → **GrabLine** → Uninstall. Portable: delete the folder. |
| **macOS** | Drag **GrabLine** from Applications to the Bin. |
| **Linux (.deb)** | `sudo apt remove grabline` |
| **Linux (AppImage/tarball)** | Delete the file or folder. |

Uninstalling removes the program and the browser pairing. It leaves your
downloads alone, and leaves the data folder above: delete it by hand if you
want the settings and history gone too.
