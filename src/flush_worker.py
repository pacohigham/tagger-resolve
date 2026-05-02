# Copyright 2026 Tagger, LLC -- support@tagger.mov
"""Background worker that flushes the queue into the open Resolve project.

Polls every POLL_INTERVAL seconds. When Resolve is open with a project,
walks the pending queue and writes metadata. Items that don't match a
clip after MAX_MATCH_ATTEMPTS polls are marked 'skipped' so we stop
re-walking the Media Pool for files that will never be imported.
"""

from __future__ import annotations

import logging
import threading
import time

from metadata_queue import MetadataQueue
from resolve_connector import get_current_project
from resolve_writer import write_for_queue_row

logger = logging.getLogger(__name__)


POLL_INTERVAL = 5.0
MAX_MATCH_ATTEMPTS = 720          # 5s * 720 = 1 hour of attempts
BATCH_SIZE = 50


class FlushWorker:
    def __init__(self, queue: MetadataQueue, paused: bool = False):
        self.queue = queue
        self._paused = threading.Event()
        if paused:
            self._paused.set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_status: str = "idle"

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def status(self) -> str:
        return self._last_status

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="flush-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self._paused.is_set():
                    self._tick()
            except Exception as e:
                logger.exception(f"flush tick failed: {e}")
            self._stop.wait(POLL_INTERVAL)

    def _tick(self) -> None:
        project = get_current_project()
        if project is None:
            self._last_status = "waiting for Resolve"
            return

        pending = self.queue.list_pending(limit=BATCH_SIZE)
        if not pending:
            self._last_status = f"Project: {project.GetName()}"
            return

        wrote = 0
        skipped = 0
        for row in pending:
            ok, msg, count = write_for_queue_row(row)
            if ok:
                self.queue.mark_written(row["id"])
                wrote += count
                logger.info(f"WROTE {row['file_name']}: {msg}")
                continue

            attempts = self.queue.increment_attempts(row["id"])
            if msg == "no_project":
                # Resolve closed mid-batch; stop and try again next tick
                break
            if attempts >= MAX_MATCH_ATTEMPTS:
                self.queue.mark_skipped(row["id"], msg)
                skipped += 1
                logger.warning(f"SKIP {row['file_name']} after {attempts} attempts: {msg}")
            else:
                logger.debug(f"defer {row['file_name']} ({msg})")

        self._last_status = (
            f"{project.GetName()}: wrote {wrote}, skipped {skipped}, "
            f"pending {self.queue.count_pending()}"
        )
