# Copyright 2026 Tagger, LLC -- support@tagger.mov
"""Shared metadata merging logic for technical (file-derived) fields.

Used by both the sync path (video_tagger.process_video) and the batch
path (cmd_batch, batch_dispatcher) to overlay camera_make, camera_model,
and color_space onto AI-produced metadata before enqueueing.
"""

from __future__ import annotations

import time


def merge_technical_metadata(
    metadata: dict,
    video_info: dict,
    tagger_version: str,
    schema_version: str = "v2",
) -> dict:
    """Merge file-derived technical fields into AI metadata and set defaults.

    Mutates and returns the metadata dict.
    """
    metadata.setdefault("tagger_version", tagger_version)
    metadata.setdefault("tagger_schema", schema_version)
    metadata.setdefault("processed_at", str(int(time.time())))

    if video_info.get("camera_make"):
        metadata["camera_make"] = video_info["camera_make"]
    if video_info.get("camera_model"):
        metadata["camera_model"] = video_info["camera_model"]
    if video_info.get("color_label"):
        metadata["color_space"] = video_info["color_label"]

    return metadata
