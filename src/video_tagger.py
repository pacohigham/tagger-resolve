# Copyright 2026 Tagger, LLC -- support@tagger.mov
"""Per-file orchestrator: extract -> analyze -> enqueue.

Implements the confidence-gated retry inherited from Tagger v1.2.4:
if required fields are missing on the first pass, re-extract frames
at shifted temporal positions (pct_offset=0.075) and try once more.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from claude_analyzer import ClaudeAnalyzer, CreditsExhaustedError
from frame_extractor import FrameExtractor
from metadata_queue import MetadataQueue

logger = logging.getLogger(__name__)


REQUIRED_FIELDS = ("description", "tags", "primary_subject", "primary_action")


def _is_complete(metadata: dict) -> bool:
    return all(metadata.get(f) for f in REQUIRED_FIELDS)


def process_video(
    video_path: str,
    analyzer: ClaudeAnalyzer,
    queue: MetadataQueue,
    tagger_version: str = "0.1.0",
) -> bool:
    """Process a single video file end-to-end.

    Returns True if metadata was queued for Resolve write. On hard
    failure (invalid file, no metadata after retry) returns False and
    logs. CreditsExhaustedError is propagated so the watcher can
    surface a tray notification.
    """
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

    metadata.setdefault("tagger_version", tagger_version)
    metadata.setdefault("processed_at", str(int(time.time())))

    row_id = queue.enqueue(video_path, metadata, duration_s=duration)
    logger.info(f"{name}: queued as row {row_id}")
    return True
