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
import threading
import time
from pathlib import Path

from batch_client import (
    BatchClient, BatchSubmitError, BatchNotReady, CreditsExhaustedError,
)
from claude_analyzer import ClaudeAnalyzer, test_proxy_connection
from config import Config, db_path, log_path
from flush_worker import FlushWorker
from frame_extractor import FrameExtractor, test_ffmpeg
from metadata_queue import MetadataQueue
from resolve_connector import describe as describe_resolve
from video_tagger import process_video
from watcher import FolderWatcher

logger = logging.getLogger(__name__)


VERSION = "0.1.0"


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


def build_analyzer(cfg: Config) -> ClaudeAnalyzer:
    return ClaudeAnalyzer(
        proxy_url=cfg.proxy_url,
        license_key=cfg.license_key,
        hardware_id=cfg.hardware_id,
        description_length=cfg.description_length,
    )


def cmd_validate(cfg: Config) -> int:
    print(f"Tagger for Resolve {VERSION}")
    print("ffmpeg/ffprobe:", "OK" if test_ffmpeg() else "MISSING")
    print("proxy:         ", "OK" if test_proxy_connection(cfg.proxy_url) else "FAIL")
    info = describe_resolve()
    print("resolve:")
    for k, v in info.items():
        print(f"  {k}: {v}")
    print("watch_folder: ", cfg.watch_folder or "(unset)")
    print("license_key:  ", "set" if cfg.license_key else "(unset)")
    print("hardware_id:  ", cfg.hardware_id or "(unset)")
    return 0


def cmd_status(queue: MetadataQueue) -> int:
    counts = queue.counts_by_status()
    if not counts:
        print("queue empty")
    else:
        for status, n in sorted(counts.items()):
            print(f"  {status:8s}  {n}")
    return 0


def cmd_process_file(cfg: Config, queue: MetadataQueue, path: str) -> int:
    if not Path(path).exists():
        print(f"file not found: {path}", file=sys.stderr)
        return 1
    analyzer = build_analyzer(cfg)
    ok = process_video(path, analyzer, queue, tagger_version=VERSION)
    print("queued" if ok else "failed")
    return 0 if ok else 1


def cmd_flush_once(queue: MetadataQueue) -> int:
    """Run a single flush tick and exit. Useful for cron / manual push."""
    worker = FlushWorker(queue)
    worker._tick()
    print(worker.status())
    return 0


def cmd_batch(cfg: Config, queue: MetadataQueue, paths: list[str]) -> int:
    """Process N files via the Batch API.

    Frame extraction stays local. Stitched grids are submitted as a single
    Anthropic batch (50% cheaper, separate quota). When the batch ends,
    metadata lands in the local queue and the existing flush worker pushes
    it into Resolve.
    """
    if not paths:
        print("no files supplied", file=sys.stderr)
        return 1

    # Stitch each file and collect (queue_row_or_path, b64, name, duration)
    items: list[tuple[str, str, str, float, str]] = []  # (custom_id, b64, name, dur, path)
    temp_dirs: list[str] = []
    print(f"Extracting frames for {len(paths)} files...")
    for i, path in enumerate(paths, 1):
        if not Path(path).exists():
            print(f"  [{i}/{len(paths)}] SKIP (missing): {path}")
            continue
        name = Path(path).name
        duration = FrameExtractor.get_duration(path)
        if duration is None:
            print(f"  [{i}/{len(paths)}] SKIP (no duration): {name}")
            continue
        stitched, td = FrameExtractor.extract_and_stitch(path)
        if not stitched:
            print(f"  [{i}/{len(paths)}] SKIP (stitch failed): {name}")
            if td:
                temp_dirs.append(td)
            continue
        b64 = BatchClient.encode_image(stitched)
        custom_id = f"job-{i:04d}"
        items.append((custom_id, b64, name, duration, path))
        if td:
            temp_dirs.append(td)
        print(f"  [{i}/{len(paths)}] stitched: {name} ({len(b64)//1024} KB)")

    if not items:
        print("no files to submit", file=sys.stderr)
        return 1

    # Submit batch
    bc = BatchClient(
        proxy_url=cfg.proxy_url,
        license_key=cfg.license_key,
        hardware_id=cfg.hardware_id,
        description_length=cfg.description_length,
    )

    print(f"\nSubmitting batch of {len(items)} items...")
    try:
        sub = bc.submit([(cid, b64, n) for (cid, b64, n, _, _) in items])
    except CreditsExhaustedError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 2
    except BatchSubmitError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 2
    print(f"  batch_id={sub.batch_id}")
    print(f"  pre-deducted={sub.credits_pre_deducted} balance_after={sub.credits_remaining}")

    # Poll
    print("\nWaiting for results (poll every 10s)...")
    def progress(st):
        c = st.counts
        print(
            f"  status={st.processing_status} "
            f"processing={c.get('processing',0)} "
            f"succeeded={c.get('succeeded',0)} errored={c.get('errored',0)}"
        )

    try:
        res = bc.wait_for(sub.batch_id, poll_interval=10.0, progress_cb=progress)
    except TimeoutError as e:
        print(f"TIMEOUT: {e}", file=sys.stderr)
        return 3

    print(f"\nBatch ended: succeeded={res.succeeded} failed={res.failed} "
          f"refunded={res.credits_refunded} balance={res.credits_remaining}")

    # Map results into the local queue
    by_id = {(cid): (n, d, p) for (cid, _, n, d, p) in items}
    enqueued = 0
    for r in res.results:
        if r.status != "succeeded" or not r.metadata:
            print(f"  [{r.custom_id}] {r.status}: {r.error or '(no metadata)'}")
            continue
        meta = dict(r.metadata)
        meta.setdefault("tagger_version", VERSION)
        meta.setdefault("tagger_schema", bc.schema_version)
        meta.setdefault("processed_at", str(int(time.time())))
        name, dur, path = by_id[r.custom_id]
        queue.enqueue(path, meta, duration_s=dur)
        enqueued += 1
        print(f"  [{r.custom_id}] OK: {name}")

    # Cleanup stitch temp dirs
    for td in temp_dirs:
        FrameExtractor.cleanup_frames([], td)

    print(f"\nQueued {enqueued} items for Resolve write.")
    print("Run --flush-once to push them into the open Resolve project.")
    return 0


