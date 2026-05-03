# Copyright 2026 Tagger, LLC -- support@tagger.mov
"""Tray (menu bar) UI for Tagger for Resolve.

Builds the pystray icon, wires up menu items, manages the processing
animation, and coordinates the watcher + flush worker + batch dispatcher.
"""

from __future__ import annotations

import logging
import subprocess
import platform
import sys
import threading
from pathlib import Path

from batch_dispatcher import BatchDispatcher
from cli import build_analyzer
from config import Config, DEMO_LIMIT, log_path
from dialogs import text_input, folder_picker, choose_from_list
from flush_worker import FlushWorker
from metadata_queue import MetadataQueue
from resolve_helpers import save_and_reload_project
from video_tagger import process_video, DemoExhaustedError
from watcher import FolderWatcher

logger = logging.getLogger(__name__)


def _assets_dir() -> Path:
    if getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS) / "assets"
    return Path(__file__).parent / "assets"


def _load_icon(path: Path):
    from PIL import Image
    return Image.open(path).convert("RGBA")


def _make_icon():
    icon_path = _assets_dir() / "tagger_menubar.png"
    if icon_path.exists():
        return _load_icon(icon_path)
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (44, 44), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((2, 2, 42, 42), radius=8, fill=(62, 124, 110, 255))
    return img


def _load_animation_frames() -> list:
    frames_dir = _assets_dir() / "menubar_frames"
    if not frames_dir.is_dir():
        return []
    paths = sorted(frames_dir.glob("frame_*.png"))
    return [_load_icon(p) for p in paths]


