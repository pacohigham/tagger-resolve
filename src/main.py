# Copyright 2026 Tagger, LLC -- support@tagger.mov
"""Tagger for Resolve entry point.

Default: cross-platform tray app via pystray. Right-click menu shows
status, pause/resume, settings dialog, push-now, and quit.

Headless / CLI modes:
  --process FILE       process a single video file once and exit
  --validate           run env checks and exit
  --status             print queue counts and exit
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys

from config import Config, db_path, log_path
from metadata_queue import MetadataQueue

VERSION = "0.2.1"


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter(
        "%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(getattr(logging, level))
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path(), maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    root.setLevel(logging.DEBUG)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tagger-resolve")
    parser.add_argument("--validate", action="store_true", help="Run env checks and exit")
    parser.add_argument("--status", action="store_true", help="Print queue counts and exit")
    parser.add_argument("--process", metavar="FILE", help="Process a single file and exit")
    parser.add_argument("--batch", nargs="+", metavar="FILE",
                        help="Process N files via the Batch API (cheaper, no rate limits)")
    parser.add_argument("--batch-from-resolve", action="store_true",
                        help="Batch-process every clip in the open Resolve project's Media Pool")
    parser.add_argument("--flush-once", action="store_true", help="Run one flush tick and exit")
    parser.add_argument("--apply-cached", action="store_true",
                        help="Re-apply cached metadata to clips in the open Resolve project")
    parser.add_argument("--clear-metadata", action="store_true",
                        help="Clear all Tagger metadata from the open Resolve project and reload it")
    parser.add_argument("--uninstall", action="store_true",
                        help="Remove app from /Applications and delete all app data")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    setup_logging(args.log_level)
    cfg = Config.load()
    queue = MetadataQueue(db_path())

    from cli import (
        cmd_validate, cmd_status, cmd_process_file, cmd_batch,
        cmd_flush_once, cmd_apply_cached, cmd_clear_metadata, cmd_uninstall,
    )

    if args.validate:
        return cmd_validate(cfg, VERSION)
    if args.status:
        return cmd_status(queue)
    if args.process:
        return cmd_process_file(cfg, queue, args.process, VERSION)
    if args.batch:
        return cmd_batch(cfg, queue, args.batch, VERSION)
    if args.batch_from_resolve:
        from resolve_connector import get_current_project
        from resolve_writer import _walk_clips
        project = get_current_project()
        if project is None:
            print("Resolve is not running with an open project.", file=sys.stderr)
            return 1
        clips = list(_walk_clips(project.GetMediaPool().GetRootFolder()))
        paths = [c.GetClipProperty("File Path") for c in clips]
        paths = [p for p in paths if p]
        return cmd_batch(cfg, queue, paths, VERSION)
    if args.flush_once:
        return cmd_flush_once(queue)
    if args.apply_cached:
        return cmd_apply_cached(queue)
    if args.clear_metadata:
        cmd_clear_metadata()
        return 0
    if args.uninstall:
        return cmd_uninstall()

    from tray import run_tray
    return run_tray(cfg, queue, VERSION)


if __name__ == "__main__":
    sys.exit(main())
