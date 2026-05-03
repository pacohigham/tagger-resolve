# Copyright 2026 Tagger, LLC -- support@tagger.mov
"""Native macOS dialog helpers (osascript-based).

Cross-platform: returns None / False on non-Darwin systems.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import tempfile

logger = logging.getLogger(__name__)


def confirm(title: str, message: str, confirm_label: str = "OK") -> bool:
    if platform.system() != "Darwin":
        return True
    try:
        safe_msg = message.replace('"', "'").replace("\n", " ")
        safe_title = title.replace('"', "'")
        safe_label = confirm_label.replace('"', "'")
        script_text = (
            f'display dialog "{safe_msg}" '
            f'with title "{safe_title}" '
            f'buttons {{"Cancel", "{safe_label}"}} '
            f'default button "Cancel" '
            f'with icon caution'
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".applescript",
            delete=False, encoding="utf-8"
        ) as tf:
            tf.write(script_text)
            tf_path = tf.name
        r = subprocess.run(
            ["osascript", tf_path],
            capture_output=True, text=True, timeout=60,
        )
        os.unlink(tf_path)
        return r.returncode == 0 and safe_label in r.stdout
    except Exception as e:
        logger.warning(f"Could not show dialog: {e}")
        return False


def text_input(title: str, message: str, default: str = "") -> str | None:
    if platform.system() != "Darwin":
        return None
    try:
        safe_msg = message.replace('"', "'").replace("\n", " ")
        safe_title = title.replace('"', "'")
        safe_default = default.replace('"', "'")
        script_text = (
            f'set result to display dialog "{safe_msg}" '
            f'with title "{safe_title}" '
            f'default answer "{safe_default}" '
            f'buttons {{"Cancel", "OK"}} default button "OK"'
            f'\nreturn text returned of result'
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".applescript",
            delete=False, encoding="utf-8"
        ) as tf:
            tf.write(script_text)
            tf_path = tf.name
        r = subprocess.run(
            ["osascript", tf_path],
            capture_output=True, text=True, timeout=120,
        )
        os.unlink(tf_path)
        if r.returncode == 0:
            return r.stdout.strip()
        return None
    except Exception as e:
        logger.warning(f"Could not show input dialog: {e}")
        return None


def folder_picker(title: str, default: str = "") -> str | None:
    if platform.system() != "Darwin":
        return None
    try:
        loc = ""
        if default:
            loc = f' default location POSIX file "{default}"'
        script = f'POSIX path of (choose folder with prompt "{title}"{loc})'
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0:
            return r.stdout.strip()
        return None
    except Exception as e:
        logger.warning(f"Folder picker failed: {e}")
        return None


def choose_from_list(title: str, options: list[str], default: str = "") -> str | None:
    if platform.system() != "Darwin":
        return None
    try:
        items = ", ".join(f'"{o}"' for o in options)
        default_item = f' default items {{"{default}"}}' if default in options else ""
        script = (
            f'choose from list {{{items}}} '
            f'with prompt "{title}"{default_item} '
            f'without multiple selections allowed'
        )
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0:
            val = r.stdout.strip()
            if val and val != "false":
                return val
        return None
    except Exception as e:
        logger.warning(f"Choose from list failed: {e}")
        return None
