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

# v2 schema (general production). Direct one-to-one mappings to Resolve
# native column fields. Single-value enums per field.
NATIVE_DIRECT_MAP = {
    "shot_size":     "Shot",
    "camera_angle":  "Angle",
    "footage_type":  "Scene",
}

# These v2 fields all merge into the Resolve "Keywords" field. Each token
# auto-creates a sub-bin in Resolve, so combining all of them into one
# comma-separated list maximises Smart Bin coverage. Order matters only
# for de-dup tie-breaking and the Description display order in the
# inspector; Smart Bins find them regardless.
KEYWORD_SOURCE_FIELDS = (
    "setting",            # Interior, Exterior, Vehicle, Studio, Mixed
    "environment",        # Urban, Suburban, Rural, Wilderness, ...
    "location_type",      # Street, Park, Office, Construction Site, ...
    "lighting",           # Day, Night, Golden Hour, ...
    "subject_category",   # list[Person, Group, Crowd, ...]
    "shot_composition",   # Single Subject, Two Shot, Group Shot, ...
    "audio_character",    # Sync Dialogue, Nat Sound, Music, ...
    "camera_make",        # Blackmagic, ARRI, Sony, DJI, ... (file-derived)
    "camera_model",       # URSA Mini Pro 12K, Alexa Mini LF, ... (file-derived)
    "color_space",        # Sony S-Log3, ARRI LogC4, ... (file-derived, optional)
    "tags",               # free-form list, 3-8 tokens
)

# Description gets the action verb-phrase appended for richer text search.
DESCRIPTION_SOURCE_FIELDS = ("description", "action")

# Third-party namespace fields -- not visible in default UI columns but
# readable via GetThirdPartyMetadata() and (per BMD docs) Smart-Bin
# filterable. Includes both v2 enum fields without a native home and our
# own provenance metadata.
THIRD_PARTY_KEYS = {
    "camera_movement",
    "person_count",
    "tagger_version",
    "tagger_schema",
    "processed_at",
    # Legacy v1 keys -- preserved so re-tagging an old clip with new schema
    # doesn't drop pre-existing third-party data on this clip.
    "primary_action",
    "transcript",
    "confidence",
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


def _scrub_token(s: str) -> str:
    """Defensive cleanup for a metadata token before it reaches Resolve.

    Replaces hyphens with spaces, collapses whitespace, and trims.
    The v2 prompt forbids hyphens, but a stray model response should still
    produce clean Keyword sub-bins.
    """
    if not s:
        return ""
    cleaned = str(s).replace("-", " ")
    return " ".join(cleaned.split()).strip()


def _to_token_list(value) -> list[str]:
    """Coerce a string or list value into a clean list of trimmed tokens.

    Splits comma-joined strings, trims whitespace, replaces hyphens with
    spaces, drops empties.
    """
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        items = value
    else:
        # Server normalises some lists to comma-joined strings; split back.
        items = [s for s in str(value).split(",")]
    out: list[str] = []
    for item in items:
        token = _scrub_token(item)
        if token:
            out.append(token)
    return out


def _build_keywords(metadata: dict) -> str:
    """Combine setting / lighting / subject_category / audio_character /
    tags into one comma-separated Keywords string. Each token auto-creates
    a Resolve keyword sub-bin, so the order is preserved and duplicates are
    removed case-insensitively.
    """
    seen: set[str] = set()
    tokens: list[str] = []
    for source in KEYWORD_SOURCE_FIELDS:
        for token in _to_token_list(metadata.get(source)):
            key = token.lower()
            if key in seen:
                continue
            seen.add(key)
            tokens.append(token)
    return ", ".join(tokens)


def _build_description(metadata: dict) -> str:
    """Combine description and action into a single readable line."""
    desc = str(metadata.get("description") or "").strip()
    action = str(metadata.get("action") or "").strip()
    if not action:
        return desc
    if not desc:
        return action
    if action.lower() in desc.lower():
        return desc
    sep = " " if desc.endswith((".", "!", "?")) else ". "
    return f"{desc}{sep}{action}"


def _build_field_payload(metadata: dict) -> tuple[dict, dict]:
    """Split v2 metadata into native fields and third-party fields.

    Returns (native_payload, third_party_payload). Native values are
    plain strings ready for SetMetadata().
    """
    native: dict[str, str] = {}

    # Direct one-to-one enums (Shot, Angle, Scene). These are display-facing
    # column values and must not contain hyphens for clean Smart Bin behaviour.
    for key, field_name in NATIVE_DIRECT_MAP.items():
        value = metadata.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, list):
            value = ", ".join(_scrub_token(v) for v in value if _scrub_token(v))
        else:
            value = _scrub_token(value)
        if value:
            native[field_name] = value

    # Description = description + action (richer text search)
    desc_combined = _build_description(metadata)
    if desc_combined:
        native["Description"] = desc_combined

    # Keywords = combined setting + lighting + subject + audio + tags
    keywords = _build_keywords(metadata)
    if keywords:
        native["Keywords"] = keywords

    # Third-party namespace. Enum-like fields (camera_movement, person_count)
    # are scrubbed so a stray hyphen in the model response never lands.
    # Free-form fields (transcript, processed_at, tagger_version) are passed
    # through untouched.
    third: dict[str, str] = {}
    enum_third = {"camera_movement", "person_count"}
    for key in THIRD_PARTY_KEYS:
        value = metadata.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        if key in enum_third:
            value = _scrub_token(value)
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
