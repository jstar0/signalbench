"""SQLite-backed, append-only human review records for a prepared batch."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .models import PreparedRecord
from .quality import preparation_digest

_HARD_EXCLUSION_REASONS = frozenset({"blank", "encoding_marker", "control_character"})
_SENTIMENTS = frozenset({"positive", "neutral", "negative"})
_TOPICS = frozenset({"pricing", "reliability", "support", "safety", "general"})


class ReviewStoreError(ValueError):
    """A requested review transition cannot be applied."""


class ReviewConflict(ReviewStoreError):
    """The caller applied a review against an out-of-date review revision."""


@dataclass(frozen=True, slots=True)
class ReviewUpdate:
    record_id: str
    include_override: str | None = None
    sentiment_override: str | None = None
    topic_override: str | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class EffectiveReview:
    record_id: str
    review_revision: int
    include_override: str | None
    sentiment_override: str | None
    topic_override: str | None
    reason: str
    effective_include: bool


class ReviewStore:
    """Owns review history without mutating source rows or prepared.csv."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def review_revision(self, batch_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT review_revision FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
        if row is None:
            raise ReviewStoreError(f"Unknown batch {batch_id!r}")
        return int(row["review_revision"])

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS batches (
                    batch_id TEXT PRIMARY KEY,
                    batch_revision INTEGER NOT NULL,
                    preparation_digest TEXT NOT NULL,
                    review_revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS batch_records (
                    batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
                    record_id TEXT NOT NULL,
                    record_position INTEGER NOT NULL,
                    quality_label TEXT NOT NULL,
                    quality_reason TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (batch_id, record_id)
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    batch_id TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    batch_revision INTEGER NOT NULL,
                    review_revision INTEGER NOT NULL,
                    include_override TEXT,
                    sentiment_override TEXT,
                    topic_override TEXT,
                    reason TEXT NOT NULL,
                    PRIMARY KEY (batch_id, record_id, review_revision),
                    FOREIGN KEY (batch_id, record_id)
                        REFERENCES batch_records(batch_id, record_id) ON DELETE CASCADE
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(batch_records)")
            }
            if "record_position" not in columns:
                connection.execute(
                    "ALTER TABLE batch_records ADD COLUMN record_position INTEGER NOT NULL DEFAULT 0"
                )

    def create_batch(
        self,
        batch_id: str,
        records: Sequence[PreparedRecord],
        *,
        metadata_by_record: dict[str, dict[str, str | None]] | None = None,
        batch_revision: int = 1,
    ) -> str:
        if not batch_id.strip():
            raise ReviewStoreError("batch_id must not be empty")
        if not records:
            raise ReviewStoreError("A review batch needs at least one prepared record")
        if len({record.record_id for record in records}) != len(records):
            raise ReviewStoreError("Prepared records must have unique record IDs")
        if batch_revision < 1:
            raise ReviewStoreError("batch_revision must be at least 1")
        digest = preparation_digest(records)
        metadata_by_record = metadata_by_record or {}
        with self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO batches(batch_id, batch_revision, preparation_digest) VALUES (?, ?, ?)",
                    (batch_id, batch_revision, digest),
                )
            except sqlite3.IntegrityError as exc:
                raise ReviewStoreError(f"Batch {batch_id!r} already exists") from exc
            connection.executemany(
                """
                INSERT INTO batch_records(
                    batch_id, record_id, record_position, quality_label, quality_reason, record_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        batch_id,
                        record.record_id,
                        position,
                        record.quality_label,
                        record.quality_reason,
                        json.dumps(record.to_row(), ensure_ascii=False, sort_keys=True),
                        json.dumps(
                            metadata_by_record.get(record.record_id, {}),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    )
                    for position, record in enumerate(records)
                ],
            )
        return digest

    def apply(
        self,
        batch_id: str,
        updates: Iterable[ReviewUpdate],
        *,
        expected_review_revision: int,
    ) -> int:
        rows = tuple(updates)
        if not rows or len(rows) > 500:
            raise ReviewStoreError("A review update must contain 1 to 500 records")
        if len({item.record_id for item in rows}) != len(rows):
            raise ReviewStoreError("A record may appear only once in a review update")
        for item in rows:
            self._validate_update(item)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            batch = connection.execute(
                "SELECT batch_revision, review_revision FROM batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            if batch is None:
                raise ReviewStoreError(f"Unknown batch {batch_id!r}")
            if batch["review_revision"] != expected_review_revision:
                raise ReviewConflict(
                    f"Batch {batch_id!r} changed from review revision {expected_review_revision} "
                    f"to {batch['review_revision']}."
                )
            current_rows = {
                row["record_id"]: row
                for row in connection.execute(
                    """
                    SELECT record.record_id, record.quality_reason,
                           latest.include_override, latest.sentiment_override,
                           latest.topic_override, latest.reason
                    FROM batch_records AS record
                    LEFT JOIN reviews AS latest
                      ON latest.batch_id = record.batch_id AND latest.record_id = record.record_id
                     AND latest.review_revision = (
                        SELECT MAX(candidate.review_revision)
                        FROM reviews AS candidate
                        WHERE candidate.batch_id = record.batch_id AND candidate.record_id = record.record_id
                     )
                    WHERE record.batch_id = ? AND record.record_id IN ({})
                    """.format(",".join("?" for _ in rows)),
                    (batch_id, *(item.record_id for item in rows)),
                )
            }
            unknown = sorted({item.record_id for item in rows} - set(current_rows))
            if unknown:
                raise ReviewStoreError(f"Unknown record(s): {', '.join(unknown)}")
            merged_rows = []
            for item in rows:
                current = current_rows[item.record_id]
                include_override = (
                    item.include_override
                    if item.include_override is not None
                    else current["include_override"]
                )
                if (
                    include_override == "include"
                    and current["quality_reason"] in _HARD_EXCLUSION_REASONS
                ):
                    raise ReviewStoreError(
                        f"{item.record_id} is a hard quality exclusion and cannot be included."
                    )
                merged_rows.append(
                    (
                        item.record_id,
                        include_override,
                        item.sentiment_override
                        if item.sentiment_override is not None
                        else current["sentiment_override"],
                        item.topic_override
                        if item.topic_override is not None
                        else current["topic_override"],
                        item.reason.strip() or current["reason"] or "",
                    )
                )
            next_revision = int(batch["review_revision"]) + 1
            connection.executemany(
                """
                INSERT INTO reviews(
                    batch_id, record_id, batch_revision, review_revision,
                    include_override, sentiment_override, topic_override, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        batch_id,
                        record_id,
                        batch["batch_revision"],
                        next_revision,
                        include_override,
                        sentiment_override,
                        topic_override,
                        reason,
                    )
                    for record_id, include_override, sentiment_override, topic_override, reason in merged_rows
                ],
            )
            connection.execute(
                "UPDATE batches SET review_revision = ? WHERE batch_id = ?",
                (next_revision, batch_id),
            )
        return next_revision

    def keep_first_duplicates(
        self, batch_id: str, *, expected_review_revision: int
    ) -> tuple[int, int]:
        """Exclude every currently included duplicate after the first, in one revision."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            batch = connection.execute(
                "SELECT batch_revision, review_revision FROM batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            if batch is None:
                raise ReviewStoreError(f"Unknown batch {batch_id!r}")
            current_revision = int(batch["review_revision"])
            if current_revision != expected_review_revision:
                raise ReviewConflict(
                    f"Batch {batch_id!r} changed from review revision {expected_review_revision} "
                    f"to {current_revision}."
                )
            records = connection.execute(
                """
                SELECT row.record_id, row.quality_label, row.quality_reason, row.record_json,
                       latest.include_override, latest.sentiment_override,
                       latest.topic_override
                FROM batch_records AS row
                LEFT JOIN reviews AS latest
                  ON latest.batch_id = row.batch_id AND latest.record_id = row.record_id
                 AND latest.review_revision = (
                    SELECT MAX(candidate.review_revision)
                    FROM reviews AS candidate
                    WHERE candidate.batch_id = row.batch_id AND candidate.record_id = row.record_id
                 )
                WHERE row.batch_id = ?
                ORDER BY row.record_position
                """,
                (batch_id,),
            ).fetchall()
            targets = []
            for row in records:
                record = json.loads(row["record_json"])
                if (
                    int(record.get("duplicate_count", 1)) <= 1
                    or int(record.get("duplicate_rank", 1)) <= 1
                ):
                    continue
                if row["quality_reason"] in _HARD_EXCLUSION_REASONS:
                    continue
                if row["include_override"] == "exclude":
                    continue
                if (
                    row["include_override"] != "include"
                    and row["quality_label"] != "valid"
                ):
                    continue
                targets.append(row)
            if not targets:
                return current_revision, 0
            next_revision = current_revision + 1
            connection.executemany(
                """
                INSERT INTO reviews(
                    batch_id, record_id, batch_revision, review_revision,
                    include_override, sentiment_override, topic_override, reason
                ) VALUES (?, ?, ?, ?, 'exclude', ?, ?, ?)
                """,
                [
                    (
                        batch_id,
                        row["record_id"],
                        batch["batch_revision"],
                        next_revision,
                        row["sentiment_override"],
                        row["topic_override"],
                        "重复组批量操作：保留首次出现",
                    )
                    for row in targets
                ],
            )
            connection.execute(
                "UPDATE batches SET review_revision = ? WHERE batch_id = ?",
                (next_revision, batch_id),
            )
        return next_revision, len(targets)

    @staticmethod
    def _validate_update(update: ReviewUpdate) -> None:
        if not update.record_id:
            raise ReviewStoreError("record_id must not be empty")
        if update.include_override not in {None, "include", "exclude"}:
            raise ReviewStoreError("include_override must be include, exclude, or null")
        if update.sentiment_override not in {None, *_SENTIMENTS}:
            raise ReviewStoreError("sentiment_override is unsupported")
        if update.topic_override not in {None, *_TOPICS}:
            raise ReviewStoreError("topic_override is unsupported")
        if len(update.reason.strip()) > 2_000:
            raise ReviewStoreError("reason exceeds the 2000-character v1 limit")
        if (
            update.include_override
            or update.sentiment_override
            or update.topic_override
        ) and not update.reason.strip():
            raise ReviewStoreError(
                "A review reason is required when overriding a record"
            )

    def effective_reviews(self, batch_id: str) -> tuple[EffectiveReview, ...]:
        with self._connect() as connection:
            batch = connection.execute(
                "SELECT review_revision FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise ReviewStoreError(f"Unknown batch {batch_id!r}")
            rows = connection.execute(
                """
                SELECT r.record_id, r.quality_label, r.quality_reason, latest.review_revision,
                       latest.include_override, latest.sentiment_override,
                       latest.topic_override, latest.reason
                FROM batch_records AS r
                LEFT JOIN reviews AS latest
                  ON latest.batch_id = r.batch_id AND latest.record_id = r.record_id
                 AND latest.review_revision = (
                    SELECT MAX(candidate.review_revision)
                    FROM reviews AS candidate
                    WHERE candidate.batch_id = r.batch_id AND candidate.record_id = r.record_id
                 )
                WHERE r.batch_id = ?
                ORDER BY r.record_position
                """,
                (batch_id,),
            ).fetchall()
        result = []
        for row in rows:
            hard = row["quality_reason"] in _HARD_EXCLUSION_REASONS
            override = row["include_override"]
            if hard:
                effective_include = False
            elif override == "include":
                effective_include = True
            elif override == "exclude":
                effective_include = False
            else:
                # `valid_and_reviewable` means that soft quality exclusions
                # stay visible for a reviewer but do not silently enter a run.
                effective_include = row["quality_label"] == "valid"
            result.append(
                EffectiveReview(
                    record_id=row["record_id"],
                    review_revision=int(row["review_revision"] or 0),
                    include_override=override,
                    sentiment_override=row["sentiment_override"],
                    topic_override=row["topic_override"],
                    reason=row["reason"] or "",
                    effective_include=effective_include,
                )
            )
        return tuple(result)
