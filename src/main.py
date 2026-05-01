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
import subprocess
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
    try:
        from braw_extractor import is_braw_available
        print("BRAW SDK:      ", "OK" if is_braw_available() else "not installed (.braw files will be skipped)")
    except Exception as e:
        print(f"BRAW SDK:       ERROR ({e})")
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

    # Stitch each file and collect tuple per item:
    #   (custom_id, b64, name, duration, path, video_info)
    items: list[tuple[str, str, str, float, str, dict]] = []
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
        info = FrameExtractor._get_video_info(path) or {}
        custom_id = f"job-{i:04d}"
        items.append((custom_id, b64, name, duration, path, info))
        if td:
            temp_dirs.append(td)
        cam = f"{info.get('camera_make','?')} / {info.get('camera_model','?')}"
        print(f"  [{i}/{len(paths)}] stitched: {name} ({len(b64)//1024} KB)  cam={cam}")

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
        sub = bc.submit([(cid, b64, n) for (cid, b64, n, _, _, _) in items])
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

    # Map results into the local queue, merging file-derived technical
    # metadata (camera_make / camera_model / color_space) so editors can
    # filter by camera in Resolve Smart Bins.
    by_id = {cid: (n, d, p, info) for (cid, _, n, d, p, info) in items}
    enqueued = 0
    for r in res.results:
        if r.status != "succeeded" or not r.metadata:
            print(f"  [{r.custom_id}] {r.status}: {r.error or '(no metadata)'}")
            continue
        meta = dict(r.metadata)
        meta.setdefault("tagger_version", VERSION)
        meta.setdefault("tagger_schema", bc.schema_version)
        meta.setdefault("processed_at", str(int(time.time())))
        name, dur, path, info = by_id[r.custom_id]
        if info.get("camera_make"):
            meta["camera_make"] = info["camera_make"]
        if info.get("camera_model"):
            meta["camera_model"] = info["camera_model"]
        if info.get("color_label"):
            meta["color_space"] = info["color_label"]
        queue.enqueue(path, meta, duration_s=dur)
        enqueued += 1
        print(f"  [{r.custom_id}] OK: {name}")

    # Cleanup stitch temp dirs
    for td in temp_dirs:
        FrameExtractor.cleanup_frames([], td)

    print(f"\nQueued {enqueued} items for Resolve write.")
    print("Run --flush-once to push them into the open Resolve project.")
    return 0


_TAGGER_NATIVE_FIELDS = [
    "Keywords", "Description", "Scene", "Shot", "Angle", "Comments",
]


