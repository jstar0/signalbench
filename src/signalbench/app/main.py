"""Local SignalBench API: asset-bound imports, mapped batches, and jobs."""

from __future__ import annotations

import asyncio
import base64
import binascii
import csv
import io as string_io
import json
import sqlite3
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..adapters.datasentinel import compare_structure
from ..adapters.imports import SheetMapping, inspect_inputs
from ..classify import classification_summary
from ..io import (
    MAX_INPUT_BYTES,
    MAX_RECORDS,
    InputContractError,
    read_prepared_csv,
    write_rows_xlsx,
)
from ..models import SourceRecord
from ..pipeline import prepare_mapped_pipeline
from ..quality import quality_summary
from ..report import render_html_report
from ..review_store import (
    EffectiveReview,
    ReviewConflict,
    ReviewStore,
    ReviewStoreError,
    ReviewUpdate,
)
from .jobs import AnalysisJobs, JobSnapshot, JobStore


class MappingPayload(BaseModel):
    asset_id: str
    source_sha256: str
    sheet: str
    columns: dict[str, str | None]
    date_format: str | None = None


class MappingRequest(BaseModel):
    sheets: list[MappingPayload] = Field(min_length=1)


class CreateBatchRequest(BaseModel):
    import_id: str
    revision: int = Field(ge=1)


class CreateRunRequest(BaseModel):
    batch_id: str


class ReviewPayload(BaseModel):
    record_id: str
    include_override: str | None = None
    sentiment_override: str | None = None
    topic_override: str | None = None
    reason: str = ""


class ReviewRequest(BaseModel):
    updates: list[ReviewPayload] = Field(min_length=1, max_length=500)


class RunReviewPayload(BaseModel):
    record_id: str
    sentiment_override: str | None = None
    topic_override: str | None = None
    reason: str


class RunReviewRequest(BaseModel):
    updates: list[RunReviewPayload] = Field(min_length=1, max_length=500)


class ExportRequest(BaseModel):
    format: str
    scope: str
    sentiment: str | None = None
    topic: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    search: str | None = Field(default=None, max_length=500)
    review_status: str | None = None
    sort: str = "source"
    order: str = "asc"


class ComparisonRequest(BaseModel):
    current_run: str
    reference_run: str


def _csv_safe(value: object) -> object:
    """Prevent spreadsheet formula execution without changing JSON exports."""

    if isinstance(value, str) and value[:1] in {"=", "+", "-", "@"}:
        return "'" + value
    return value


def _encode_cursor(offset: int) -> str:
    return (
        base64.urlsafe_b64encode(f"v1:{offset}".encode("ascii"))
        .decode("ascii")
        .rstrip("=")
    )


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode("ascii")
        version, value = decoded.split(":", maxsplit=1)
        offset = int(value)
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError("cursor is invalid") from exc
    if version != "v1" or offset < 0 or offset > MAX_RECORDS:
        raise ValueError("cursor is outside the supported range")
    return offset


