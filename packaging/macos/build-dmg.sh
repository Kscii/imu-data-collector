#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
APP="$ROOT/dist/IMU Data Collector.app"
ARCH="${1:?缺少架构名称，例如 arm64 或 x86_64}"
VERSION="${APP_VERSION:?缺少 APP_VERSION}"
OUTPUT="$ROOT/dist-installer/IMU-Data-Collector-${VERSION}-macOS-${ARCH}.dmg"
STAGE="$ROOT/build/dmg-${ARCH}"

test -d "$APP"
rm -rf "$STAGE"
mkdir -p "$STAGE" "$ROOT/dist-installer"
cp -R "$APP" "$STAGE/IMU Data Collector.app"
ln -s /Applications "$STAGE/Applications"
hdiutil create \
  -volname "IMU Data Collector" \
  -srcfolder "$STAGE" \
  -ov \
  -format UDZO \
  "$OUTPUT"
