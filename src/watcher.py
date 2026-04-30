# Copyright 2026 Tagger, LLC -- support@tagger.mov
"""Watch folder for incoming video files and dispatch to the tagger.

Uses watchdog cross-platform. Files are debounced so partial uploads
don't trigger processing until the file size has been stable for
STABILITY_SECONDS.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Iterable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)


DEFAULT_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".mxf", ".avi", ".mkv", ".mts", ".m2ts",
    ".prores", ".dnxhd", ".dnxhr",
}

STABILITY_SECONDS = 3.0
STABILITY_POLL_INTERVAL = 1.0


class _StabilityHandler(FileSystemEventHandler):
    def __init__(self, watcher: "FolderWatcher"):
        self._watcher = watcher

    def on_created(self, event):
        if not event.is_directory:
            self._watcher._enqueue_candidate(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._watcher._enqueue_candidate(event.dest_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._watcher._enqueue_candidate(event.src_path)


class FolderWatcher:
    """Watch a folder, dispatch stable video files to a callback."""

    def __init__(
        self,
        folder: str,
        on_stable: Callable[[str], None],
        extensions: Iterable[str] | None = None,
    ):
        self.folder = str(Path(folder).expanduser().resolve())
        self.on_stable = on_stable
        self.extensions = {e.lower() for e in (extensions or DEFAULT_EXTENSIONS)}
        self._observer: Observer | None = None
        self._candidates: dict[str, float] = {}
        self._processed: set[str] = set()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._poll_thread: threading.Thread | None = None

    def start(self) -> None:
        if not os.path.isdir(self.folder):
            raise FileNotFoundError(f"Watch folder does not exist: {self.folder}")
        self._observer = Observer()
        self._observer.schedule(_StabilityHandler(self), self.folder, recursive=True)
        self._observer.start()
        self._stop_event.clear()
        self._poll_thread = threading.Thread(
            target=self._stability_loop, name="watcher-stability", daemon=True
        )
        self._poll_thread.start()
        logger.info(f"Watching: {self.folder}")
        self._enqueue_existing()

    def stop(self) -> None:
        self._stop_event.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    def _enqueue_existing(self) -> None:
        for root, _, files in os.walk(self.folder):
            for f in files:
                p = os.path.join(root, f)
                self._enqueue_candidate(p)

    def _enqueue_candidate(self, path: str) -> None:
        if Path(path).suffix.lower() not in self.extensions:
            return
        with self._lock:
            if path in self._processed:
                return
            self._candidates[path] = time.time()

    def _stability_loop(self) -> None:
        last_sizes: dict[str, int] = {}
        while not self._stop_event.is_set():
            time.sleep(STABILITY_POLL_INTERVAL)
            now = time.time()
            ready: list[str] = []
            with self._lock:
                items = list(self._candidates.items())
            for path, first_seen in items:
                try:
                    size = os.path.getsize(path)
                except OSError:
                    with self._lock:
                        self._candidates.pop(path, None)
                    continue
                prev = last_sizes.get(path)
                last_sizes[path] = size
                if prev == size and (now - first_seen) >= STABILITY_SECONDS:
                    ready.append(path)
            for path in ready:
                with self._lock:
                    self._candidates.pop(path, None)
                    self._processed.add(path)
                last_sizes.pop(path, None)
                try:
                    self.on_stable(path)
                except Exception as e:
                    logger.exception(f"on_stable failed for {path}: {e}")