def run_tray(cfg: Config, queue: MetadataQueue, version: str) -> int:
    try:
        import pystray
    except ImportError as e:
        print(f"pystray and Pillow required: {e}", file=sys.stderr)
        return 1

    analyzer = build_analyzer(cfg)
    worker = FlushWorker(queue)
    worker.start()

    icon_image = _make_icon()
    animation_frames = _load_animation_frames()
    icon = pystray.Icon("TaggerResolve", icon_image, "Tagger for Resolve")

    _anim_lock = threading.Lock()
    _processing_count = 0
    _frame_index = 0
    _anim_stop = threading.Event()

    def _processing_count_inc():
        nonlocal _processing_count
        with _anim_lock:
            _processing_count += 1

    def _processing_count_dec():
        nonlocal _processing_count
        with _anim_lock:
            _processing_count = max(0, _processing_count - 1)

    dispatcher = BatchDispatcher(
        cfg=cfg, queue=queue, tagger_version=version, analyzer=analyzer,
        on_batch_start=_processing_count_inc,
        on_batch_end=_processing_count_dec,
    )
    dispatcher.start()

    def on_stable(p: str) -> None:
        dispatcher.add(p)

    watcher: FolderWatcher | None = None
    if cfg.watch_folder and Path(cfg.watch_folder).is_dir():
        watcher = FolderWatcher(cfg.watch_folder, on_stable)
        try:
            watcher.start()
        except FileNotFoundError as e:
            logger.error(str(e))
            watcher = None
    else:
        logger.warning("watch_folder not configured; running in queue-only mode")

    def _animation_tick():
        nonlocal _frame_index
        while not _anim_stop.is_set():
            with _anim_lock:
                active = _processing_count > 0
            if active and animation_frames:
                _frame_index = (_frame_index + 1) % len(animation_frames)
                icon.icon = animation_frames[_frame_index]
            elif _frame_index != 0:
                _frame_index = 0
                icon.icon = icon_image
            _anim_stop.wait(0.08)

    if animation_frames:
        anim_thread = threading.Thread(target=_animation_tick, daemon=True)
        anim_thread.start()

    def license_status(_):
        if cfg.is_licensed:
            return "Licensed"
        return f"Demo: {cfg.demo_remaining}/{DEMO_LIMIT} files remaining"

    def status_text(_):
        c = queue.counts_by_status()
        pending = c.get("pending", 0)
        written = c.get("written", 0)
        return f"Pending: {pending}  Written: {written}"

    def worker_status(_):
        return worker.status()

    def tag_open_project(icon, item):
        def _run():
            from resolve_connector import get_current_project
            from resolve_writer import _walk_clips

            project = get_current_project()
            if project is None:
                logger.warning("Tag Open Project: no Resolve project is open")
                return

            clips = list(_walk_clips(project.GetMediaPool().GetRootFolder()))
            paths = [c.GetClipProperty("File Path") for c in clips]
            paths = [p for p in paths if p and Path(p).exists()]

            if not paths:
                logger.warning("Tag Open Project: no clips with valid file paths")
                return

            project_name = project.GetName()
            logger.info(f"Tag Open Project: processing {len(paths)} clips")
            _processing_count_inc()
            try:
                for i, p in enumerate(paths, 1):
                    name = Path(p).name
                    logger.info(f"  [{i}/{len(paths)}] {name}")
                    try:
                        process_video(p, analyzer, queue, tagger_version=version, cfg=cfg)
                    except DemoExhaustedError as e:
                        logger.warning(str(e))
                        break
                    except Exception as e:
                        logger.exception(f"  Failed: {name}: {e}")

                logger.info("Tag Open Project: done, flushing queue to Resolve")
                worker._tick()
                save_and_reload_project(project_name)
            except Exception as e:
                logger.exception(f"Tag Open Project failed: {e}")
            finally:
                _processing_count_dec()

        threading.Thread(target=_run, daemon=True).start()

    def enter_license(icon, item):
        nonlocal analyzer
        current = cfg.license_key or ""
        entered = text_input(
            title="Enter License Key",
            message="Enter your Tagger for Resolve license key to unlock the full monthly quota.",
            default=current,
        )
        if entered is None or not entered.strip() or entered.strip() == current:
            return
        cfg.license_key = entered.strip()
        cfg.save()
        logger.info("License key saved")

        if platform.system() == "Darwin":
            try:
                subprocess.run(
                    ["osascript", "-e",
                     'display dialog "License key saved. Tagger for Resolve is now licensed." '
                     'buttons {"OK"} default button "OK" with title "License Activated"'],
                    capture_output=True, timeout=30,
                )
            except Exception:
                pass

        analyzer = build_analyzer(cfg)
        icon.update_menu()

    def show_settings(icon, item):
        nonlocal analyzer, watcher

        folder = folder_picker(
            "Choose a watch folder (or Cancel to skip)",
            default=cfg.watch_folder,
        )
        if folder is not None:
            old_folder = cfg.watch_folder
            cfg.watch_folder = folder.rstrip("/")
            if cfg.watch_folder != old_folder:
                if watcher is not None:
                    watcher.stop()
                    watcher = None
                if cfg.watch_folder and Path(cfg.watch_folder).is_dir():
                    watcher = FolderWatcher(cfg.watch_folder, on_stable)
                    watcher.start()
                    logger.info(f"Watcher restarted: {cfg.watch_folder}")

        desc = choose_from_list(
            "Description length",
            ["brief", "standard", "detailed"],
            default=cfg.description_length,
        )
        if desc is not None:
            cfg.description_length = desc

        cfg.save()
        analyzer = build_analyzer(cfg)
        icon.update_menu()
        logger.info("Settings saved")

    def open_logs(icon, item):
        p = str(log_path().parent)
        sys_name = platform.system()
        if sys_name == "Darwin":
            subprocess.Popen(["open", p])
        elif sys_name == "Windows":
            subprocess.Popen(["explorer", p])
        else:
            subprocess.Popen(["xdg-open", p])

    def clear_tagger_metadata(icon, item):
        from cli import cmd_clear_metadata
        cmd_clear_metadata()

    def quit_app(icon, item):
        _anim_stop.set()
        icon.stop()

    icon.menu = pystray.Menu(
        pystray.MenuItem(license_status, None, enabled=False),
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.MenuItem(worker_status, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Tag Open Project", tag_open_project),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Clear Tagger Metadata", clear_tagger_metadata),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Settings...", show_settings),
        pystray.MenuItem("Enter License Key", enter_license),
        pystray.MenuItem("Open logs folder", open_logs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app),
    )

    try:
        icon.run()
    finally:
        _anim_stop.set()
        dispatcher.stop()
        worker.stop()
        if watcher is not None:
            watcher.stop()
    return 0
