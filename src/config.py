# Copyright 2026 Tagger, LLC -- support@tagger.mov
"""Tagger for Resolve configuration.

Lives at:
  macOS:   ~/Library/Application Support/TaggerResolve/config.json
  Windows: %APPDATA%/TaggerResolve/config.json
  Linux:   ~/.config/TaggerResolve/config.json

Schema is intentionally flat. Created on first run with defaults.
"""

from __future__ import annotations

import json
import logging
import os
import platform
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

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


@dataclass
class Config:
    proxy_url: str = "https://tagger-1t2g.onrender.com"
    license_key: str = ""
    hardware_id: str = ""
    watch_folder: str = ""
    description_length: str = "standard"   # brief | standard | detailed
    auto_push: bool = True

    @classmethod
    def load(cls) -> "Config":
        path = config_path()
        if not path.exists():
            cfg = cls()
            cfg.save()
            return cfg
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"Could not read config, using defaults: {e}")
            return cls()
        # Tolerate extra keys; only consume known fields
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        path = config_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)
