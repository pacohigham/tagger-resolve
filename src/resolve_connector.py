# Copyright 2026 Tagger, LLC -- support@tagger.mov
"""Cross-platform DaVinci Resolve scripting API connection.

Locates the DaVinciResolveScript module on macOS, Windows, and Linux,
imports it, and returns a live Resolve proxy when the application is
running. Returns None silently when Resolve is closed -- callers poll
this in the flush worker.
"""

from __future__ import annotations

import logging
import os
import platform
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


_DEFAULT_PATHS = {
    "Darwin": {
        "api": "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting",
        "lib": "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so",
    },
    "Windows": {
        "api": r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting",
        "lib": r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll",
    },
    "Linux": {
        "api": "/opt/resolve/Developer/Scripting",
        "lib": "/opt/resolve/libs/Fusion/fusionscript.so",
    },
}


def _platform_paths() -> dict:
    system = platform.system()
    paths = _DEFAULT_PATHS.get(system)
    if not paths:
        raise RuntimeError(f"Unsupported platform: {system}")
    return paths


def _ensure_env() -> None:
    """Set RESOLVE_SCRIPT_API/LIB and PYTHONPATH if not already set.

    Honours user-set env vars; only fills in defaults when missing.
    """
    paths = _platform_paths()
    api = os.environ.get("RESOLVE_SCRIPT_API") or paths["api"]
    lib = os.environ.get("RESOLVE_SCRIPT_LIB") or paths["lib"]
    os.environ["RESOLVE_SCRIPT_API"] = api
    os.environ["RESOLVE_SCRIPT_LIB"] = lib

    modules_dir = str(Path(api) / "Modules")
    if modules_dir not in sys.path:
        sys.path.insert(0, modules_dir)


def import_resolve_module():
    """Import and return the DaVinciResolveScript module.

    Raises ImportError with a clear message if Resolve is not installed.
    """
    _ensure_env()
    try:
        import DaVinciResolveScript as dvr
    except ImportError as e:
        raise ImportError(
            "DaVinciResolveScript module not found. Verify DaVinci Resolve "
            "Studio is installed and Preferences > General > External "
            "scripting using is set to Local. "
            f"RESOLVE_SCRIPT_API={os.environ.get('RESOLVE_SCRIPT_API')}, "
            f"RESOLVE_SCRIPT_LIB={os.environ.get('RESOLVE_SCRIPT_LIB')}"
        ) from e
    return dvr


def get_resolve():
    """Return a live Resolve proxy, or None if Resolve is not running.

    Does not raise on a closed Resolve -- returns None so callers can poll.
    """
    try:
        dvr = import_resolve_module()
    except ImportError as e:
        logger.warning(str(e))
        return None
    try:
        resolve = dvr.scriptapp("Resolve")
    except Exception as e:
        logger.debug(f"scriptapp() failed: {e}")
        return None
    return resolve


def get_current_project(resolve=None):
    """Return the currently open Resolve project, or None.

    Convenience wrapper that handles the resolve -> project_manager ->
    current_project chain and tolerates any link being None.
    """
    if resolve is None:
        resolve = get_resolve()
    if resolve is None:
        return None
    pm = resolve.GetProjectManager()
    if pm is None:
        return None
    return pm.GetCurrentProject()


def is_resolve_available() -> bool:
    """Return True if Resolve is running with a project open."""
    return get_current_project() is not None


def describe() -> dict:
    """Return a diagnostic dict for the current Resolve state."""
    resolve = get_resolve()
    info = {
        "platform": platform.system(),
        "api_path": os.environ.get("RESOLVE_SCRIPT_API"),
        "lib_path": os.environ.get("RESOLVE_SCRIPT_LIB"),
        "running": False,
        "project": None,
        "version": None,
    }
    if resolve is None:
        return info
    info["running"] = True
    try:
        info["version"] = resolve.GetVersionString()
    except Exception:
        pass
    project = get_current_project(resolve)
    if project is not None:
        info["project"] = project.GetName()
    return info