def _page(
    rows: list[dict[str, Any]], *, cursor: str | None, limit: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    total = len(rows)
    offset = _decode_cursor(cursor)
    if total and offset >= total:
        offset = ((total - 1) // limit) * limit
    elif not total:
        offset = 0
    end = min(offset + limit, total)
    return rows[offset:end], {
        "filtered_total": total,
        "offset": offset,
        "limit": limit,
        "page_number": offset // limit + 1,
        "page_count": max(1, (total + limit - 1) // limit),
        "cursor": _encode_cursor(offset) if offset else None,
        "previous_cursor": _encode_cursor(max(0, offset - limit)) if offset else None,
        "next_cursor": _encode_cursor(offset + limit) if end < total else None,
    }


class WorkspaceStore:
    """The app's local registry; browser clients never submit filesystem paths."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.assets_dir = root / "assets"
        self.workspaces_dir = root / "workspaces"
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)
        self.path = root / "state.sqlite"
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS assets (
                    asset_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, path TEXT NOT NULL,
                    sha256 TEXT NOT NULL, byte_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS imports (
                    import_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, asset_ids_json TEXT NOT NULL,
                    inspection_json TEXT NOT NULL, mapping_json TEXT
                );
                CREATE TABLE IF NOT EXISTS batches (
                    batch_id TEXT PRIMARY KEY, import_id TEXT NOT NULL, import_revision INTEGER NOT NULL,
                    workspace_path TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, path TEXT NOT NULL,
                    filename TEXT NOT NULL, media_type TEXT NOT NULL, scope TEXT NOT NULL, format TEXT NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def add_asset(self, display_name: str, content: bytes) -> dict[str, Any]:
        safe_name = Path(display_name or "upload").name
        suffix = Path(safe_name).suffix.lower()
        if suffix not in {".csv", ".tsv", ".xlsx"}:
            raise InputContractError("Unsupported input format; use CSV, TSV, or XLSX.")
        if len(content) > MAX_INPUT_BYTES:
            raise InputContractError(
                f"{safe_name} exceeds the {MAX_INPUT_BYTES}-byte v1 limit."
            )
        asset_id = f"asset_{uuid.uuid4().hex}"
        destination = self.assets_dir / asset_id / safe_name
        destination.parent.mkdir(parents=True)
        destination.write_bytes(content)
        from ..io import sha256_file

        digest = sha256_file(destination)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO assets VALUES (?, ?, ?, ?, ?)",
                (asset_id, safe_name, str(destination), digest, len(content)),
            )
        return {
            "asset_id": asset_id,
            "display_name": safe_name,
            "sha256": digest,
            "byte_count": len(content),
        }

    def asset_paths(self, asset_ids: Sequence[str]) -> list[Path]:
        if not asset_ids or len(set(asset_ids)) != len(asset_ids):
            raise InputContractError("Each import asset must be selected once.")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT asset_id, path FROM assets WHERE asset_id IN ({})".format(
                    ",".join("?" for _ in asset_ids)
                ),
                tuple(asset_ids),
            ).fetchall()
        by_id = {row["asset_id"]: Path(row["path"]) for row in rows}
        missing = [asset_id for asset_id in asset_ids if asset_id not in by_id]
        if missing:
            raise KeyError(", ".join(missing))
        return [by_id[asset_id] for asset_id in asset_ids]

    def create_import(
        self, assets: list[dict[str, Any]], inspection: list[dict[str, Any]]
    ) -> str:
        import_id = f"import_{uuid.uuid4().hex}"
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO imports VALUES (?, 1, ?, ?, NULL)",
                (
                    import_id,
                    json.dumps([asset["asset_id"] for asset in assets]),
                    json.dumps(inspection, ensure_ascii=False),
                ),
            )
        return import_id

    def get_import(self, import_id: str) -> sqlite3.Row:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM imports WHERE import_id=?", (import_id,)
            ).fetchone()
        if row is None:
            raise KeyError(import_id)
        return row

    def update_mapping(
        self, import_id: str, revision: int, mapping: list[dict[str, Any]]
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE imports SET revision=revision+1, mapping_json=? WHERE import_id=? AND revision=?",
                (json.dumps(mapping, ensure_ascii=False), import_id, revision),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("revision_conflict")
        return revision + 1

    def create_batch(self, import_id: str, revision: int) -> tuple[str, Path]:
        row = self.get_import(import_id)
        if row["revision"] != revision:
            raise RuntimeError("revision_conflict")
        if not row["mapping_json"]:
            raise InputContractError(
                "Choose and map at least one sheet before creating a batch."
            )
        batch_id = f"batch_{uuid.uuid4().hex}"
        workspace = self.workspaces_dir / batch_id
        workspace.mkdir()
        mappings = [SheetMapping(**item) for item in json.loads(row["mapping_json"])]
        prepare_mapped_pipeline(
            self.asset_paths(json.loads(row["asset_ids_json"])),
            mappings,
            output_dir=workspace,
            batch_id=batch_id,
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO batches VALUES (?, ?, ?, ?)",
                (batch_id, import_id, revision, str(workspace)),
            )
        return batch_id, workspace

    def batch_path(self, batch_id: str) -> Path:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT workspace_path FROM batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
        if row is None:
            raise KeyError(batch_id)
        return Path(row["workspace_path"])

    def register_artifact(
        self,
        job_id: str,
        path: Path,
        *,
        filename: str,
        media_type: str,
        scope: str,
        format: str,
    ) -> str:
        artifact_id = f"artifact_{uuid.uuid4().hex}"
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?)",
                (artifact_id, job_id, str(path), filename, media_type, scope, format),
            )
        return artifact_id

    def artifact(self, artifact_id: str) -> sqlite3.Row:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
        if row is None:
            raise KeyError(artifact_id)
        return row


def _snapshot_payload(snapshot: JobSnapshot) -> dict[str, Any]:
    return {
        "job_id": snapshot.job_id,
        "status": snapshot.status,
        "revision": snapshot.revision,
        "capabilities": {
            "pause": snapshot.status == "running",
            "resume": snapshot.status in {"paused", "interrupted"},
        },
        "progress": {"done": snapshot.done, "total": snapshot.total, "unit": "records"},
        "error": snapshot.error,
    }


def _reviewed_records(
    records: list[Any], reviews: Sequence[EffectiveReview]
) -> list[Any]:
    """Apply independent review decisions to an in-memory result view only."""

    by_id = {review.record_id: review for review in reviews}
    for record in records:
        review = by_id.get(record.record_id)
        if review is None:
            continue
        if not review.effective_include:
            record.classification_status = "not_applicable"
            continue
        if record.classification_status == "classified":
            if review.sentiment_override is not None:
                record.sentiment_label = review.sentiment_override
            if review.topic_override is not None:
                record.topic = review.topic_override
    return records


def create_app(runtime_root: Path | None = None) -> FastAPI:
    root = runtime_root or Path("runtime/signalbench")
    store = WorkspaceStore(root)
    jobs = AnalysisJobs(JobStore(root / "jobs.sqlite"))
    app = FastAPI(title="SignalBench", version="0.1.0")

    def error(
        status: int, code: str, message: str, *, retryable: bool = False
    ) -> HTTPException:
        return HTTPException(
            status,
            {"error": {"code": code, "message": message, "retryable": retryable}},
        )

    @app.post("/api/v1/imports", status_code=201)
    async def upload_import(
        files: Annotated[list[UploadFile], File(min_length=1)],
    ) -> dict[str, Any]:
        assets: list[dict[str, Any]] = []
        inspection: list[dict[str, Any]] = []
        upload_errors: list[dict[str, str]] = []
        for item in files:
            display_name = item.filename or "upload"
            try:
                asset = store.add_asset(display_name, await item.read())
                tables = inspect_inputs(store.asset_paths([asset["asset_id"]])).tables
            except InputContractError as exc:
                upload_errors.append(
                    {"display_name": Path(display_name).name, "message": str(exc)}
                )
                continue
            assets.append(asset)
            inspection.extend(
                {
                    "asset_id": asset["asset_id"],
                    "source_file": table.source_file,
                    "source_sha256": table.source_sha256,
                    "sheet": table.sheet,
                    "columns": list(table.columns),
                    "preview_rows": list(table.preview_rows),
                }
                for table in tables
            )
        if not assets:
            message = (
                upload_errors[0]["message"]
                if upload_errors
                else "No usable input file was uploaded."
            )
            raise error(422, "INVALID_IMPORT", message)
        import_id = store.create_import(assets, inspection)
        return {
            "import_id": import_id,
            "revision": 1,
            "assets": assets,
            "sheets": inspection,
            "errors": upload_errors,
        }

    @app.patch("/api/v1/imports/{import_id}/mapping")
    def map_import(
        import_id: str,
        request: MappingRequest,
        if_match: Annotated[int, Header(alias="If-Match")],
    ) -> dict[str, Any]:
        try:
            imported = store.get_import(import_id)
            owned = set(json.loads(imported["asset_ids_json"]))
            if any(item.asset_id not in owned for item in request.sheets):
                raise error(
                    404,
                    "ASSET_NOT_IN_IMPORT",
                    "An asset does not belong to this import.",
                )
            mapping = [item.model_dump(exclude={"asset_id"}) for item in request.sheets]
            for item in mapping:
                SheetMapping(**item)
            revision = store.update_mapping(import_id, if_match, mapping)
        except KeyError as exc:
            raise error(
                404, "IMPORT_NOT_FOUND", f"Unknown import {import_id}."
            ) from exc
        except RuntimeError as exc:
            raise error(
                409,
                "REVISION_CONFLICT",
                "This import changed; reload before saving mapping.",
            ) from exc
        except (InputContractError, ValueError) as exc:
            raise error(422, "INVALID_MAPPING", str(exc)) from exc
        return {"import_id": import_id, "revision": revision}

    @app.post("/api/v1/batches", status_code=201)
    def create_batch(request: CreateBatchRequest) -> dict[str, Any]:
        try:
            batch_id, workspace = store.create_batch(
                request.import_id, request.revision
            )
            records = read_prepared_csv(workspace / "prepared.csv")
        except KeyError as exc:
            raise error(404, "IMPORT_NOT_FOUND", "Import does not exist.") from exc
        except RuntimeError as exc:
            raise error(
                409,
                "REVISION_CONFLICT",
                "Import changed; reload before creating a batch.",
            ) from exc
        except (InputContractError, ValueError) as exc:
            raise error(422, "INVALID_MAPPING", str(exc)) from exc
        return {
            "batch_id": batch_id,
            "revision": 1,
            "record_count": len(records),
            "workspace": "local",
        }

    @app.post("/api/v1/analysis-runs", status_code=202)
    def create_run(request: CreateRunRequest) -> dict[str, Any]:
        try:
            workspace = store.batch_path(request.batch_id)
            effective_reviews = ReviewStore(
                workspace / "reviews.sqlite"
            ).effective_reviews(request.batch_id)
            eligible = {
                review.record_id
                for review in effective_reviews
                if review.effective_include
            }
            total = len(eligible)
            if total == 0:
                raise error(
                    422,
                    "EMPTY_ANALYSIS_SCOPE",
                    "No valid records are available for analysis.",
                )
            run_output = workspace / "runs" / f"run_{uuid.uuid4().hex}"
            run_output.mkdir(parents=True)
            return _snapshot_payload(
                jobs.submit(
                    workspace / "prepared.csv",
                    run_output,
                    total,
                    eligible_record_ids=eligible,
                    batch_id=request.batch_id,
                    review_snapshot=[asdict(review) for review in effective_reviews],
                )
            )
        except KeyError as exc:
            raise error(404, "BATCH_NOT_FOUND", "Batch does not exist.") from exc

    @app.get("/api/v1/analysis-runs")
    def list_runs() -> dict[str, Any]:
        return {"runs": jobs.store.list()}

    @app.get("/api/v1/batches/{batch_id}/records")
    def batch_records(
        batch_id: str,
        quality: str | None = None,
        source: str | None = Query(default=None, max_length=200),
        search: str | None = Query(default=None, max_length=500),
        include: str | None = None,
        sort: str = "position",
        order: str = "asc",
        cursor: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        allowed_quality = {None, "all", "valid", "invalid", "display_issue"}
        allowed_include = {None, "included", "excluded", "pending"}
        allowed_sort = {"position", "source", "quality", "duplicate", "text", "include"}
        if quality not in allowed_quality:
            raise error(
                422, "INVALID_QUALITY_FILTER", "Choose a supported quality filter."
            )
        if include not in allowed_include:
            raise error(
                422, "INVALID_INCLUDE_FILTER", "Choose a supported inclusion filter."
            )
        if sort not in allowed_sort or order not in {"asc", "desc"}:
            raise error(
                422, "INVALID_SORT", "Choose a supported sort field and direction."
            )
        try:
            workspace = store.batch_path(batch_id)
            records = read_prepared_csv(workspace / "prepared.csv")
            review_store = ReviewStore(workspace / "reviews.sqlite")
            reviews = review_store.effective_reviews(batch_id)
            review_revision = review_store.review_revision(batch_id)
            metadata = {
                item["record_id"]: item
                for item in json.loads(
                    (workspace / "record_metadata.json").read_text(encoding="utf-8")
                )["records"]
            }
        except KeyError as exc:
            raise error(404, "BATCH_NOT_FOUND", "Batch does not exist.") from exc
        review_by_id = {review.record_id: review for review in reviews}
        included_total = sum(review.effective_include for review in reviews)
        pending_total = sum(
            record.quality_label != "valid"
            and review_by_id[record.record_id].include_override is None
            for record in records
        )
        search_text = search.strip().casefold() if search else ""
        source_text = source.strip().casefold() if source else ""
        result_rows = []
        for position, record in enumerate(records):
            review = review_by_id[record.record_id]
            if quality not in {None, "all"} and record.quality_label != quality:
                continue
            if include == "included" and not review.effective_include:
                continue
            if include == "excluded" and review.effective_include:
                continue
            if include == "pending" and (
                record.quality_label == "valid" or review.include_override is not None
            ):
                continue
            if (
                source_text
                and source_text
                not in f"{record.platform} {record.source_sheet}".casefold()
            ):
                continue
            if (
                search_text
                and search_text
                not in f"{record.text_raw} {record.platform} {record.source_sheet}".casefold()
            ):
                continue
            result_rows.append(
                {
                    "_position": position,
                    "record_id": record.record_id,
                    "text_raw": record.text_raw,
                    "platform": record.platform,
                    "source_sheet": record.source_sheet,
                    "source_row": record.source_row,
                    "quality_label": record.quality_label,
                    "quality_reason": record.quality_reason,
                    "duplicate_group": record.duplicate_group,
                    "duplicate_rank": record.duplicate_rank,
                    "duplicate_count": record.duplicate_count,
                    "effective_include": review.effective_include,
                    "review_revision": review.review_revision,
                    "include_override": review.include_override,
                    "sentiment_override": review.sentiment_override,
                    "topic_override": review.topic_override,
                    "review_reason": review.reason,
                    "occurred_at": metadata.get(record.record_id, {}).get(
                        "occurred_at"
                    ),
                    "date_parse_status": metadata.get(record.record_id, {}).get(
                        "date_parse_status", "unmapped"
                    ),
                }
            )
        sort_keys = {
            "position": lambda row: row["_position"],
            "source": lambda row: (
                row["platform"].casefold(),
                row["source_sheet"].casefold(),
                row["source_row"],
            ),
            "quality": lambda row: (row["quality_label"], row["quality_reason"]),
            "duplicate": lambda row: (row["duplicate_group"], row["duplicate_rank"]),
            "text": lambda row: row["text_raw"].casefold(),
            "include": lambda row: not row["effective_include"],
        }
        result_rows.sort(
            key=lambda row: (sort_keys[sort](row), row["record_id"].casefold()),
            reverse=order == "desc",
        )
        try:
            page_rows, pagination = _page(result_rows, cursor=cursor, limit=limit)
        except ValueError as exc:
            raise error(422, "INVALID_CURSOR", str(exc)) from exc
        for row in page_rows:
            row.pop("_position", None)
        return {
            "batch_id": batch_id,
            "rows": page_rows,
            "batch_total": len(records),
            "included_total": included_total,
            "excluded_total": len(records) - included_total,
            "pending_total": pending_total,
            "review_revision": review_revision,
            **pagination,
        }

    @app.patch("/api/v1/batches/{batch_id}/reviews")
    def review_records(
        batch_id: str,
        request: ReviewRequest,
        if_match: Annotated[int, Header(alias="If-Match")],
    ) -> dict[str, Any]:
        try:
            workspace = store.batch_path(batch_id)
            revision = ReviewStore(workspace / "reviews.sqlite").apply(
                batch_id,
                [ReviewUpdate(**item.model_dump()) for item in request.updates],
                expected_review_revision=if_match,
            )
        except KeyError as exc:
            raise error(404, "BATCH_NOT_FOUND", "Batch does not exist.") from exc
        except ReviewConflict as exc:
            raise error(409, "REVIEW_CONFLICT", str(exc)) from exc
        except ReviewStoreError as exc:
            raise error(422, "INVALID_REVIEW", str(exc)) from exc
        return {"batch_id": batch_id, "review_revision": revision}

    @app.post("/api/v1/batches/{batch_id}/reviews/keep-first-duplicates")
    def keep_first_duplicates(
        batch_id: str,
        if_match: Annotated[int, Header(alias="If-Match")],
    ) -> dict[str, Any]:
        try:
            workspace = store.batch_path(batch_id)
            revision, updated_count = ReviewStore(
                workspace / "reviews.sqlite"
            ).keep_first_duplicates(batch_id, expected_review_revision=if_match)
        except KeyError as exc:
            raise error(404, "BATCH_NOT_FOUND", "Batch does not exist.") from exc
        except ReviewConflict as exc:
            raise error(409, "REVIEW_CONFLICT", str(exc)) from exc
        except ReviewStoreError as exc:
            raise error(422, "INVALID_REVIEW", str(exc)) from exc
        return {
            "batch_id": batch_id,
            "review_revision": revision,
            "updated_count": updated_count,
        }

    @app.get("/api/v1/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        try:
            return _snapshot_payload(jobs.store.get(job_id))
        except KeyError as exc:
            raise error(404, "JOB_NOT_FOUND", "Job does not exist.") from exc

    @app.get("/api/v1/analysis-runs/{job_id}/summary")
    def run_summary(job_id: str) -> dict[str, Any]:
        try:
            snapshot = jobs.store.get(job_id)
            classified = jobs.output_path(job_id) / "classified.csv"
        except KeyError as exc:
            raise error(404, "JOB_NOT_FOUND", "Job does not exist.") from exc
        if snapshot.status != "succeeded" or not classified.exists():
            raise error(409, "RESULT_NOT_READY", "Analysis results are not ready.")
        records = read_prepared_csv(classified)
        reviews = [
            EffectiveReview(**item) for item in jobs.store.review_snapshot(job_id)
        ]
        return {
            "job_id": job_id,
            "summary": classification_summary(_reviewed_records(records, reviews)),
        }

    @app.get("/api/v1/analysis-runs/{job_id}/records")
    def run_records(
        job_id: str,
        sentiment: str | None = None,
        topic: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        search: str | None = Query(default=None, max_length=500),
        review_status: str | None = None,
        sort: str = "source",
        order: str = "asc",
        cursor: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        if sentiment not in {None, "positive", "neutral", "negative"}:
            raise error(
                422, "INVALID_SENTIMENT_FILTER", "Choose a supported sentiment filter."
            )
        if topic not in {
            None,
            "pricing",
            "reliability",
            "support",
            "safety",
            "general",
        }:
            raise error(422, "INVALID_TOPIC_FILTER", "Choose a supported topic filter.")
        if review_status not in {None, "reviewed", "unreviewed"}:
            raise error(
                422, "INVALID_REVIEW_FILTER", "Choose reviewed or unreviewed results."
            )
        if date_from and date_to and date_from > date_to:
            raise error(
                422, "INVALID_DATE_RANGE", "date_from must be on or before date_to."
            )
        if sort not in {
            "source",
            "sentiment",
            "topic",
            "date",
            "text",
        } or order not in {"asc", "desc"}:
            raise error(
                422, "INVALID_SORT", "Choose a supported sort field and direction."
            )
        try:
            snapshot = jobs.store.get(job_id)
            classified = jobs.output_path(job_id) / "classified.csv"
            reviews = {
                item["record_id"]: EffectiveReview(**item)
                for item in jobs.store.review_snapshot(job_id)
            }
            batch_id = jobs.store.batch_id(job_id)
            review_revision = ReviewStore(
                store.batch_path(batch_id) / "reviews.sqlite"
            ).review_revision(batch_id)
            metadata_path = jobs.output_path(job_id).parents[1] / "record_metadata.json"
            metadata = {
                item["record_id"]: item
                for item in json.loads(metadata_path.read_text(encoding="utf-8"))[
                    "records"
                ]
            }
        except KeyError as exc:
            raise error(404, "JOB_NOT_FOUND", "Job does not exist.") from exc
        if snapshot.status != "succeeded" or not classified.exists():
            raise error(409, "RESULT_NOT_READY", "Analysis results are not ready.")
        rows = []
        unparsed_dates = 0
        search_text = search.strip().casefold() if search else ""
        for record in read_prepared_csv(classified):
            review = reviews.get(record.record_id)
            if review is not None and not review.effective_include:
                continue
            final_sentiment = (
                review.sentiment_override
                if review and review.sentiment_override
                else record.sentiment_label
            )
            final_topic = (
                review.topic_override
                if review and review.topic_override
                else record.topic
            )
            if record.classification_status != "classified":
                continue
            has_label_review = bool(
                review
                and (
                    review.sentiment_override is not None
                    or review.topic_override is not None
                )
            )
            if review_status == "reviewed" and not has_label_review:
                continue
            if review_status == "unreviewed" and has_label_review:
                continue
            if sentiment and final_sentiment != sentiment:
                continue
            if topic and final_topic != topic:
                continue
            date_metadata = metadata.get(record.record_id, {})
            occurred_at = date_metadata.get("occurred_at")
            parsed_date = str(occurred_at)[:10] if occurred_at else None
            if (
                search_text
                and search_text
                not in f"{record.text_raw} {record.platform} {record.source_sheet}".casefold()
            ):
                continue
            if date_metadata.get("date_parse_status") == "invalid_format":
                unparsed_dates += 1
            if date_from and (
                parsed_date is None or parsed_date < date_from.isoformat()
            ):
                continue
            if date_to and (parsed_date is None or parsed_date > date_to.isoformat()):
                continue
            rows.append(
                {
                    "record_id": record.record_id,
                    "text_raw": record.text_raw,
                    "platform": record.platform,
                    "source_sheet": record.source_sheet,
                    "source_row": record.source_row,
                    "algorithm_sentiment": record.sentiment_label,
                    "algorithm_topic": record.topic,
                    "sentiment": final_sentiment,
                    "topic": final_topic,
                    "decision_note": record.decision_note,
                    "rule_hits": list(record.rule_hits),
                    "review_reason": review.reason if review else "",
                    "review_revision": review.review_revision if review else 0,
                    "occurred_at": occurred_at,
                    "date_parse_status": date_metadata.get(
                        "date_parse_status", "unmapped"
                    ),
                }
            )
        sort_keys = {
            "source": lambda row: (
                row["platform"].casefold(),
                row["source_sheet"].casefold(),
                row["source_row"],
            ),
            "sentiment": lambda row: row["sentiment"],
            "topic": lambda row: row["topic"],
            "date": lambda row: (row["occurred_at"] is None, row["occurred_at"] or ""),
            "text": lambda row: row["text_raw"].casefold(),
        }
        rows.sort(
            key=lambda row: (sort_keys[sort](row), row["record_id"].casefold()),
            reverse=order == "desc",
        )
        try:
            page_rows, pagination = _page(rows, cursor=cursor, limit=limit)
        except ValueError as exc:
            raise error(422, "INVALID_CURSOR", str(exc)) from exc
        return {
            "job_id": job_id,
            "rows": page_rows,
            **pagination,
            "review_revision": review_revision,
            "unparsed_date_count": unparsed_dates,
            "date_filter_available": any(
                item.get("occurred_at") for item in metadata.values()
            ),
        }

    @app.patch("/api/v1/analysis-runs/{job_id}/reviews")
    def review_run_results(
        job_id: str,
        request: RunReviewRequest,
        if_match: Annotated[int, Header(alias="If-Match")],
    ) -> dict[str, Any]:
        try:
            snapshot = jobs.store.get(job_id)
            batch_id = jobs.store.batch_id(job_id)
            workspace = store.batch_path(batch_id)
        except KeyError as exc:
            raise error(404, "JOB_NOT_FOUND", "Analysis run does not exist.") from exc
        if snapshot.status != "succeeded":
            raise error(
                409, "RESULT_NOT_READY", "Only a completed analysis can be reviewed."
            )
        try:
            review_store = ReviewStore(workspace / "reviews.sqlite")
            revision = review_store.apply(
                batch_id,
                [ReviewUpdate(**item.model_dump()) for item in request.updates],
                expected_review_revision=if_match,
            )
            effective = review_store.effective_reviews(batch_id)
            jobs.store.replace_review_snapshot(
                job_id, [asdict(review) for review in effective]
            )
        except ReviewConflict as exc:
            raise error(409, "REVIEW_CONFLICT", str(exc)) from exc
        except ReviewStoreError as exc:
            raise error(422, "INVALID_REVIEW", str(exc)) from exc
        return {"job_id": job_id, "review_revision": revision}

    @app.post("/api/v1/analysis-runs/{job_id}/exports", status_code=201)
    def export_run(job_id: str, request: ExportRequest) -> dict[str, Any]:
        if request.scope not in {"summary", "detail"} or request.format not in {
            "html",
            "json",
            "csv",
            "xlsx",
        }:
            raise error(
                422,
                "INVALID_EXPORT",
                "Use scope summary/detail and format html/json/csv/xlsx.",
            )
        if request.scope == "detail" and request.format == "html":
            raise error(
                422, "INVALID_EXPORT", "Detail export supports JSON, CSV, or XLSX."
            )
        if request.sentiment not in {None, "positive", "neutral", "negative"}:
            raise error(
                422, "INVALID_SENTIMENT_FILTER", "Choose a supported sentiment filter."
            )
        if request.topic not in {
            None,
            "pricing",
            "reliability",
            "support",
            "safety",
            "general",
        }:
            raise error(422, "INVALID_TOPIC_FILTER", "Choose a supported topic filter.")
        if (
            request.date_from
            and request.date_to
            and request.date_from > request.date_to
        ):
            raise error(
                422, "INVALID_DATE_RANGE", "date_from must be on or before date_to."
            )
        if request.review_status not in {None, "reviewed", "unreviewed"}:
            raise error(
                422,
                "INVALID_REVIEW_FILTER",
                "Choose reviewed or unreviewed results.",
            )
        if request.sort not in {
            "source",
            "sentiment",
            "topic",
            "date",
            "text",
        } or request.order not in {"asc", "desc"}:
            raise error(
                422, "INVALID_SORT", "Choose a supported sort field and direction."
            )
        try:
            snapshot = jobs.store.get(job_id)
            output = jobs.output_path(job_id)
            classified_path = output / "classified.csv"
            reviews = [
                EffectiveReview(**item) for item in jobs.store.review_snapshot(job_id)
            ]
        except KeyError as exc:
            raise error(404, "JOB_NOT_FOUND", "Job does not exist.") from exc
        if snapshot.status != "succeeded" or not classified_path.exists():
            raise error(409, "RESULT_NOT_READY", "Analysis results are not ready.")
        records = read_prepared_csv(classified_path)
        reviewed = _reviewed_records(records, reviews)
        active_filter = any(
            (
                request.sentiment,
                request.topic,
                request.date_from,
                request.date_to,
                request.search,
                request.review_status,
            )
        )
        review_by_id = {review.record_id: review for review in reviews}
        metadata: dict[str, dict[str, Any]] = {}
        if request.date_from or request.date_to or request.sort == "date":
            metadata_path = output.parents[1] / "record_metadata.json"
            metadata = {
                item["record_id"]: item
                for item in json.loads(metadata_path.read_text(encoding="utf-8"))[
                    "records"
                ]
            }

        def matches_export_filter(record: Any) -> bool:
            if request.sentiment and record.sentiment_label != request.sentiment:
                return False
            if request.topic and record.topic != request.topic:
                return False
            if (
                request.search
                and request.search.strip().casefold()
                not in f"{record.text_raw} {record.platform} {record.source_sheet}".casefold()
            ):
                return False
            review = review_by_id.get(record.record_id)
            has_label_review = bool(
                review
                and (
                    review.sentiment_override is not None
                    or review.topic_override is not None
                )
            )
            if request.review_status == "reviewed" and not has_label_review:
                return False
            if request.review_status == "unreviewed" and has_label_review:
                return False
            if request.date_from or request.date_to:
                occurred_at = metadata.get(record.record_id, {}).get("occurred_at")
                parsed_date = str(occurred_at)[:10] if occurred_at else None
                if request.date_from and (
                    parsed_date is None or parsed_date < request.date_from.isoformat()
                ):
                    return False
                if request.date_to and (
                    parsed_date is None or parsed_date > request.date_to.isoformat()
                ):
                    return False
            return True

        filtered_reviewed = [
            record for record in reviewed if matches_export_filter(record)
        ]
        export_sort = {
            "source": lambda record: (
                record.platform.casefold(),
                record.source_sheet.casefold(),
                record.source_row,
            ),
            "sentiment": lambda record: record.sentiment_label,
            "topic": lambda record: record.topic,
            "date": lambda record: (
                metadata.get(record.record_id, {}).get("occurred_at") is None,
                metadata.get(record.record_id, {}).get("occurred_at") or "",
            ),
            "text": lambda record: record.text_raw.casefold(),
        }
        filtered_reviewed.sort(
            key=lambda record: (
                export_sort[request.sort](record),
                record.record_id.casefold(),
            ),
            reverse=request.order == "desc",
        )
        artifact_dir = output / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        suffix = request.format
        filtered_marker = "-filtered" if active_filter else ""
        filename = (
            f"signalbench-{request.scope}-{job_id[-8:]}{filtered_marker}.{suffix}"
        )
        path = artifact_dir / f"{token}.{suffix}"
        media_types = {
            "html": "text/html; charset=utf-8",
            "json": "application/json",
            "csv": "text/csv; charset=utf-8",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }
        if request.scope == "summary":
            quality = quality_summary(records)
            classification = classification_summary(filtered_reviewed)
            payload = {
                "schema_version": 1,
                "job_id": job_id,
                "filter": request.model_dump(
                    mode="json", exclude={"format", "scope"}, exclude_none=True
                ),
                "quality": quality,
                "classification": classification,
            }
            if request.format == "html":
                checkpoint_digest = json.loads(
                    (output / "checkpoint.json").read_text(encoding="utf-8")
                )["input_digest"]
                path.write_text(
                    render_html_report(
                        quality_rows=quality,
                        classification_rows=classification,
                        reasons={},
                        digest=checkpoint_digest,
                        classifier_name="deterministic-v1 + frozen reviews",
                    ),
                    encoding="utf-8",
                )
            elif request.format == "json":
                path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            else:
                rows = [{"section": "classification", **row} for row in classification]
                if request.format == "xlsx":
                    write_rows_xlsx(path, {"summary": rows})
                else:
                    buffer = string_io.StringIO(newline="")
                    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
                    writer.writeheader()
                    writer.writerows(rows)
                    path.write_text(buffer.getvalue(), encoding="utf-8")
        else:
            review_by_id = {review.record_id: review for review in reviews}
            rows = []
            detail_records = filtered_reviewed if active_filter else records
            for record in detail_records:
                review = review_by_id.get(record.record_id)
                row = record.to_row()
                row.update(
                    {
                        "effective_include": bool(review.effective_include)
                        if review
                        else record.quality_label == "valid",
                        "sentiment_override": review.sentiment_override
                        if review
                        else None,
                        "topic_override": review.topic_override if review else None,
                        "review_reason": review.reason if review else "",
                    }
                )
                rows.append(row)
            if request.format == "json":
                path.write_text(
                    json.dumps(
                        {"schema_version": 1, "job_id": job_id, "records": rows},
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            elif request.format == "xlsx":
                write_rows_xlsx(
                    path,
                    {
                        "records": [
                            {key: _csv_safe(value) for key, value in row.items()}
                            for row in rows
                        ]
                    },
                )
            else:
                buffer = string_io.StringIO(newline="")
                fieldnames = (
                    list(rows[0])
                    if rows
                    else [
                        *records[0].to_row(),
                        "effective_include",
                        "sentiment_override",
                        "topic_override",
                        "review_reason",
                    ]
                )
                writer = csv.DictWriter(buffer, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(
                    {key: _csv_safe(value) for key, value in row.items()}
                    for row in rows
                )
                path.write_text(buffer.getvalue(), encoding="utf-8")
        artifact_id = store.register_artifact(
            job_id,
            path,
            filename=filename,
            media_type=media_types[request.format],
            scope=request.scope,
            format=request.format,
        )
        return {
            "artifact_id": artifact_id,
            "filename": filename,
            "scope": request.scope,
            "format": request.format,
        }

    @app.get("/api/v1/artifacts/{artifact_id}/download")
    def download_artifact(artifact_id: str) -> FileResponse:
        try:
            artifact = store.artifact(artifact_id)
        except KeyError as exc:
            raise error(404, "ARTIFACT_NOT_FOUND", "Artifact does not exist.") from exc
        return FileResponse(
            artifact["path"],
            media_type=artifact["media_type"],
            filename=artifact["filename"],
        )

    @app.post("/api/v1/comparisons")
    def compare_runs(request: ComparisonRequest) -> dict[str, Any]:
        def completed(job_id: str) -> list[Any]:
            try:
                snapshot = jobs.store.get(job_id)
                path = jobs.output_path(job_id) / "classified.csv"
                reviews = [
                    EffectiveReview(**item)
                    for item in jobs.store.review_snapshot(job_id)
                ]
            except KeyError as exc:
                raise error(
                    404, "JOB_NOT_FOUND", f"Unknown analysis run {job_id}."
                ) from exc
            if snapshot.status != "succeeded" or not path.exists():
                raise error(
                    409, "RESULT_NOT_READY", f"Analysis run {job_id} is not complete."
                )
            return [
                row
                for row in _reviewed_records(read_prepared_csv(path), reviews)
                if row.classification_status == "classified"
            ]

        current = completed(request.current_run)
        reference = completed(request.reference_run)

        def distribution(rows: list[Any], field: str) -> dict[str, Any]:
            counts: dict[str, int] = {}
            for row in rows:
                value = str(getattr(row, field) or "general")
                counts[value] = counts.get(value, 0) + 1
            total = len(rows)
            if not total:
                return {"count": 0, "shares": None}
            return {
                "count": total,
                "shares": {
                    key: round(value / total, 6)
                    for key, value in sorted(counts.items())
                },
            }

        def source_rows(rows: list[Any]) -> list[SourceRecord]:
            return [
                SourceRecord(
                    row.record_id,
                    row.source_file,
                    row.source_sheet,
                    row.source_row,
                    row.platform,
                    row.author_hash,
                    row.text_raw,
                )
                for row in rows
            ]

        structural = compare_structure(
            request.reference_run,
            source_rows(reference),
            request.current_run,
            source_rows(current),
        )
        limitations = []
        if len(reference) < 30 or len(current) < 30:
            limitations.append("sample_lt_30")
        if not reference or not current:
            limitations.append("empty_analysis_distribution")
        return {
            "reference_run": request.reference_run,
            "current_run": request.current_run,
            "reference_count": len(reference),
            "current_count": len(current),
            "reliable": not limitations,
            "limitations": limitations,
            "sentiment": {
                "reference": distribution(reference, "sentiment_label"),
                "current": distribution(current, "sentiment_label"),
            },
            "topics": {
                "reference": distribution(reference, "topic"),
                "current": distribution(current, "topic"),
            },
            "structure": asdict(structural),
        }

    @app.post("/api/v1/jobs/{job_id}/{action}")
    def control_job(job_id: str, action: str) -> dict[str, Any]:
        try:
            operation = {
                "pause": jobs.request_pause,
                "resume": jobs.resume,
                "cancel": jobs.request_cancel,
            }.get(action)
            if operation is None:
                raise error(404, "JOB_ACTION_NOT_FOUND", "Unknown job action.")
            return _snapshot_payload(operation(job_id))
        except KeyError as exc:
            raise error(404, "JOB_NOT_FOUND", "Job does not exist.") from exc
        except ValueError as exc:
            raise error(409, "INVALID_JOB_STATE", str(exc)) from exc

    @app.get("/api/v1/jobs/{job_id}/events")
    async def job_events(
        job_id: str,
        after: int = 0,
        last_event_id: Annotated[int | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        try:
            jobs.store.get(job_id)
        except KeyError as exc:
            raise error(404, "JOB_NOT_FOUND", "Job does not exist.") from exc

        async def stream() -> AsyncIterator[str]:
            cursor = max(after, last_event_id or 0)
            while True:
                events = jobs.store.events(job_id, cursor)
                for event in events:
                    cursor = event["seq"]
                    yield f"id: {cursor}\nevent: {event['kind']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if jobs.store.get(job_id).status in {
                    "succeeded",
                    "failed",
                    "cancelled",
                }:
                    return
                await asyncio.sleep(0.15)

        return StreamingResponse(stream(), media_type="text/event-stream")

    package_static = Path(__file__).resolve().parents[1] / "static"
    web_dist = (
        package_static
        if (package_static / "index.html").is_file()
        else Path(__file__).resolve().parents[3] / "web" / "dist"
    )
    if web_dist.is_dir():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="workbench")
    return app


app = create_app()
