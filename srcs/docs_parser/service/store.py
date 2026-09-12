"""Job state on disk, so a restart does not lose it.

SQLite on the model volume. Results survive a restart, and a client polling a
job it submitted before the restart gets an answer instead of a 404.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

JobStatus = Literal["queued", "running", "done", "error"]

DB_PATH = Path(os.environ.get("DOCPARSER_DB", "/models/jobs.db"))
UPLOAD_DIR = Path(os.environ.get("DOCPARSER_UPLOADS", "/models/uploads"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    status      TEXT NOT NULL,
    pdf_path    TEXT,
    max_pages   INTEGER,
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL,
    error       TEXT,
    result      TEXT
);
CREATE INDEX IF NOT EXISTS jobs_finished ON jobs (finished_at);
"""


@dataclass
class Job:
    id: str
    filename: str
    pdf_path: str
    max_pages: int | None = None
    status: JobStatus = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

    @property
    def duration(self) -> float | None:
        if self.started_at is None:
            return None
        return round((self.finished_at or time.time()) - self.started_at, 1)


class JobStore:
    """Every job, on disk. Small enough that plain synchronous SQLite is fine."""

    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")  # a reader never blocks the writer
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def save(self, job: Job) -> None:
        self._db.execute(
            """
            INSERT OR REPLACE INTO jobs (
                id, filename, status, pdf_path, max_pages, created_at,
                started_at, finished_at, error, result
            ) VALUES (
                :id, :filename, :status, :pdf_path, :max_pages, :created_at,
                :started_at, :finished_at, :error, :result
            )
            """,
            {
                "id": job.id,
                "filename": job.filename,
                "status": job.status,
                "pdf_path": job.pdf_path,
                "max_pages": job.max_pages,
                "created_at": job.created_at,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "error": job.error,
                "result": json.dumps(job.result) if job.result else None,
            },
        )
        self._db.commit()

    def get(self, job_id: str) -> Job | None:
        row = self._db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._to_job(row) if row else None

    def recover(self) -> list[Job]:
        """Clean up after a restart. Returns the jobs worth running again.

        A job that was still `queued` never started, and its PDF is on the
        volume, so it is handed back to be re-queued. A job that was `running`
        is failed instead of retried: it may be exactly the document that
        brought the worker down, and retrying it on every boot would be a loop.
        """
        now = time.time()
        interrupted = self._db.execute(
            "SELECT pdf_path FROM jobs WHERE status='running'"
        ).fetchall()
        self._db.execute(
            "UPDATE jobs SET status='error', finished_at=?,"
            " error='interrupted by a server restart' WHERE status='running'",
            (now,),
        )
        self._db.commit()
        for row in interrupted:  # their uploads are not needed again
            if row["pdf_path"]:
                Path(row["pdf_path"]).unlink(missing_ok=True)

        rows = self._db.execute("SELECT * FROM jobs WHERE status='queued'").fetchall()
        jobs = []
        for row in rows:
            job = self._to_job(row)
            if Path(job.pdf_path).exists():
                jobs.append(job)
            else:  # upload lost: nothing to run
                job.status = "error"
                job.error = "upload was lost in a restart"
                job.finished_at = now
                self.save(job)
        return jobs

    def prune(self, max_jobs: int, ttl: float) -> None:
        """Drop old finished jobs, and any upload files they left behind."""
        cutoff = time.time() - ttl
        stale = self._db.execute(
            "SELECT id, pdf_path FROM jobs WHERE finished_at IS NOT NULL AND"
            " (finished_at < ? OR id NOT IN"
            "  (SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?))",
            (cutoff, max_jobs),
        ).fetchall()

        for row in stale:
            if row["pdf_path"]:
                Path(row["pdf_path"]).unlink(missing_ok=True)
            self._db.execute("DELETE FROM jobs WHERE id = ?", (row["id"],))
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    @staticmethod
    def _to_job(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            filename=row["filename"],
            pdf_path=row["pdf_path"],
            max_pages=row["max_pages"],
            status=row["status"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            error=row["error"],
            result=json.loads(row["result"]) if row["result"] else None,
        )
