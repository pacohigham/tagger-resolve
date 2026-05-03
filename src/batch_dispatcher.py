# Copyright 2026 Tagger, LLC -- support@tagger.mov
"""Dispatcher: collects files into windowed groups, tags via sync API.

The watcher and Tag Open Project feed file paths into add(). When the
window reaches N files or T seconds (whichever first), the dispatcher
processes each file synchronously via the proxy server (~4s per clip).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from claude_analyzer import ClaudeAnalyzer
from config import Config
from metadata_queue import MetadataQueue
from video_tagger import process_video, DemoExhaustedError

logger = logging.getLogger(__name__)


class BatchDispatcher:
    def __init__(
        self,
        cfg: Config,
        queue: MetadataQueue,
        tagger_version: str,
        analyzer: ClaudeAnalyzer,
        on_batch_start: Callable | None = None,
        on_batch_end: Callable | None = None,
    ):
        self._cfg = cfg
        self._queue = queue
        self._version = tagger_version
        self._analyzer = analyzer
        self._on_start = on_batch_start or (lambda: None)
        self._on_end = on_batch_end or (lambda: None)

        self._batch_size = cfg.batch_size
        self._timeout = cfg.batch_window_seconds

        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._window: list[str] = []
        self._window_opened_at: float = 0.0
        self._force_flush = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._done_events: list[threading.Event] = []
        self._status: str = ""

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._dispatch_loop, name="batch-dispatcher", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        with self._cond:
            self._stop.set()
            self._force_flush = True
            self._cond.notify()
        if self._thread is not None:
            self._thread.join(timeout=300)
            self._thread = None

    def status(self) -> str:
        return self._status

    def add(self, path: str) -> None:
        with self._cond:
            self._window.append(path)
            if self._window_opened_at == 0.0:
                self._window_opened_at = time.time()
            self._cond.notify()

    def submit_paths(self, paths: list[str]) -> None:
        """Add all paths and block until every batch completes."""
        done = threading.Event()
        with self._cond:
            for p in paths:
                self._window.append(p)
            if self._window_opened_at == 0.0:
                self._window_opened_at = time.time()
            self._force_flush = True
            self._done_events.append(done)
            self._cond.notify()
        done.wait()

    def _should_flush(self) -> bool:
        if not self._window:
            return False
        if self._force_flush:
            return True
        if len(self._window) >= self._batch_size:
            return True
        if self._window_opened_at and (time.time() - self._window_opened_at) >= self._timeout:
            return True
        return False

    def _time_until_deadline(self) -> float | None:
        if not self._window or self._window_opened_at == 0.0:
            return None
        remaining = self._timeout - (time.time() - self._window_opened_at)
        return max(0.1, remaining)

    def _dispatch_loop(self) -> None:
        while True:
            with self._cond:
                while not self._should_flush():
                    if self._stop.is_set() and not self._window:
                        return
                    timeout = self._time_until_deadline()
                    self._cond.wait(timeout=timeout or 5.0)

                batch_paths = list(self._window)
                self._window.clear()
                self._window_opened_at = 0.0
                events_to_signal = list(self._done_events) if self._force_flush else []
                if self._force_flush and not self._window:
                    self._done_events.clear()
                self._force_flush = False

            chunks = [
                batch_paths[i:i + self._batch_size]
                for i in range(0, len(batch_paths), self._batch_size)
            ]
            for chunk in chunks:
                self._process_batch(chunk)

            for ev in events_to_signal:
                ev.set()

            if self._stop.is_set():
                return

    def _process_batch(self, paths: list[str]) -> None:
        logger.info(f"Processing {len(paths)} files")
        self._on_start()
        try:
            ok = 0
            for i, p in enumerate(paths, 1):
                name = Path(p).name
                self._status = f"Tagging {i}/{len(paths)}: {name}"
                try:
                    if process_video(
                        p, self._analyzer, self._queue,
                        tagger_version=self._version, cfg=self._cfg,
                    ):
                        ok += 1
                        logger.info(f"  [{i}/{len(paths)}] OK: {name}")
                except DemoExhaustedError as e:
                    logger.warning(str(e))
                    break
                except Exception as e:
                    logger.exception(f"  [{i}/{len(paths)}] FAILED: {name}: {e}")
            logger.info(f"Done: {ok}/{len(paths)} queued for Resolve write")
        finally:
            self._status = ""
            self._on_end()
