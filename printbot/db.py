from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cups_job_id TEXT NOT NULL,
    user_id     INTEGER NOT NULL,
    user_name   TEXT NOT NULL,
    channel_id  INTEGER,
    file_name   TEXT NOT NULL,
    pages       INTEGER NOT NULL,
    copies      INTEGER NOT NULL,
    sheets      INTEGER NOT NULL,
    color       INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',  -- queued / done / canceled / failed
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS jobs_user_day ON jobs (user_id, created_at);
CREATE INDEX IF NOT EXISTS jobs_cups ON jobs (cups_job_id);
"""


@dataclass(frozen=True)
class JobRecord:
    cups_job_id: str
    user_id: int
    channel_id: int | None
    file_name: str
    sheets: int


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def add_job(self, *, cups_job_id: str, user_id: int, user_name: str, channel_id: int | None,
                file_name: str, pages: int, copies: int, color: bool) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO jobs (cups_job_id, user_id, user_name, channel_id, file_name,"
                " pages, copies, sheets, color) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cups_job_id, user_id, user_name, channel_id, file_name,
                 pages, copies, pages * copies, int(color)),
            )

    def set_status(self, cups_job_id: str, status: str) -> bool:
        """Move a queued job to a final status. Returns False if it was already final."""
        with self._conn:
            cur = self._conn.execute(
                "UPDATE jobs SET status = ? WHERE cups_job_id = ? AND status = 'queued'",
                (status, cups_job_id),
            )
        return cur.rowcount > 0

    def sheets_today(self, user_id: int) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(sheets), 0) FROM jobs WHERE user_id = ?"
            " AND date(created_at) = date('now', 'localtime') AND status NOT IN ('canceled', 'failed')",
            (user_id,),
        ).fetchone()
        return int(row[0])

    def get(self, cups_job_id: str) -> JobRecord | None:
        row = self._conn.execute(
            "SELECT cups_job_id, user_id, channel_id, file_name, sheets FROM jobs"
            " WHERE cups_job_id = ? ORDER BY id DESC LIMIT 1",
            (cups_job_id,),
        ).fetchone()
        return JobRecord(*row) if row else None

    def queued(self) -> list[JobRecord]:
        rows = self._conn.execute(
            "SELECT cups_job_id, user_id, channel_id, file_name, sheets FROM jobs"
            " WHERE status = 'queued'"
        ).fetchall()
        return [JobRecord(*r) for r in rows]
