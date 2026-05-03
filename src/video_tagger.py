# Copyright 2026 Tagger, LLC -- support@tagger.mov
"""Per-file orchestrator: extract -> analyze -> enqueue.

Implements the confidence-gated retry inherited from Tagger v1.2.4:
if required fields are missing on the first pass, re-extract frames
at shifted temporal positions (pct_offset=0.075) and try once more.
"""

from __future__ import annotations

import logging
from pathlib import Path

from claude_analyzer import ClaudeAnalyzer, CreditsExhaustedError
from config import Config
from frame_extractor import FrameExtractor
from metadata_merge import merge_technical_metadata
from metadata_queue import MetadataQueue


class DemoExhaustedError(Exception):
    """All 20 free demo files have been used."""

logger = logging.getLogger(__name__)


REQUIRED_FIELDS = ("description", "tags", "shot_size", "footage_type")


def _is_complete(metadata: dict) -> bool:
    return all(metadata.get(f) for f in REQUIRED_FIELDS)


def process_video(
    video_path: str,
    analyzer: ClaudeAnalyzer,
    queue: MetadataQueue,
    tagger_version: str = "0.1.0",
    cfg: Config | None = None,
) -> bool:
    """Process a single video file end-to-end.

    Returns True if metadata was queued for Resolve write. On hard
    failure (invalid file, no metadata after retry) returns False and
    logs. CreditsExhaustedError and DemoExhaustedError are propagated
    so the caller can surface a notification.
    """
    is_demo = cfg and not cfg.is_licensed
    if is_demo:
        if cfg.demo_remaining <= 0:
            raise DemoExhaustedError(
                f"All {DEMO_LIMIT} free demo files used. "
                "Enter a license key to continue."
            )

    name = Path(video_path).name
    logger.info(f"Processing {name}")

    duration = FrameExtractor.get_duration(video_path)
    if duration is None:
        logger.error(f"Could not determine duration: {name}")
        return False

    stitched, temp_dir = FrameExtractor.extract_and_stitch(video_path)
    metadata: dict = {}
    if stitched:
        try:
            metadata = analyzer.analyze_grid(stitched)
        finally:
            FrameExtractor.cleanup_frames([stitched], temp_dir)
    else:
        FrameExtractor.cleanup_frames([], temp_dir)

    if not _is_complete(metadata):
        logger.info(f"{name}: incomplete metadata, retrying with shifted offsets")
        s2, t2 = FrameExtractor.extract_and_stitch(video_path, pct_offset=0.075)
        if s2:
            try:
                retry = analyzer.analyze_grid(s2)
                if retry:
                    metadata = retry
            finally:
                FrameExtractor.cleanup_frames([s2], t2)
        else:
            FrameExtractor.cleanup_frames([], t2)

    if not metadata:
        logger.error(f"{name}: no metadata produced")
        return False

    info = FrameExtractor._get_video_info(video_path) or {}
    merge_technical_metadata(
        metadata, info,
        tagger_version=tagger_version,
        schema_version=getattr(analyzer, "schema_version", "v2"),
    )

    row_id = queue.enqueue(video_path, metadata, duration_s=duration)

    if is_demo:
        cfg.use_demo_file()
        logger.info(f"{name}: demo file used, {cfg.demo_remaining}/{DEMO_LIMIT} remaining")

    logger.info(f"{name}: queued as row {row_id}")
    return True
