#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$PROJECT_DIR/dist/macos"
APP_DIR="$BUILD_DIR/Lumen.app"
CONTENTS_DIR="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
ICONSET_DIR="$BUILD_DIR/Lumen.iconset"
ICON_SOURCE="$PROJECT_DIR/store-assets/lumen-app-icon.png"

rm -rf "$APP_DIR" "$ICONSET_DIR"
mkdir -p "$MACOS_DIR" "$RESOURCES_DIR" "$ICONSET_DIR"

cp "$PROJECT_DIR/macos/Info.plist" "$CONTENTS_DIR/Info.plist"

for size in 16 32 128 256 512; do
  double_size=$((size * 2))
  sips -z "$size" "$size" "$ICON_SOURCE" --out "$ICONSET_DIR/icon_${size}x${size}.png" >/dev/null
  sips -z "$double_size" "$double_size" "$ICON_SOURCE" --out "$ICONSET_DIR/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET_DIR" -o "$RESOURCES_DIR/Lumen.icns"

SDK_PATH="$(xcrun --sdk macosx --show-sdk-path)"
swiftc \
  "$PROJECT_DIR/macos/LumenDesktop.swift" \
  -sdk "$SDK_PATH" \
  -framework AppKit \
  -framework WebKit \
  -framework Carbon \
  -O \
  -target arm64-apple-macos12 \
  -o "$MACOS_DIR/Lumen"

codesign --force --deep --sign - "$APP_DIR"

rm -f "$BUILD_DIR/Lumen-macOS.zip"
ditto -c -k --sequesterRsrc --keepParent "$APP_DIR" "$BUILD_DIR/Lumen-macOS.zip"
mkdir -p "$PROJECT_DIR/static/downloads"
cp "$BUILD_DIR/Lumen-macOS.zip" "$PROJECT_DIR/static/downloads/Lumen-macOS.zip"

echo "Built $APP_DIR"
echo "Download package: $BUILD_DIR/Lumen-macOS.zip"
