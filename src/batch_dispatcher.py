# Copyright 2026 Tagger, LLC -- support@tagger.mov
"""Batch dispatcher: collects files into windowed batches for the Batch API.

The watcher and Tag Open Project feed file paths into add(). When the
window reaches N files or T seconds (whichever first), the dispatcher
extracts frames, submits a batch, polls for results, and enqueues
metadata into the local SQLite queue.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from batch_client import (
    BatchClient,
    BatchSubmitError,
    CreditsExhaustedError,
)
from claude_analyzer import ClaudeAnalyzer
from config import Config
from frame_extractor import FrameExtractor
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
        logger.info(f"Batch: extracting frames for {len(paths)} files")
        self._on_start()

        items: list[tuple[str, str, str, float, str, dict]] = []
        temp_dirs: list[str] = []

        try:
            for i, path in enumerate(paths, 1):
                name = Path(path).name
                self._status = f"Extracting {i}/{len(paths)}: {name}"
                if not Path(path).exists():
                    logger.warning(f"  [{i}/{len(paths)}] SKIP (missing): {path}")
                    continue
                duration = FrameExtractor.get_duration(path)
                if duration is None:
                    logger.warning(f"  [{i}/{len(paths)}] SKIP (no duration): {name}")
                    continue
                stitched, td = FrameExtractor.extract_and_stitch(path)
                if td:
                    temp_dirs.append(td)
                if not stitched:
                    logger.warning(f"  [{i}/{len(paths)}] SKIP (stitch failed): {name}")
                    continue
                b64 = BatchClient.encode_image(stitched)
                info = FrameExtractor._get_video_info(path) or {}
                custom_id = f"job-{i:04d}"
                items.append((custom_id, b64, name, duration, path, info))
                logger.info(f"  [{i}/{len(paths)}] stitched: {name} ({len(b64)//1024} KB)")

            if not items:
                logger.warning("Batch: no valid files to submit")
                return

            bc = BatchClient(
                proxy_url=self._cfg.proxy_url,
                license_key=self._cfg.license_key,
                hardware_id=self._cfg.effective_hardware_id,
                description_length=self._cfg.description_length,
            )

            self._status = f"Submitting {len(items)} files..."
            logger.info(f"Batch: submitting {len(items)} items")
            try:
                sub = bc.submit([(cid, b64, n) for (cid, b64, n, _, _, _) in items])
            except CreditsExhaustedError as e:
                logger.warning(f"Batch: credits exhausted, falling back to sync: {e}")
                self._sync_fallback(paths)
                return
            except BatchSubmitError as e:
                logger.error(f"Batch: submit failed, falling back to sync: {e}")
                self._sync_fallback(paths)
                return

            logger.info(
                f"Batch: submitted batch_id={sub.batch_id} "
                f"pre_deducted={sub.credits_pre_deducted} balance={sub.credits_remaining}"
            )

            def _progress(st):
                c = st.counts
                done = c.get('succeeded', 0)
                errs = c.get('errored', 0)
                left = c.get('processing', 0)
                self._status = f"Tagging: {done} done, {left} processing"
                if errs:
                    self._status += f", {errs} failed"
                logger.info(
                    f"Batch {sub.batch_id}: {st.processing_status} "
                    f"processing={left} succeeded={done} errored={errs}"
                )

            try:
                res = bc.wait_for(sub.batch_id, poll_interval=10.0, progress_cb=_progress)
            except TimeoutError as e:
                logger.error(f"Batch: polling timed out: {e}")
                return

            logger.info(
                f"Batch ended: succeeded={res.succeeded} failed={res.failed} "
                f"refunded={res.credits_refunded} balance={res.credits_remaining}"
            )

            by_id = {cid: (n, d, p, info) for (cid, _, n, d, p, info) in items}
            enqueued = 0
            is_demo = self._cfg and not self._cfg.is_licensed
            for r in res.results:
                if r.status != "succeeded" or not r.metadata:
                    logger.warning(f"  [{r.custom_id}] {r.status}: {r.error or '(no metadata)'}")
                    continue
                meta = dict(r.metadata)
                meta.setdefault("tagger_version", self._version)
                meta.setdefault("tagger_schema", bc.schema_version)
                meta.setdefault("processed_at", str(int(time.time())))
                name, dur, path, info = by_id[r.custom_id]
                if info.get("camera_make"):
                    meta["camera_make"] = info["camera_make"]
                if info.get("camera_model"):
                    meta["camera_model"] = info["camera_model"]
                if info.get("color_label"):
                    meta["color_space"] = info["color_label"]
                self._queue.enqueue(path, meta, duration_s=dur)
                enqueued += 1
                if is_demo:
                    self._cfg.use_demo_file()
                logger.info(f"  [{r.custom_id}] OK: {name}")

            logger.info(f"Batch: queued {enqueued} items for Resolve write")

        finally:
            self._status = ""
            for td in temp_dirs:
                FrameExtractor.cleanup_frames([], td)
            self._on_end()

    def _sync_fallback(self, paths: list[str]) -> None:
        logger.info(f"Sync fallback: processing {len(paths)} files individually")
        for p in paths:
            name = Path(p).name
            try:
                process_video(
                    p, self._analyzer, self._queue,
                    tagger_version=self._version, cfg=self._cfg,
                )
            except DemoExhaustedError as e:
                logger.warning(str(e))
                break
            except Exception as e:
                logger.exception(f"Sync fallback failed: {name}: {e}")
