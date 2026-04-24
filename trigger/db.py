"""SQLite-backed job queue for the UDB trigger service."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    payload      TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'queued',
    log_path     TEXT,
    exit_code    INTEGER,
    error        TEXT,
    created_at   REAL    NOT NULL,
    started_at   REAL,
    finished_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS watches (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    payload          TEXT    NOT NULL,   -- same shape as a DownloadRequest
    name             TEXT    NOT NULL,
    year             INTEGER,
    created_at       REAL    NOT NULL,
    last_scanned_at  REAL,
    UNIQUE(name, year)
);
CREATE INDEX IF NOT EXISTS idx_watches_last ON watches(last_scanned_at);
"""


class JobDB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        # reset any jobs that were mid-flight when the service died
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status='failed', error='interrupted' "
                "WHERE status='running'"
            )
            self._conn.commit()

    def enqueue(self, payload: dict[str, Any]) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO jobs (payload, status, created_at) VALUES (?, 'queued', ?)",
                (json.dumps(payload), time.time()),
            )
            self._conn.commit()
            return cur.lastrowid

    def claim_next(self) -> Optional[sqlite3.Row]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE jobs SET status='running', started_at=? WHERE id=?",
                (time.time(), row["id"]),
            )
            self._conn.commit()
            return self._conn.execute(
                "SELECT * FROM jobs WHERE id=?", (row["id"],)
            ).fetchone()

    def finish(self, job_id: int, exit_code: int, error: Optional[str] = None) -> None:
        status = "done" if exit_code == 0 else "failed"
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status=?, exit_code=?, error=?, finished_at=? WHERE id=?",
                (status, exit_code, error, time.time(), job_id),
            )
            self._conn.commit()

    def set_log_path(self, job_id: int, log_path: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET log_path=? WHERE id=?", (log_path, job_id)
            )
            self._conn.commit()

    def get(self, job_id: int) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def delete(self, job_id: int) -> Optional[dict[str, Any]]:
        """Delete one job row. Returns the deleted row (pre-delete) or None
        if it didn't exist. Refuses if the job is running — caller should
        cancel first."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            if row["status"] == "running":
                raise ValueError("cannot delete a running job; cancel first")
            self._conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            self._conn.commit()
            return self._row_to_dict(row)

    def cleanup_finished(self, keep_last: int = 0) -> list[dict[str, Any]]:
        """Delete all done/failed jobs except the most recent `keep_last`.
        Returns the deleted rows so callers can also clean up log files."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE status IN ('done','failed') "
                "ORDER BY id DESC"
            ).fetchall()
            to_delete = [r for r in rows[keep_last:]]
            if not to_delete:
                return []
            ids = [r["id"] for r in to_delete]
            placeholders = ",".join("?" * len(ids))
            self._conn.execute(
                f"DELETE FROM jobs WHERE id IN ({placeholders})", ids
            )
            self._conn.commit()
            return [self._row_to_dict(r) for r in to_delete]

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        try:
            d["payload"] = json.loads(d["payload"])
        except Exception:
            pass
        return d

    # --- watches ------------------------------------------------------------
    def add_watch(self, payload: dict[str, Any]) -> int:
        """Register a show for periodic re-downloads. Idempotent on (name, year)."""
        name = str(payload.get("name") or "").strip()
        year = payload.get("year")
        year_int = int(year) if year is not None else None
        if not name:
            raise ValueError("payload.name is required for watch")
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM watches WHERE name=? AND (year IS ? OR year=?)",
                (name, year_int, year_int),
            ).fetchone()
            if existing:
                # Refresh stored payload so changes (e.g. resolution) stick.
                self._conn.execute(
                    "UPDATE watches SET payload=? WHERE id=?",
                    (json.dumps(payload), existing["id"]),
                )
                self._conn.commit()
                return existing["id"]
            cur = self._conn.execute(
                "INSERT INTO watches (payload, name, year, created_at) VALUES (?, ?, ?, ?)",
                (json.dumps(payload), name, year_int, time.time()),
            )
            self._conn.commit()
            return cur.lastrowid

    def remove_watch(self, watch_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM watches WHERE id=?", (watch_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def list_watches(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM watches ORDER BY id DESC"
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def mark_watch_scanned(self, watch_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE watches SET last_scanned_at=? WHERE id=?",
                (time.time(), watch_id),
            )
            self._conn.commit()
