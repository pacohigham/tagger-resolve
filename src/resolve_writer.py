# Copyright 2026 Tagger, LLC -- support@tagger.mov
"""Resolve metadata writer.

Walks the open project's Media Pool, matches clips by filename + duration,
and writes Tagger metadata via SetMetadata() / SetThirdPartyMetadata().

Match strategy (option C):
  - exact filename match, AND
  - duration within DURATION_TOLERANCE_S seconds
  - if multiple clips match the filename and only one matches duration,
    we use it; if multiple match both, we write to all (rare in practice)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional

from resolve_connector import get_current_project

logger = logging.getLogger(__name__)


DURATION_TOLERANCE_S = 0.5

# Mapping from Tagger metadata keys to Resolve native field names.
# Lists are joined into comma-separated strings before write.
NATIVE_FIELD_MAP = {
    "tags":            "Keywords",
    "description":     "Description",
    "scene":           "Scene",
    "shot_type":       "Shot",
    "primary_subject": "Comments",
}

# These keys are kept in the third-party namespace -- not visible in
# the Media Pool UI but readable via GetThirdPartyMetadata().
THIRD_PARTY_KEYS = {
    "primary_action",
    "transcript",
    "tagger_version",
    "confidence",
    "processed_at",
}


def _walk_clips(folder) -> Iterable:
    """Yield every MediaPoolItem under the given folder, recursively."""
    for clip in folder.GetClipList() or []:
        yield clip
    for sub in folder.GetSubFolderList() or []:
        yield from _walk_clips(sub)


def _clip_filename(clip) -> Optional[str]:
    """Return the on-disk filename of a MediaPoolItem, or None."""
    try:
        path = clip.GetClipProperty("File Path")
    except Exception:
        path = None
    if path:
        return Path(path).name
    try:
        return clip.GetName()
    except Exception:
        return None


def _clip_duration_seconds(clip) -> Optional[float]:
    """Return clip duration in seconds, or None if not derivable."""
    try:
        frames = clip.GetClipProperty("Frames")
        fps = clip.GetClipProperty("FPS")
        if frames and fps:
            return float(frames) / float(fps)
    except Exception:
        pass
    try:
        dur = clip.GetClipProperty("Duration")
        if dur and ":" in str(dur):
            parts = [float(x) for x in str(dur).split(":")]
            if len(parts) == 4:
                h, m, s, f = parts
                fps = float(clip.GetClipProperty("FPS") or 24)
                return h * 3600 + m * 60 + s + (f / fps if fps else 0)
    except Exception:
        pass
    return None


def find_matching_clips(
    project,
    file_name: str,
    duration_s: Optional[float],
) -> list:
    """Return Media Pool clips whose filename and (if known) duration match."""
    if project is None:
        return []
    root = project.GetMediaPool().GetRootFolder()
    same_name = []
    for clip in _walk_clips(root):
        name = _clip_filename(clip)
        if name and name.lower() == file_name.lower():
            same_name.append(clip)

    if not same_name or duration_s is None:
        return same_name

    same_duration = []
    for clip in same_name:
        clip_dur = _clip_duration_seconds(clip)
        if clip_dur is None:
            continue
        if abs(clip_dur - duration_s) <= DURATION_TOLERANCE_S:
            same_duration.append(clip)

    return same_duration if same_duration else same_name


def _build_field_payload(metadata: dict) -> tuple[dict, dict]:
    """Split metadata into native fields and third-party fields.

    Returns (native_payload, third_party_payload). Native values are
    coerced to strings; lists become comma-separated.
    """
    native: dict[str, str] = {}
    for key, field_name in NATIVE_FIELD_MAP.items():
        value = metadata.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, list):
            value = ",".join(str(v).strip() for v in value if str(v).strip())
        if value:
            native[field_name] = str(value)

    third: dict[str, str] = {}
    for key in THIRD_PARTY_KEYS:
        value = metadata.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, list):
            value = ",".join(str(v) for v in value)
        third[f"Tagger.{key}"] = str(value)

    return native, third


def write_metadata_to_clip(clip, metadata: dict) -> tuple[bool, str]:
    """Write metadata to a single clip. Returns (ok, message)."""
    native, third = _build_field_payload(metadata)
    if not native and not third:
        return False, "No fields to write"

    errors: list[str] = []

    # Per-field writes: dict mode is all-or-nothing across Resolve versions,
    # so a single unknown field name invalidates the whole batch. Writing one
    # field at a time gives partial success and a clear log of which fields
    # this Resolve version recognises.
    written_fields: list[str] = []
    for name, value in native.items():
        try:
            ok = clip.SetMetadata(name, value)
            if ok:
                written_fields.append(name)
            else:
                errors.append(f"SetMetadata({name!r}) returned False")
        except Exception as e:
            errors.append(f"SetMetadata({name!r}) raised {e!r}")

    if third and hasattr(clip, "SetThirdPartyMetadata"):
        try:
            ok = clip.SetThirdPartyMetadata(third)
            if not ok:
                errors.append(f"SetThirdPartyMetadata returned False for {list(third)}")
        except Exception as e:
            errors.append(f"SetThirdPartyMetadata raised {e!r}")

    if not written_fields and errors:
        return False, "; ".join(errors)
    msg = f"wrote {written_fields}"
    if third:
        msg += f" + thirdparty={list(third)}"
    if errors:
        msg += f" (warnings: {'; '.join(errors)})"
    return True, msg


def write_for_queue_row(row: dict) -> tuple[bool, str, int]:
    """Write metadata for a queue row into the open Resolve project.

    Returns (ok, message, clips_written). When ok is False but Resolve
    is open, the caller should treat it as a transient failure and
    retry next tick. When the project is closed, returns
    (False, 'no_project', 0) so the caller can keep waiting.
    """
    project = get_current_project()
    if project is None:
        return False, "no_project", 0

    file_name = row.get("file_name") or Path(row["file_path"]).name
    duration_s = row.get("duration_s")
    metadata = row.get("metadata") or {}

    matches = find_matching_clips(project, file_name, duration_s)
    if not matches:
        return False, f"no clip matching {file_name}", 0

    written = 0
    last_msg = ""
    for clip in matches:
        ok, msg = write_metadata_to_clip(clip, metadata)
        last_msg = msg
        if ok:
            written += 1
        else:
            logger.warning(f"Failed write for {file_name}: {msg}")

    if written == 0:
        return False, last_msg, 0
    return True, f"wrote to {written}/{len(matches)} clip(s); {last_msg}", written
