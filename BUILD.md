# Building Tagger for Resolve

## What gets built

| Platform | Output | Format |
|---|---|---|
| macOS   | `dist/Tagger for Resolve <ver>.dmg` | DMG installer with drag-to-Applications |
| Windows | `dist/Tagger for Resolve/Tagger for Resolve.exe` | folder with .exe |
| Linux   | `dist/Tagger for Resolve/Tagger for Resolve` | folder with executable |

The same `Tagger for Resolve.spec` is used on all three platforms. Run the build on each target OS.

## Runtime dependencies (NOT bundled)

These are loaded at runtime from the customer's machine and intentionally not packaged with the app:

- **ffmpeg** + **ffprobe** -- system PATH or standard install location
- **DaVinci Resolve Studio** -- provides `DaVinciResolveScript.py` (auto-discovered)
- **Blackmagic RAW SDK** (optional, for `.braw` files) -- provides `BlackmagicRawAPI.framework` / `.dll` / `.so`

Customers install these themselves before running Tagger. The `--validate` command checks for each.

## macOS build

```bash
cd "/Users/sn4ck5/Desktop/Tagger for Resolve v0.1.0"
.venv/bin/pip install pyinstaller    # one-time
.venv/bin/pyinstaller --noconfirm "Tagger for Resolve.spec"
```

Output: `dist/Tagger for Resolve.app` (~35 MB).

### Create the DMG installer

```bash
./create_dmg.sh
```

Output: `dist/Tagger for Resolve 0.1.0.dmg` (~14 MB). Opens with a drag-to-Applications layout. The version number in the DMG filename comes from the `VERSION` file.

### Test before distributing

```bash
"dist/Tagger for Resolve.app/Contents/MacOS/Tagger for Resolve" --validate
open "dist/Tagger for Resolve.app"   # should appear as a tray icon in the menu bar
```

### Code signing (for distribution outside testing)

Without a Developer ID Application certificate, customers downloading the unsigned `.app` will see a Gatekeeper warning ("can't be opened because it is from an unidentified developer"). They can override by right-clicking → Open the first time, but it's not customer-grade.

For shippable builds:

1. Get a Developer ID Application certificate via Apple Developer Program ($99/yr)
2. Update `Tagger for Resolve.spec`:
   ```python
   codesign_identity="Developer ID Application: Your Name (TEAMID)",
   ```
3. After build, sign the bundle:
   ```bash
   codesign --deep --force --options runtime \
            --sign "Developer ID Application: Your Name (TEAMID)" \
            "dist/Tagger for Resolve.app"
   ```
4. Notarize the .app, then build the DMG:
   ```bash
   xcrun notarytool submit "dist/Tagger for Resolve.app.zip" \
            --apple-id YOUR@APPLE.ID \
            --team-id TEAMID \
            --password APP_SPECIFIC_PASSWORD \
            --wait
   xcrun stapler staple "dist/Tagger for Resolve.app"
   ./create_dmg.sh
   ```

## Windows build

(On a Windows machine with Python 3.9+ and the same venv contents installed):
```cmd
cd "Tagger for Resolve v0.1.0"
.venv\Scripts\pip install pyinstaller
.venv\Scripts\pyinstaller --noconfirm "Tagger for Resolve.spec"
```

Output: `dist\Tagger for Resolve\Tagger for Resolve.exe`. Distribute the whole `dist\Tagger for Resolve\` folder (or zip it).

For signed builds: Authenticode certificate via DigiCert / Sectigo (~$300/yr). Sign with `signtool sign /f cert.pfx /p PASS dist\...\Tagger\ for\ Resolve.exe`.

## Linux build

(On Linux with Python 3.9+):
```bash
cd "Tagger for Resolve v0.1.0"
.venv/bin/pip install pyinstaller
.venv/bin/pyinstaller --noconfirm "Tagger for Resolve.spec"
```

Output: `dist/Tagger for Resolve/Tagger for Resolve`. Wrap in an `.AppImage` for easier distribution.

## Versioning

Bump `VERSION` (the file is bundled into the .app so `--validate` reflects it):
```bash
echo "0.1.1" > VERSION
.venv/bin/pyinstaller --noconfirm "Tagger for Resolve.spec"
```

Also bump the `CFBundleShortVersionString` and `CFBundleVersion` in the spec's `info_plist` block to match. (Yes, three places. PyInstaller doesn't read VERSION automatically.)

## Build artifacts

- `dist/` and `build/` are gitignored. Rebuild from source.
- `Tagger for Resolve.spec` IS committed -- it's our build configuration.
- `TaggerResolve.icns` IS committed -- the app icon.
- `create_dmg.sh` IS committed -- builds the macOS DMG installer.

## App icon

`TaggerResolve.icns` is the app icon (sage green square with white "T"). To regenerate it from scratch, create a `TaggerResolve.iconset/` folder with PNGs at standard sizes (16 through 1024, plus @2x variants) and run `iconutil -c icns TaggerResolve.iconset -o TaggerResolve.icns`. The iconset folder is gitignored.
