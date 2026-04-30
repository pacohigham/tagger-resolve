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

from claude_analyzer import ClaudeAnalyzer, test_proxy_connection
from config import Config, db_path, log_path
from flush_worker import FlushWorker
from frame_extractor import test_ffmpeg
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
    if args.flush_once:
        return cmd_flush_once(queue)
    return run_tray(cfg, queue)


if __name__ == "__main__":
    sys.exit(main())
