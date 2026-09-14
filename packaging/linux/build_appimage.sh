#!/usr/bin/env bash
# Assemble an AppImage from the PyInstaller onedir bundle in dist/grabline/.
# Usage: packaging/linux/build_appimage.sh <version>
# Requires: packaging/make_icons.py has produced packaging/grabline.png, and the
# X11/xcb build libs are installed (the release workflow installs them).
set -euo pipefail

VERSION="${1:?usage: build_appimage.sh <version>}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DIST="$ROOT/dist"
APPDIR="$ROOT/build/AppDir"

[ -d "$DIST/grabline" ] || { echo "missing $DIST/grabline - run pyinstaller first" >&2; exit 1; }

# Bundle the X11/xcb/xkb system libraries Qt's platform plugin dlopens at
# runtime, straight INTO the app bundle (dist/grabline/_internal). PyInstaller
# ships Qt's own libs but NOT these, so on a fresh Ubuntu/Mint the app dies with
# "could not load the Qt platform plugin xcb". Putting them in _internal (which
# is on the app's rpath) fixes both this AppImage and a plain tarball of the
# same bundle - nothing for the user to install.
echo "bundling Qt/xcb runtime libraries into the app..."
for lib in \
  libxcb-cursor.so.0 libxcb-xinerama.so.0 libxcb-icccm.so.4 libxcb-image.so.0 \
  libxcb-keysyms.so.1 libxcb-randr.so.0 libxcb-render-util.so.0 libxcb-shape.so.0 \
  libxcb-util.so.1 libxcb-xkb.so.1 libxkbcommon-x11.so.0 libxkbcommon.so.0; do
  src="$(ldconfig -p | awk -v n="$lib" 'index($1, n)==1 {print $NF; exit}')"
  if [ -n "$src" ] && [ -f "$src" ]; then
    cp -Lv "$src" "$DIST/grabline/_internal/"
  else
    echo "  (skip $lib - not on the build system)"
  fi
done

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"
cp -r "$DIST/grabline/." "$APPDIR/usr/bin/"
cp "$ROOT/packaging/linux/grabline.desktop" "$APPDIR/grabline.desktop"
cp "$ROOT/packaging/grabline.png" "$APPDIR/grabline.png"

cat > "$APPDIR/AppRun" <<'APPRUN'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
export LD_LIBRARY_PATH="$HERE/usr/bin/_internal:${LD_LIBRARY_PATH:-}"
exec "$HERE/usr/bin/grabline" "$@"
APPRUN
chmod +x "$APPDIR/AppRun"

TOOL="$ROOT/build/appimagetool"
if [ ! -x "$TOOL" ]; then
  curl -fsSL -o "$TOOL" \
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
  chmod +x "$TOOL"
fi

# --appimage-extract-and-run avoids needing FUSE on the CI runner.
ARCH=x86_64 "$TOOL" --appimage-extract-and-run "$APPDIR" \
  "$DIST/Grabline-$VERSION-x86_64.AppImage"
echo "built $DIST/Grabline-$VERSION-x86_64.AppImage"
