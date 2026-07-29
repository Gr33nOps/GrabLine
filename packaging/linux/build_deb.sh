#!/usr/bin/env bash
# Build a .deb from the PyInstaller bundle in dist/grabline.
#
#   bash packaging/linux/build_deb.sh 1.2.3   ->  dist/grabline_1.2.3_amd64.deb
#
# Layout follows the usual convention for a bundled (non-distro-built) app:
# the whole bundle lives in /opt/grabline, with a launcher symlink on PATH and
# the desktop entry + icons in the system locations so the app grid and search
# find it. User data (downloads, grabline.db) is untouched by install/remove.
set -euo pipefail

VERSION="${1:?usage: build_deb.sh <version>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUNDLE="$ROOT/dist/grabline"
STAGE="$ROOT/build/deb"
OUT="$ROOT/dist/grabline_${VERSION}_amd64.deb"

[ -d "$BUNDLE" ] || { echo "missing $BUNDLE - run PyInstaller first" >&2; exit 1; }

rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" \
         "$STAGE/opt/grabline" \
         "$STAGE/usr/bin" \
         "$STAGE/usr/share/applications" \
         "$STAGE/usr/share/icons/hicolor/256x256/apps"

cp -r "$BUNDLE/." "$STAGE/opt/grabline/"
chmod 0755 "$STAGE/opt/grabline/grabline"
[ -f "$STAGE/opt/grabline/grabline-host" ] && chmod 0755 "$STAGE/opt/grabline/grabline-host"
ln -sf /opt/grabline/grabline "$STAGE/usr/bin/grabline"
cp "$ROOT/packaging/grabline.png" "$STAGE/usr/share/icons/hicolor/256x256/apps/grabline.png"

cat > "$STAGE/usr/share/applications/grabline.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=GrabLine
GenericName=Download Manager
Comment=Download manager with browser integration, video and torrent support
Exec=/usr/bin/grabline %u
Icon=grabline
Terminal=false
Categories=Network;FileTransfer;Qt;
Keywords=download;downloader;torrent;video;manager;
MimeType=application/x-bittorrent;x-scheme-handler/magnet;
StartupNotify=false
StartupWMClass=Grabline
DESKTOP

# Installed size in KiB, as dpkg expects.
INSTALLED_KB="$(du -sk "$STAGE/opt" | cut -f1)"

cat > "$STAGE/DEBIAN/control" <<CONTROL
Package: grabline
Version: ${VERSION}
Section: net
Priority: optional
Architecture: amd64
Maintainer: GrabLine <noreply@grabline.dev>
Installed-Size: ${INSTALLED_KB}
Depends: libc6, libglib2.0-0, libgl1, libegl1, libxkbcommon0, libdbus-1-3
Recommends: ffmpeg
Homepage: https://github.com/Gr33nOps/GrabLine
Description: Download manager with browser integration
 Grabline downloads files, videos, streams and torrents, with a browser
 extension that hands links to the app. Segmented downloading, a queue
 manager, scheduling and conversion are built in.
CONTROL

cat > "$STAGE/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    update-desktop-database -q /usr/share/applications 2>/dev/null || true
    gtk-update-icon-cache -q -f /usr/share/icons/hicolor 2>/dev/null || true
fi
exit 0
POSTINST

cat > "$STAGE/DEBIAN/postrm" <<'POSTRM'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    update-desktop-database -q /usr/share/applications 2>/dev/null || true
    gtk-update-icon-cache -q -f /usr/share/icons/hicolor 2>/dev/null || true
fi
exit 0
POSTRM

# Normalise the modes PyInstaller and the heredocs leave behind: lintian and
# dpkg both object to group-writable files in a package.
find "$STAGE" -type d -exec chmod 0755 {} +
find "$STAGE" -type f -perm -u+x -exec chmod 0755 {} +
find "$STAGE" -type f -not -perm -u+x -exec chmod 0644 {} +
chmod 0755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/postrm"

dpkg-deb --root-owner-group --build "$STAGE" "$OUT" >/dev/null
echo "built $OUT"
