#!/usr/bin/env bash
# Rebuild the bundled native launcher on macOS with Command Line Tools.
set -euo pipefail
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
BUILD_DIR="$(mktemp -d)"
trap 'rm -f "$BUILD_DIR/arm64" "$BUILD_DIR/x86_64"; rmdir "$BUILD_DIR"' EXIT
clang -Wall -Wextra -Werror -arch arm64 -mmacosx-version-min=11.0 \
  "$ROOT/scripts/macos_launcher.c" -o "$BUILD_DIR/arm64"
clang -Wall -Wextra -Werror -arch x86_64 -mmacosx-version-min=10.15 \
  "$ROOT/scripts/macos_launcher.c" -o "$BUILD_DIR/x86_64"
lipo -create "$BUILD_DIR/arm64" "$BUILD_DIR/x86_64" \
  -output "$ROOT/sensorius/resources/sensorius-macos-launcher"
chmod +x "$ROOT/sensorius/resources/sensorius-macos-launcher"
