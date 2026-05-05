# Copyright 2026 Tagger, LLC -- support@tagger.mov
"""SQLite-backed metadata queue.

Stores per-file analysis results so they survive restarts and can be
flushed into Resolve once a project is open. Status flow:

  pending  -- analysis succeeded, awaiting Resolve write
  written  -- successfully written to a Media Pool clip
  failed   -- analysis failed permanently (kept for diagnostics)
  skipped  -- file was queued but not found in any opened project
              after retries exhausted
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path    TEXT NOT NULL,
    file_name    TEXT NOT NULL,
    duration_s   REAL,
    metadata     TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status);
CREATE INDEX IF NOT EXISTS idx_queue_filename ON queue(file_name);
"""


class MetadataQueue:
    """Thread-safe SQLite queue for pending Resolve metadata writes."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def enqueue(
        self,
        file_path: str,
        metadata: dict,
        duration_s: Optional[float] = None,
    ) -> int:
        """Insert a new pending row and return its id.

        Replaces any prior pending/failed row for the same file_path so
        re-processing the same file doesn't accumulate duplicates.
        """
        now = time.time()
        name = Path(file_path).name
        meta_json = json.dumps(metadata)
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM queue WHERE file_path = ? AND status IN ('pending','failed')",
                (file_path,),
            )
            cur = conn.execute(
                """
                INSERT INTO queue
                  (file_path, file_name, duration_s, metadata, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (file_path, name, duration_s, meta_json, now, now),
            )
            return int(cur.lastrowid)

    def list_pending(self, limit: int = 100) -> list[dict]:
        """Return up to limit pending rows ordered by oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM queue WHERE status = 'pending' "
                "ORDER BY created_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def count_pending(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM queue WHERE status = 'pending'"
            ).fetchone()
        return int(row["n"]) if row else 0

    def counts_by_status(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM queue GROUP BY status"
            ).fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    def mark_written(self, row_id: int) -> None:
        self._update_status(row_id, "written", error=None)

    def mark_failed(self, row_id: int, error: str) -> None:
        self._update_status(row_id, "failed", error=error)

    def mark_skipped(self, row_id: int, error: str) -> None:
        self._update_status(row_id, "skipped", error=error)

    def increment_attempts(self, row_id: int) -> int:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE queue SET attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (time.time(), row_id),
            )
            row = conn.execute(
                "SELECT attempts FROM queue WHERE id = ?", (row_id,)
            ).fetchone()
        return int(row["attempts"]) if row else 0

    def _update_status(self, row_id: int, status: str, error: Optional[str]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE queue SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
                (status, error, time.time(), row_id),
            )

    def lookup_written(self, file_name: str, duration_s: float | None = None,
                       tolerance: float = 0.5) -> dict | None:
        """Find the most recent written row matching filename and duration."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM queue WHERE file_name = ? AND status = 'written' "
                "ORDER BY updated_at DESC",
                (file_name,),
            ).fetchall()
        if not rows:
            return None
        if duration_s is None:
            return self._row_to_dict(rows[0])
        for r in rows:
            if r["duration_s"] is not None and abs(r["duration_s"] - duration_s) <= tolerance:
                return self._row_to_dict(r)
        return self._row_to_dict(rows[0])

    def list_written_filenames(self) -> set[str]:
        """Return the set of file_name values with status='written'."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT file_name FROM queue WHERE status = 'written'"
            ).fetchall()
        return {r["file_name"] for r in rows}

    def purge_written(self, older_than_seconds: float = 365 * 24 * 3600) -> int:
        """Delete written rows older than the cutoff. Returns rows deleted."""
        cutoff = time.time() - older_than_seconds
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM queue WHERE status = 'written' AND updated_at < ?",
                (cutoff,),
            )
            return cur.rowcount or 0

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        try:
            d["metadata"] = json.loads(d["metadata"])
        except (TypeError, ValueError):
            d["metadata"] = {}
        return d
