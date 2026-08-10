#!/bin/bash
set -euo pipefail

VERSION=$(tr -d '[:space:]' < VERSION)
ARCH=$(uname -m)
DIST_DIR=${OUROBOROS_DIST_DIR:-dist}
PAYLOAD_DIR="$DIST_DIR/Ouroboros"
APPDIR=${OUROBOROS_APPDIR:-$DIST_DIR/Ouroboros.AppDir}
OUTPUT="$DIST_DIR/Ouroboros-${VERSION}-linux-${ARCH}.AppImage"
TOOL_VERSION=1.9.1

case "$ARCH" in
    x86_64)
        TOOL_ARCH=x86_64
        TOOL_SHA256=ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0
        ;;
    aarch64)
        TOOL_ARCH=aarch64
        TOOL_SHA256=f0837e7448a0c1e4e650a93bb3e85802546e60654ef287576f46c71c126a9158
        ;;
    *)
        echo "Unsupported AppImage architecture: $ARCH" >&2
        exit 1
        ;;
esac

if [ ! -x "$PAYLOAD_DIR/Ouroboros" ]; then
    echo "ERROR: PyInstaller payload not found at $PAYLOAD_DIR/Ouroboros" >&2
    echo "Run build_linux.sh first, or point OUROBOROS_DIST_DIR at its dist directory." >&2
    exit 1
fi

rm -rf "$APPDIR"
mkdir -p \
    "$APPDIR/usr/lib" \
    "$APPDIR/usr/share/applications" \
    "$APPDIR/usr/share/icons/hicolor/1024x1024/apps"
cp -a "$PAYLOAD_DIR" "$APPDIR/usr/lib/ouroboros"
install -m 0755 packaging/appimage/AppRun "$APPDIR/AppRun"
install -m 0644 packaging/appimage/ouroboros.desktop "$APPDIR/ouroboros.desktop"
install -m 0644 packaging/appimage/ouroboros.desktop \
    "$APPDIR/usr/share/applications/ouroboros.desktop"
install -m 0644 assets/icon_1024.png "$APPDIR/ouroboros.png"
install -m 0644 assets/icon_1024.png \
    "$APPDIR/usr/share/icons/hicolor/1024x1024/apps/ouroboros.png"

if [ "${1:-}" = "--appdir-only" ]; then
    echo "Prepared AppDir: $APPDIR"
    exit 0
fi

TOOL_CACHE=${APPIMAGE_TOOL_CACHE_DIR:-${XDG_CACHE_HOME:-${HOME:-/tmp}/.cache}/ouroboros/appimage}
TOOL="$TOOL_CACHE/appimagetool-${TOOL_VERSION}-${TOOL_ARCH}.AppImage"
TOOL_URL="https://github.com/AppImage/appimagetool/releases/download/${TOOL_VERSION}/appimagetool-${TOOL_ARCH}.AppImage"
mkdir -p "$TOOL_CACHE"
if [ ! -f "$TOOL" ]; then
    curl --fail --location --silent --show-error "$TOOL_URL" --output "$TOOL.tmp"
    mv "$TOOL.tmp" "$TOOL"
fi

ACTUAL_SHA256=$(sha256sum "$TOOL" | awk '{print $1}')
if [ "$ACTUAL_SHA256" != "$TOOL_SHA256" ]; then
    echo "appimagetool SHA256 mismatch: expected $TOOL_SHA256, got $ACTUAL_SHA256" >&2
    exit 1
fi
chmod +x "$TOOL"

rm -f "$OUTPUT"
ARCH="$TOOL_ARCH" APPIMAGE_EXTRACT_AND_RUN=1 "$TOOL" "$APPDIR" "$OUTPUT"
chmod +x "$OUTPUT"
rm -rf "$APPDIR"

echo "AppImage: $OUTPUT"
