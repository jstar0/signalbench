"""Persistent local analysis jobs with real batch-boundary control."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from queue import Queue
from typing import Any

from ..classify import ClassificationCancelled, ClassificationPaused
from ..pipeline import classify_mapped_pipeline


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    job_id: str
    status: str
    revision: int
    done: int
    total: int
    error: str | None


class JobStore:
    """SQLite source of truth for local job state and replayable events."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY, prepared_path TEXT NOT NULL,
                    output_dir TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL,
                    done INTEGER NOT NULL, total INTEGER NOT NULL, error TEXT,
                    eligible_ids_json TEXT, batch_id TEXT, review_snapshot_json TEXT
                );
                CREATE TABLE IF NOT EXISTS job_events (
                    job_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL, PRIMARY KEY(job_id, seq)
                );
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
            if "eligible_ids_json" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN eligible_ids_json TEXT")
            if "batch_id" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN batch_id TEXT")
            if "review_snapshot_json" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN review_snapshot_json TEXT")
            interrupted = connection.execute(
                "SELECT job_id, done, total FROM jobs WHERE status IN ('queued','running','pausing','cancelling')"
            ).fetchall()
            for row in interrupted:
                connection.execute(
                    "UPDATE jobs SET status='interrupted', revision=revision+1 WHERE job_id=?",
                    (row["job_id"],),
                )
                self._event(
                    connection,
                    row["job_id"],
                    "interrupted",
                    {"done": row["done"], "total": row["total"], "error": "worker_restarted"},
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def create(
        self,
        prepared_path: Path,
        output_dir: Path,
        total: int,
        eligible_record_ids: set[str] | None = None,
        batch_id: str | None = None,
        review_snapshot: list[dict[str, Any]] | None = None,
    ) -> JobSnapshot:
        job_id = f"job_{uuid.uuid4().hex}"
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO jobs(
                    job_id, prepared_path, output_dir, status, revision, done, total, error,
                    eligible_ids_json, batch_id, review_snapshot_json
                ) VALUES (?, ?, ?, 'queued', 1, 0, ?, NULL, ?, ?, ?)""",
                (
                    job_id,
                    str(prepared_path),
                    str(output_dir),
                    total,
                    None if eligible_record_ids is None else json.dumps(sorted(eligible_record_ids)),
                    batch_id,
                    None if review_snapshot is None else json.dumps(review_snapshot, ensure_ascii=False, sort_keys=True),
                ),
            )
            self._event(connection, job_id, "queued", {"done": 0, "total": total})
        return self.get(job_id)

    def get(self, job_id: str) -> JobSnapshot:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return JobSnapshot(row["job_id"], row["status"], row["revision"], row["done"], row["total"], row["error"])

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT job_id, batch_id, status, revision, done, total, error FROM jobs ORDER BY rowid DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def update(self, job_id: str, status: str, *, done: int | None = None, error: str | None = None) -> JobSnapshot:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            new_done = row["done"] if done is None else done
            revision = row["revision"] + 1
            connection.execute(
                "UPDATE jobs SET status=?, revision=?, done=?, error=? WHERE job_id=?",
                (status, revision, new_done, error, job_id),
            )
            self._event(connection, job_id, status, {"done": new_done, "total": row["total"], "error": error})
        return self.get(job_id)

    def events(self, job_id: str, after: int = 0) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT seq, kind, payload_json FROM job_events WHERE job_id=? AND seq>? ORDER BY seq", (job_id, after)
            ).fetchall()
        return [{"seq": row["seq"], "kind": row["kind"], "payload": json.loads(row["payload_json"])} for row in rows]

    def eligible_ids(self, job_id: str) -> set[str] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT eligible_ids_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return None if row["eligible_ids_json"] is None else set(json.loads(row["eligible_ids_json"]))

    def review_snapshot(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT review_snapshot_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return [] if row["review_snapshot_json"] is None else list(json.loads(row["review_snapshot_json"]))

    def batch_id(self, job_id: str) -> str:
        with self._connect() as connection:
            row = connection.execute("SELECT batch_id FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None or row["batch_id"] is None:
            raise KeyError(job_id)
        return str(row["batch_id"])

    def replace_review_snapshot(self, job_id: str, reviews: list[dict[str, Any]]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT status, done, total FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            connection.execute(
                "UPDATE jobs SET review_snapshot_json=?, revision=revision+1 WHERE job_id=?",
                (json.dumps(reviews, ensure_ascii=False, sort_keys=True), job_id),
            )
            self._event(
                connection,
                job_id,
                "review_updated",
                {"done": row["done"], "total": row["total"], "error": None},
            )

    @staticmethod
    def _event(connection: sqlite3.Connection, job_id: str, kind: str, payload: dict[str, Any]) -> None:
        seq = connection.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM job_events WHERE job_id=?", (job_id,)).fetchone()[0]
        connection.execute("INSERT INTO job_events VALUES (?, ?, ?, ?)", (job_id, seq, kind, json.dumps(payload, sort_keys=True)))


class AnalysisJobs:
    """One local worker; pause and cancellation are checked after actual batches."""

    def __init__(self, store: JobStore) -> None:
        self.store = store
        self._queue: Queue[str | None] = Queue(maxsize=5)
        self._worker = threading.Thread(target=self._work, daemon=True)
        self._worker.start()

    def submit(
        self,
        prepared_path: Path,
        output_dir: Path,
        total: int,
        eligible_record_ids: set[str] | None = None,
        batch_id: str | None = None,
        review_snapshot: list[dict[str, Any]] | None = None,
    ) -> JobSnapshot:
        snapshot = self.store.create(
            prepared_path,
            output_dir,
            total,
            eligible_record_ids,
            batch_id,
            review_snapshot,
        )
        self._queue.put_nowait(snapshot.job_id)
        return snapshot

    def request_pause(self, job_id: str) -> JobSnapshot:
        snapshot = self.store.get(job_id)
        if snapshot.status != "running":
            raise ValueError("Only a running job can be paused.")
        return self.store.update(job_id, "pausing")

    def request_cancel(self, job_id: str) -> JobSnapshot:
        snapshot = self.store.get(job_id)
        if snapshot.status not in {"queued", "running", "pausing", "paused"}:
            raise ValueError("Only an active job can be cancelled.")
        return self.store.update(job_id, "cancelling")

    def resume(self, job_id: str) -> JobSnapshot:
        snapshot = self.store.get(job_id)
        if snapshot.status not in {"paused", "interrupted"}:
            raise ValueError("Only a paused or interrupted job can be resumed.")
        updated = self.store.update(job_id, "queued")
        self._queue.put_nowait(job_id)
        return updated

    def _work(self) -> None:
        while True:
            job_id = self._queue.get()
            if job_id is None:
                return
            try:
                snapshot = self.store.get(job_id)
                if snapshot.status == "cancelling":
                    self.store.update(job_id, "cancelled")
                    continue
                if snapshot.status != "queued":
                    continue
                self.store.update(job_id, "running")
                self._run(job_id)
            finally:
                self._queue.task_done()

    def _run(self, job_id: str) -> None:
        snapshot = self.store.get(job_id)
        prepared = Path(self._path(job_id, "prepared_path"))
        output = Path(self._path(job_id, "output_dir"))
        eligible = self.store.eligible_ids(job_id)

        def after_batch(done: int, _total: int) -> str:
            current = self.store.get(job_id)
            if current.status == "cancelling":
                self.store.update(job_id, "cancelling", done=done)
                return "cancel"
            if current.status == "pausing":
                self.store.update(job_id, "pausing", done=done)
                return "pause"
            self.store.update(job_id, "running", done=done)
            return "continue"

        try:
            classify_mapped_pipeline(
                prepared_path=prepared,
                output_dir=output,
                batch_size=1,
                after_batch=after_batch,
                eligible_record_ids=eligible,
            )
        except ClassificationPaused:
            self.store.update(job_id, "paused")
        except ClassificationCancelled:
            self.store.update(job_id, "cancelled")
        except Exception as exc:  # noqa: BLE001 - a worker must persist any task failure.
            self.store.update(job_id, "failed", error=str(exc))
        else:
            # A runner can finish a batch between a user request and its next
            # callback.  Inspect durable state once more before claiming
            # success, so the request still has its documented outcome.
            terminal_request = self.store.get(job_id).status
            if terminal_request == "cancelling":
                self.store.update(job_id, "cancelled")
            elif terminal_request == "pausing":
                self.store.update(job_id, "paused")
            else:
                self.store.update(job_id, "succeeded", done=snapshot.total)

    def _path(self, job_id: str, column: str) -> str:
        with self.store._connect() as connection:
            row = connection.execute(f"SELECT {column} FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return str(row[column])

    def output_path(self, job_id: str) -> Path:
        """Return a job's server-owned workspace path for internal routes."""

        return Path(self._path(job_id, "output_dir"))
