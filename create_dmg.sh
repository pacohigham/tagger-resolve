#!/bin/bash
# Build a macOS DMG installer for Tagger for Resolve.
#
# Usage:
#   ./create_dmg.sh
#
# Prerequisites:
#   - dist/Tagger for Resolve.app must exist (run PyInstaller first)
#
# Output:
#   dist/Tagger for Resolve <version>.dmg

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="Tagger for Resolve"
APP_PATH="$SCRIPT_DIR/dist/$APP_NAME.app"
VERSION="$(cat "$SCRIPT_DIR/VERSION")"
DMG_NAME="$APP_NAME $VERSION"
DMG_PATH="$SCRIPT_DIR/dist/$DMG_NAME.dmg"
DMG_TEMP="$SCRIPT_DIR/dist/$DMG_NAME-temp.dmg"
STAGING="$SCRIPT_DIR/dist/dmg-staging"
VOL_NAME="Install $APP_NAME"
BG_IMAGE="$SCRIPT_DIR/src/assets/dmg_background.png"

# ---- Preflight ----
if [ ! -d "$APP_PATH" ]; then
    echo "ERROR: $APP_PATH not found."
    echo "Run PyInstaller first:"
    echo "  .venv/bin/pyinstaller --noconfirm \"Tagger for Resolve.spec\""
    exit 1
fi

echo "Building DMG: $DMG_NAME"
echo "  App: $APP_PATH"
echo "  Version: $VERSION"
echo ""

# ---- Clean previous ----
rm -rf "$STAGING"
rm -f "$DMG_PATH" "$DMG_TEMP"

# ---- Stage contents ----
mkdir -p "$STAGING/.background"
cp -R "$APP_PATH" "$STAGING/"
ln -s /Applications "$STAGING/Applications"
cp "$BG_IMAGE" "$STAGING/.background/background.png"

# ---- Create read-write DMG ----
echo "Creating temporary DMG..."
hdiutil create \
    -volname "$VOL_NAME" \
    -srcfolder "$STAGING" \
    -ov \
    -format UDRW \
    "$DMG_TEMP"

# ---- Mount and style the DMG ----
echo "Mounting and styling..."
MOUNT_OUTPUT=$(hdiutil attach -readwrite -noverify "$DMG_TEMP")
DEVICE=$(echo "$MOUNT_OUTPUT" | grep "^/dev/" | head -1 | awk '{print $1}')
MOUNT_POINT="/Volumes/$VOL_NAME"

# Wait for mount and Finder to index
sleep 3

# Delete any stale .DS_Store so Finder starts fresh
rm -f "$MOUNT_POINT/.DS_Store"

echo "Applying Finder layout..."
osascript << EOF
tell application "Finder"
    tell disk "$VOL_NAME"
        open
        delay 1
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set bounds of container window to {200, 200, 740, 580}
        set theViewOptions to the icon view options of container window
        set arrangement of theViewOptions to not arranged
        set icon size of theViewOptions to 96
        set background picture of theViewOptions to file ".background:background.png"
        delay 1
        set position of item "$APP_NAME.app" of container window to {130, 160}
        set position of item "Applications" of container window to {410, 160}
        close
        open
        update without registering applications
        delay 2
        close
    end tell
end tell
EOF

# Allow Finder to flush .DS_Store
sync
sleep 2

# ---- Unmount ----
echo "Unmounting..."
hdiutil detach "$DEVICE" -quiet || hdiutil detach "$DEVICE" -force

# ---- Convert to compressed read-only DMG ----
echo "Compressing to final DMG..."
hdiutil convert "$DMG_TEMP" \
    -format UDZO \
    -imagekey zlib-level=9 \
    -o "$DMG_PATH"

# ---- Cleanup ----
rm -f "$DMG_TEMP"
rm -rf "$STAGING"

echo ""
echo "Done: $DMG_PATH"
ls -lh "$DMG_PATH"
