"""Explicit table selection and field mapping for the import workflow.

The legacy CLI intentionally guesses aliases and consumes every non-empty
sheet.  The workbench needs the inverse contract: it shows every available
table first, then only turns user-selected, explicitly mapped tables into
``SourceRecord`` objects.  Parsing itself stays in :mod:`signalbench.io` so
CSV/XLSX handling, limits, and record-ID construction have one owner.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .. import io
from ..io import InputContractError
from ..models import SourceRecord

DATE_FORMAT_ISO8601 = "iso8601"
DATE_FORMAT_YMD = "yyyy-mm-dd"
_MAPPABLE_FIELDS = frozenset({"text", "platform", "author", "record_id", "created_at"})


@dataclass(frozen=True, slots=True)
class TablePreview:
    """A table that the user can choose in an import mapping screen."""

    source_file: str
    source_sha256: str
    sheet: str
    columns: tuple[str, ...]
    preview_rows: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class ImportInspection:
    """Non-mutating inventory for a set of locally selected files."""

    tables: tuple[TablePreview, ...]
    rejected_files: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class SheetMapping:
    """One selected source sheet and its explicit SignalBench field mapping."""

    source_sha256: str
    sheet: str
    columns: Mapping[str, str | None]
    date_format: str | None = None

    def __post_init__(self) -> None:
        unknown = set(self.columns) - _MAPPABLE_FIELDS
        if unknown:
            raise ValueError(f"Unsupported mapping field(s): {', '.join(sorted(unknown))}")
        if not self.source_sha256 or len(self.source_sha256) != 64:
            raise ValueError("source_sha256 must be the selected file's SHA-256 digest")
        if not self.sheet:
            raise ValueError("sheet must not be empty")
        if self.date_format not in {None, DATE_FORMAT_ISO8601, DATE_FORMAT_YMD}:
            raise ValueError("date_format must be iso8601, yyyy-mm-dd, or null")
        if self.columns.get("created_at") is None and self.date_format is not None:
            raise ValueError("date_format requires a created_at column")
        if self.columns.get("created_at") is not None and self.date_format is None:
            raise ValueError("created_at requires an explicit date_format")


@dataclass(frozen=True, slots=True)
class RecordMetadata:
    """Date metadata kept beside, rather than inside, the legacy SourceRecord."""

    record_id: str
    date_raw: str
    occurred_at: str | None
    date_parse_status: str


@dataclass(frozen=True, slots=True)
class MappedImport:
    records: tuple[SourceRecord, ...]
    metadata: tuple[RecordMetadata, ...]
    manifest: dict[str, Any]


def _check_input_file(path: Path) -> None:
    size = path.stat().st_size
    if size > io.MAX_INPUT_BYTES:
        raise InputContractError(
            f"{path.name} is {size} bytes; v1 supports files up to {io.MAX_INPUT_BYTES} bytes."
        )


def _tables_for_path(path: Path) -> Iterator[tuple[str, list[str], Iterator[tuple[int, dict[str, str]]]]]:
    if path.suffix.lower() in {".csv", ".tsv"}:
        headers, rows = io._iter_csv_rows(path)
        yield "__csv__", headers, rows
    else:
        yield from io._iter_xlsx_sheets(path)


def _validate_headers(headers: Sequence[str], *, path: Path, sheet: str) -> None:
    if len(headers) > io.MAX_COLUMNS:
        raise InputContractError(f"Input has {len(headers)} columns; v1 supports at most {io.MAX_COLUMNS}.")
    if any(len(header) > io.MAX_METADATA_CHARS for header in headers):
        raise InputContractError(
            f"{path.name}:{sheet} has a header longer than the {io.MAX_METADATA_CHARS}-character v1 limit."
        )
    nonempty = [header for header in headers if header]
    if len(nonempty) != len(set(nonempty)):
        raise InputContractError(f"{path.name}:{sheet} has duplicate column headers; rename them before mapping.")


def inspect_inputs(paths: Sequence[Path], *, preview_limit: int = 20) -> ImportInspection:
    """List all mappable tables and a bounded source preview without guessing fields."""

    if not 1 <= preview_limit <= 100:
        raise ValueError("preview_limit must be between 1 and 100")
    inputs = io.discover_inputs(paths)
    if len(inputs) > io.MAX_INPUT_FILES:
        raise InputContractError(f"Input expands to {len(inputs)} files; v1 supports at most {io.MAX_INPUT_FILES}.")
    tables: list[TablePreview] = []
    for path in inputs:
        _check_input_file(path)
        source_sha256 = io.sha256_file(path)
        for sheet, headers, rows in _tables_for_path(path):
            _validate_headers(headers, path=path, sheet=sheet)
            preview_rows: list[dict[str, str]] = []
            for _row_number, row in rows:
                if len(preview_rows) == preview_limit:
                    break
                preview_rows.append({header: row.get(header, "") for header in headers if header})
            tables.append(
                TablePreview(
                    source_file=path.name,
                    source_sha256=source_sha256,
                    sheet=sheet,
                    columns=tuple(headers),
                    preview_rows=tuple(preview_rows),
                )
            )
    if not tables:
        raise InputContractError("Inputs contained no tables with a header row.")
    return ImportInspection(tuple(tables))


def _parse_date(value: str, date_format: str | None) -> tuple[str | None, str]:
    raw = value.strip()
    if date_format is None:
        return None, "unmapped"
    if not raw:
        return None, "missing"
    try:
        if date_format == DATE_FORMAT_YMD:
            if len(raw) != 10 or raw[4] != "-" or raw[7] != "-":
                raise ValueError
            parsed = date.fromisoformat(raw)
            return parsed.isoformat(), "parsed_date"
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        parsed_at = datetime.fromisoformat(normalized)
        if parsed_at.tzinfo is None:
            raise ValueError
        return parsed_at.astimezone(UTC).isoformat().replace("+00:00", "Z"), "parsed_datetime"
    except ValueError:
        return None, "invalid_format"


def _validated_columns(mapping: SheetMapping, available: Sequence[str]) -> dict[str, str | None]:
    if not mapping.columns.get("text"):
        raise InputContractError(f"{mapping.sheet} requires a text column before creating a batch.")
    available_set = set(available)
    resolved = {field: mapping.columns.get(field) for field in _MAPPABLE_FIELDS}
    for field, column in resolved.items():
        if column is not None and column not in available_set:
            raise InputContractError(
                f"{mapping.sheet} maps {field} to unknown column {column!r}; refresh the file preview."
            )
    used = [column for column in resolved.values() if column is not None]
    if len(used) != len(set(used)):
        raise InputContractError(f"{mapping.sheet} maps one source column to multiple fields.")
    return {field: resolved[field] for field in ("text", "platform", "author", "record_id")}


def load_mapped_records(paths: Sequence[Path], mappings: Sequence[SheetMapping]) -> MappedImport:
    """Create records from selected sheets only, with mapping-bound date metadata."""

    if not mappings:
        raise InputContractError("Select at least one sheet and map its text column.")
    inputs = io.discover_inputs(paths)
    if len(inputs) > io.MAX_INPUT_FILES:
        raise InputContractError(f"Input expands to {len(inputs)} files; v1 supports at most {io.MAX_INPUT_FILES}.")
    selected = {(mapping.source_sha256, mapping.sheet): mapping for mapping in mappings}
    if len(selected) != len(mappings):
        raise InputContractError("A source sheet can be mapped only once per import.")

    records: list[SourceRecord] = []
    metadata: list[RecordMetadata] = []
    seen_ids: set[str] = set()
    manifest_files: list[dict[str, Any]] = []
    used_keys: set[tuple[str, str]] = set()

    for path in inputs:
        _check_input_file(path)
        source_sha256 = io.sha256_file(path)
        file_entry: dict[str, Any] = {
            "file": path.name,
            "format": path.suffix.lower().lstrip("."),
            "sha256": source_sha256,
            "sheets": [],
        }
        for sheet, headers, rows in _tables_for_path(path):
            _validate_headers(headers, path=path, sheet=sheet)
            mapping = selected.get((source_sha256, sheet))
            sheet_entry: dict[str, Any] = {
                "name": sheet,
                "columns": headers,
                "selected": mapping is not None,
                "rows": 0,
            }
            if mapping is None:
                file_entry["sheets"].append(sheet_entry)
                continue
            used_keys.add((source_sha256, sheet))
            columns = _validated_columns(mapping, headers)
            sheet_entry["mapping"] = dict(mapping.columns)
            sheet_entry["date_format"] = mapping.date_format
            for row_number, row in rows:
                record = io._record_from_row(
                    path=path,
                    source_sha256=source_sha256,
                    sheet=sheet,
                    row_number=row_number,
                    row=row,
                    columns=columns,
                )
                if record is None:
                    continue
                if len(records) >= io.MAX_RECORDS:
                    raise InputContractError(
                        f"Input exceeds the {io.MAX_RECORDS}-record v1 limit at {path.name}:{row_number}."
                    )
                if record.record_id in seen_ids:
                    raise InputContractError(f"Stable record id collision at {path.name}:{row_number}.")
                seen_ids.add(record.record_id)
                created_at = mapping.columns.get("created_at")
                date_raw = row.get(created_at, "") if created_at else ""
                occurred_at, date_parse_status = _parse_date(str(date_raw), mapping.date_format)
                records.append(record)
                metadata.append(
                    RecordMetadata(record.record_id, str(date_raw), occurred_at, date_parse_status)
                )
                sheet_entry["rows"] += 1
            file_entry["sheets"].append(sheet_entry)
        manifest_files.append(file_entry)

    missing = selected.keys() - used_keys
    if missing:
        rendered = ", ".join(f"{digest[:12]}:{sheet}" for digest, sheet in sorted(missing))
        raise InputContractError(f"Selected source sheet changed or disappeared: {rendered}.")
    if not records:
        raise InputContractError("Selected sheets contained no non-empty data rows.")
    return MappedImport(
        records=tuple(records),
        metadata=tuple(metadata),
        manifest={"schema_version": 2, "record_count": len(records), "files": manifest_files},
    )
