# PyInstaller spec for Tagger for Resolve.
#
# Build:
#   .venv/bin/pyinstaller --noconfirm "Tagger for Resolve.spec"
#
# Output: dist/Tagger for Resolve.app on macOS, dist/Tagger for Resolve/
# on Windows + Linux.
#
# Runtime dependencies that are NOT bundled (loaded dynamically from the
# customer's machine):
#   - ffmpeg / ffprobe        -- system PATH or standard install location
#   - DaVinciResolveScript    -- ships with DaVinci Resolve Studio
#   - BlackmagicRawAPI        -- ships with Blackmagic RAW SDK
# These are intentionally external because each updates on its own cycle
# and bundling them would create version-lock + license issues.

# -*- mode: python ; coding: utf-8 -*-

import platform
import sys
from pathlib import Path

block_cipher = None
APP_NAME = "Tagger for Resolve"
SRC_DIR  = "src"

# pystray on macOS uses PyObjC which has many submodules PyInstaller doesn't
# auto-detect. watchdog also has platform-specific observer backends.
hidden_imports = [
    # pystray + macOS backend
    "pystray",
    "pystray._darwin",
    "pystray._base",
    "pystray._util",
    "pystray._util.darwin",
    # PyObjC pieces pystray needs on macOS
    "Foundation",
    "AppKit",
    "objc",
    # watchdog cross-platform observers
    "watchdog",
    "watchdog.observers",
    "watchdog.events",
    # Pillow
    "PIL",
    "PIL.Image",
    "PIL.ImageDraw",
    "PIL.ImageFont",
]

if platform.system() == "Darwin":
    hidden_imports.append("watchdog.observers.fsevents")
elif platform.system() == "Linux":
    hidden_imports.append("watchdog.observers.inotify")
elif platform.system() == "Windows":
    hidden_imports.append("watchdog.observers.read_directory_changes")

_bin_dir = Path("bin")
_ffmpeg_datas = []
if _bin_dir.is_dir():
    for tool in ("ffmpeg", "ffprobe", "ffmpeg.exe", "ffprobe.exe"):
        p = _bin_dir / tool
        if p.exists():
            _ffmpeg_datas.append((str(p), "bin"))

a = Analysis(
    [str(Path(SRC_DIR) / "main.py")],
    pathex=[SRC_DIR],
    binaries=[],
    datas=[
        ("VERSION", "."),
        (str(Path(SRC_DIR) / "assets" / "tagger_menubar.png"), "assets"),
        (str(Path(SRC_DIR) / "assets" / "tagger_512.png"), "assets"),
        (str(Path(SRC_DIR) / "assets" / "menubar_frames"), "assets/menubar_frames"),
    ] + _ffmpeg_datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Drop test framework, we don't need it at runtime
        "pytest",
        "_pytest",
        # Drop unused stdlib bits
        "tkinter",
        "test",
        "unittest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                # tray app -- no terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,       # set up later for distribution
    entitlements_file="entitlements.plist",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)

# macOS .app bundle
if platform.system() == "Darwin":
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon="TaggerResolve.icns",
        bundle_identifier="mov.tagger.taggerresolve",
        info_plist={
            "CFBundleShortVersionString":  "0.2.0",
            "CFBundleVersion":             "0.2.0",
            "CFBundleName":                APP_NAME,
            "CFBundleDisplayName":         APP_NAME,
            "LSUIElement":                 False,
            "NSHighResolutionCapable":     True,
            # macOS 11+ for Resolve Studio scripting reliability
            "LSMinimumSystemVersion":      "11.0",
            "NSHumanReadableCopyright":    "Copyright (c) 2026 Tagger, LLC",
        },
    )
