# Copyright 2026 Tagger, LLC -- support@tagger.mov
"""Tagger for Resolve configuration.

Lives at:
  macOS:   ~/Library/Application Support/TaggerResolve/config.json
  Windows: %APPDATA%/TaggerResolve/config.json
  Linux:   ~/.config/TaggerResolve/config.json

Schema is intentionally flat. Created on first run with defaults.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

try:
    import keyring
    _HAS_KEYRING = True
except ImportError:
    _HAS_KEYRING = False

_KEYRING_SERVICE = "TaggerForResolve"
_KEYRING_ACCOUNT = "license_key"

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)

logger = logging.getLogger(__name__)


def app_dir() -> Path:
    system = platform.system()
    if system == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    elif system == "Windows":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    d = base / "TaggerResolve"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return app_dir() / "config.json"


def db_path() -> Path:
    return app_dir() / "queue.db"


def log_path() -> Path:
    logs = app_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs / "tagger-resolve.log"


DEMO_LIMIT = 20


@dataclass
class Config:
    proxy_url: str = "https://tagger-1t2g.onrender.com"
    license_key: str = ""
    hardware_id: str = ""
    watch_folder: str = ""
    description_length: str = "standard"   # brief | standard | detailed
    batch_size: int = 8
    batch_window_seconds: float = 60.0
    demo_files_used: int = 0

    @property
    def is_licensed(self) -> bool:
        return bool(self.license_key)

    @property
    def is_subscription(self) -> bool:
        return bool(_UUID_RE.match(self.license_key.strip())) if self.license_key else False

    @property
    def effective_hardware_id(self) -> str:
        return "" if self.is_subscription else self.hardware_id

    @property
    def demo_remaining(self) -> int:
        return max(0, DEMO_LIMIT - self.demo_files_used)

    def use_demo_file(self) -> bool:
        """Consume one demo credit. Returns False if none left."""
        if self.demo_files_used >= DEMO_LIMIT:
            return False
        self.demo_files_used += 1
        self.save()
        return True

    @classmethod
    def load(cls) -> "Config":
        path = config_path()
        if not path.exists():
            cfg = cls()
            cfg._ensure_hardware_id()
            cfg.save()
            return cfg
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"Could not read config, using defaults: {e}")
            return cls()
        known = {f.name for f in cls.__dataclass_fields__.values()}
        cfg = cls(**{k: v for k, v in data.items() if k in known})
        stored_key = _get_keyring_license()
        if stored_key:
            cfg.license_key = stored_key
        elif cfg.license_key:
            _set_keyring_license(cfg.license_key)
        if cfg._ensure_hardware_id():
            cfg.save()
        return cfg

    def _ensure_hardware_id(self) -> bool:
        """Generate a stable hardware ID if not set. Returns True if changed."""
        if self.hardware_id:
            return False
        raw = platform.node() + "-" + str(uuid.getnode())
        self.hardware_id = "TFR-" + hashlib.sha256(raw.encode()).hexdigest()[:16].upper()
        return True

    def save(self) -> None:
        if self.license_key:
            _set_keyring_license(self.license_key)
        path = config_path()
        data = asdict(self)
        data.pop("license_key", None)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)


def _get_keyring_license() -> str:
    if not _HAS_KEYRING:
        return ""
    try:
        val = keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
        return val or ""
    except Exception as e:
        logger.warning(f"Keychain read failed, falling back to config: {e}")
        return ""


def _set_keyring_license(key: str) -> None:
    if not _HAS_KEYRING:
        return
    try:
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT, key)
    except Exception as e:
        logger.warning(f"Keychain write failed: {e}")