def run_tray(cfg: Config, queue: MetadataQueue) -> int:
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError as e:
        print(f"pystray and Pillow required: {e}", file=sys.stderr)
        return 1

    analyzer = build_analyzer(cfg)
    worker = FlushWorker(queue, paused=not cfg.auto_push)
    worker.start()

    watcher: FolderWatcher | None = None
    if cfg.watch_folder and Path(cfg.watch_folder).is_dir():
        def on_stable(p: str) -> None:
            try:
                process_video(p, analyzer, queue, tagger_version=VERSION)
            except Exception as e:
                logger.exception(f"process_video failed for {p}: {e}")
        watcher = FolderWatcher(cfg.watch_folder, on_stable)
        try:
            watcher.start()
        except FileNotFoundError as e:
            logger.error(str(e))
            watcher = None
    else:
        logger.warning("watch_folder not configured; running in queue-only mode")

    icon_image = _make_icon()
    icon = pystray.Icon("TaggerResolve", icon_image, "Tagger for Resolve")

    def status_text(_):
        c = queue.counts_by_status()
        pending = c.get("pending", 0)
        written = c.get("written", 0)
        return f"Pending: {pending}  Written: {written}"

    def worker_status(_):
        return worker.status()

    def toggle_pause(icon, item):
        if worker.paused:
            worker.resume()
        else:
            worker.pause()
        icon.update_menu()

    def push_now(icon, item):
        threading.Thread(target=worker._tick, daemon=True).start()

    def open_logs(icon, item):
        import subprocess
        import platform
        p = str(log_path().parent)
        sys_name = platform.system()
        if sys_name == "Darwin":
            subprocess.Popen(["open", p])
        elif sys_name == "Windows":
            subprocess.Popen(["explorer", p])
        else:
            subprocess.Popen(["xdg-open", p])

    def quit_app(icon, item):
        icon.stop()

    icon.menu = pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.MenuItem(worker_status, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            lambda _: "Resume auto-push" if worker.paused else "Pause auto-push",
            toggle_pause,
        ),
        pystray.MenuItem("Push now", push_now),
        pystray.MenuItem("Open logs folder", open_logs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app),
    )

    try:
        icon.run()
    finally:
        worker.stop()
        if watcher is not None:
            watcher.stop()
    return 0


def _make_icon():
    """Generate a simple 64x64 sage-green square icon for the tray."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((4, 4, 60, 60), radius=12, fill=(62, 124, 110, 255))
    draw.text((18, 16), "T", fill=(247, 243, 235, 255))
    return img


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
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    setup_logging(args.log_level)
    cfg = Config.load()
    queue = MetadataQueue(db_path())

    if args.validate:
        return cmd_validate(cfg)
    if args.status:
        return cmd_status(queue)
    if args.process:
        return cmd_process_file(cfg, queue, args.process)
    if args.batch:
        return cmd_batch(cfg, queue, args.batch)
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
        return cmd_batch(cfg, queue, paths)
    if args.flush_once:
        return cmd_flush_once(queue)
    return run_tray(cfg, queue)


if __name__ == "__main__":
    sys.exit(main())