def _do_clear_metadata() -> None:
    """Worker thread: confirm, clear, save, close, reopen.

    Runs off the main thread so the tray stays responsive.
    """
    from resolve_connector import get_resolve, get_current_project
    from resolve_writer import _walk_clips

    project = get_current_project()
    if project is None:
        logger.warning("Clear Metadata: no Resolve project is open")
        return

    project_name = project.GetName()
    clips = list(_walk_clips(project.GetMediaPool().GetRootFolder()))
    tagged = sum(
        1 for c in clips
        if any((c.GetMetadata() or {}).get(f) for f in _TAGGER_NATIVE_FIELDS)
        or (c.GetThirdPartyMetadata() or {})
    )

    # --- Confirmation dialog (macOS native) ---
    confirmed = _confirm_dialog(
        title="Clear Tagger Metadata",
        message=(
            f"Remove all Tagger-generated metadata from {tagged} clip(s) "
            f"in \"{project_name}\"?\n\n"
            "Keywords, Descriptions, Scene, Shot, Angle and all Tagger fields "
            "will be cleared. The project will be saved and reloaded so the "
            "keyword bins are removed from the Media Pool.\n\n"
            "This cannot be undone."
        ),
    )
    if not confirmed:
        logger.info("Clear Metadata: cancelled by user")
        return

    # --- Clear metadata from all clips ---
    nc = tc = 0
    for c in clips:
        existing = c.GetMetadata() or {}
        for f in _TAGGER_NATIVE_FIELDS:
            if existing.get(f):
                if c.SetMetadata(f, ""):
                    nc += 1
        third = c.GetThirdPartyMetadata() or {}
        if third:
            c.SetThirdPartyMetadata({k: "" for k in third})
            tc += len(third)

    logger.info(f"Cleared {nc} native + {tc} third-party fields from {len(clips)} clips")

    # --- Save FIRST. Abort if save fails -- never close an unsaved project. ---
    resolve = get_resolve()
    pm = resolve.GetProjectManager()

    logger.info(f"Saving {project_name}...")
    saved = pm.SaveProject()
    if not saved:
        logger.error(
            f"SaveProject() returned False for {project_name!r}. "
            "Metadata was cleared from clips but the project was NOT closed. "
            "Save the project manually before reopening to clear keyword bins."
        )
        return

    logger.info(f"Project saved: {project_name}")

    # --- Reload to clear keyword bins.
    # CloseProject() returns False on the currently active project.
    # LoadProject on the same name while it is already active does not
    # trigger a real reload -- Resolve returns the existing handle.
    #
    # Reliable pattern: create a disposable empty project (switches active
    # project away from ours), then LoadProject to pull ours fresh from the
    # just-saved database. Resolve rebuilds the keyword bin tree from
    # scratch; with all keywords cleared, the bins disappear.
    _TEMP = "__TFR_temp_reload__"
    temp = pm.CreateProject(_TEMP)
    if not temp:
        logger.warning(
            "Could not create temp project for reload. Metadata is cleared "
            "and saved. Close and reopen the project in Resolve manually to "
            "clear the keyword bins."
        )
        return

    reloaded = pm.LoadProject(project_name)
    pm.DeleteProject(_TEMP)

    if reloaded:
        logger.info(f"Project reloaded: {project_name} -- keyword bins cleared")
    else:
        logger.warning(
            f"LoadProject({project_name!r}) failed. Metadata is cleared and "
            "saved. Open the project manually in Resolve."
        )


def _confirm_dialog(title: str, message: str) -> bool:
    """Show a native confirmation dialog. Returns True if the user confirms."""
    import platform
    system = platform.system()
    if system == "Darwin":
        try:
            # Write the AppleScript to a temp file so we avoid shell quoting
            # and encoding issues with the message string.
            import tempfile
            safe_msg = message.replace('"', "'").replace("\n", " ")
            safe_title = title.replace('"', "'")
            script_text = (
                f'display dialog "{safe_msg}" '
                f'with title "{safe_title}" '
                f'buttons {{"Cancel", "Clear Metadata"}} '
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
            import os; os.unlink(tf_path)
            return r.returncode == 0 and "Clear Metadata" in r.stdout
        except Exception as e:
            logger.warning(f"Could not show dialog: {e}")
            return False
    # Windows / Linux: just proceed (no easy cross-platform dialog here)
    return True


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

    def clear_tagger_metadata(icon, item):
        """Clear all Tagger-written metadata from every clip in the open
        project, then save + close + reopen the project so Resolve rebuilds
        its keyword bin tree from scratch (empty).

        Called directly (not threaded) because the Resolve scripting API
        is not thread-safe -- calls from background threads silently fail.
        The tray will be briefly unresponsive during the dialog + clear,
        which is acceptable for a destructive operation.
        """
        _do_clear_metadata()

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
        pystray.MenuItem("Clear Tagger Metadata...", clear_tagger_metadata),
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
    parser.add_argument("--clear-metadata", action="store_true",
                        help="Clear all Tagger metadata from the open Resolve project and reload it")
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
    if args.clear_metadata:
        _do_clear_metadata()
        return 0
    return run_tray(cfg, queue)


if __name__ == "__main__":
    sys.exit(main())
