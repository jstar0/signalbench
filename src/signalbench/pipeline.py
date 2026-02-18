from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .adapters.imports import SheetMapping, load_mapped_records
from .classify import (
    CheckpointMismatch,
    Classifier,
    DeterministicClassifier,
    ReplayClassifier,
    classify_records,
)
from .config import load_config
from .io import (
    load_source_records,
    read_prepared_csv,
    write_json,
    write_prepared_csv,
    write_prepared_xlsx,
)
from .models import PreparedRecord
from .quality import prepare_records, quality_summary
from .report import build_report
from .review_store import ReviewStore


class UnsupportedClassifier(ValueError):
    pass


def _config_digest(config: Mapping[str, Any]) -> str:
    canonical = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_classifier(
    name: str,
    config: Mapping[str, Any],
    *,
    replay_path: Path | None = None,
) -> Classifier:
    resolved = (name or "deterministic").strip().casefold()
    if resolved in {"deterministic", "mock", "baseline"}:
        return DeterministicClassifier(config)
    if resolved == "replay":
        if replay_path is None:
            raise ValueError("--replay is required when --classifier replay is selected.")
        return ReplayClassifier(replay_path)
    raise UnsupportedClassifier(
        f"Unsupported classifier {name!r}. Available classifiers: deterministic, replay."
    )


def prepare_pipeline(
    inputs: Sequence[Path],
    *,
    output_dir: Path,
    config_path: Path | None = None,
) -> tuple[list[PreparedRecord], dict[str, Any]]:
    config = load_config(config_path)
    source_records, manifest = load_source_records(inputs, config)
    prepared = prepare_records(source_records, config)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        **manifest,
        "config_sha256": _config_digest(config),
        "preparation_digest": _preparation_digest(prepared),
    }
    write_prepared_csv(output_dir / "prepared.csv", prepared)
    write_prepared_xlsx(output_dir / "prepared.xlsx", prepared)
    write_json(output_dir / "input_manifest.json", manifest)
    # Keep a compact quality snapshot next to the prepared rows so an inspect
    # run never needs to re-open the source workbook.
    from .report import write_rows_csv

    write_rows_csv(output_dir / "quality_summary.csv", quality_summary(prepared))
    return prepared, manifest


def _mapping_digest(mappings: Sequence[SheetMapping]) -> str:
    """Bind a mapped batch to the selected tables and field semantics."""

    payload = [
        {
            "source_sha256": mapping.source_sha256,
            "sheet": mapping.sheet,
            "columns": dict(sorted(mapping.columns.items())),
            "date_format": mapping.date_format,
        }
        for mapping in sorted(mappings, key=lambda item: (item.source_sha256, item.sheet))
    ]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def prepare_mapped_pipeline(
    inputs: Sequence[Path],
    mappings: Sequence[SheetMapping],
    *,
    output_dir: Path,
    batch_id: str,
    review_database: Path | None = None,
    config_path: Path | None = None,
) -> tuple[list[PreparedRecord], dict[str, Any]]:
    """Prepare explicitly mapped tables and initialize their independent reviews.

    This is the workbench entry point.  The legacy :func:`prepare_pipeline`
    remains available for scripted alias-based CLI use; it is not the import
    path a user-facing app should call.
    """

    config = load_config(config_path)
    imported = load_mapped_records(inputs, mappings)
    prepared = prepare_records(imported.records, config)
    metadata_by_record = {
        item.record_id: {
            "record_id": item.record_id,
            "date_raw": item.date_raw,
            "occurred_at": item.occurred_at,
            "date_parse_status": item.date_parse_status,
        }
        for item in imported.metadata
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    prepared_digest = _preparation_digest(prepared)
    mapping_digest = _mapping_digest(mappings)
    manifest = {
        **imported.manifest,
        "config_sha256": _config_digest(config),
        "mapping_sha256": mapping_digest,
        "preparation_digest": prepared_digest,
        # A future worker binds its checkpoint to this composite digest.  It
        # differs when a user changes even a date-only mapping, while keeping
        # the legacy PreparedRecord schema untouched.
        "mapped_preparation_digest": hashlib.sha256(
            f"{prepared_digest}\x1f{mapping_digest}".encode()
        ).hexdigest(),
    }
    write_prepared_csv(output_dir / "prepared.csv", prepared)
    write_prepared_xlsx(output_dir / "prepared.xlsx", prepared)
    write_json(output_dir / "input_manifest.json", manifest)
    write_json(output_dir / "record_metadata.json", {"records": list(metadata_by_record.values())})
    from .report import write_rows_csv

    write_rows_csv(output_dir / "quality_summary.csv", quality_summary(prepared))
    store = ReviewStore(review_database or output_dir / "reviews.sqlite")
    store.create_batch(batch_id, prepared, metadata_by_record=metadata_by_record)
    return prepared, manifest


def _preparation_digest(records: Sequence[PreparedRecord]) -> str:
    from .quality import preparation_digest

    return preparation_digest(records)


def classify_pipeline(
    *,
    prepared_path: Path,
    output_dir: Path,
    classifier_name: str = "deterministic",
    config_path: Path | None = None,
    batch_size: int = 32,
    retries: int = 2,
    backoff_seconds: float = 0.0,
    checkpoint_path: Path | None = None,
    fail_after_batches: int | None = None,
    replay_path: Path | None = None,
) -> tuple[list[PreparedRecord], Path]:
    config = load_config(config_path)
    # Pass the same rules the gate ran under, so an edited label is caught.
    records = read_prepared_csv(prepared_path, config)
    classifier = build_classifier(classifier_name, config, replay_path=replay_path)
    resolved_checkpoint = checkpoint_path or output_dir / "checkpoint.json"
    classified = classify_records(
        records,
        classifier,
        batch_size=batch_size,
        retries=retries,
        backoff_seconds=backoff_seconds,
        checkpoint_path=resolved_checkpoint,
        fail_after_batches=fail_after_batches,
        config_digest=_config_digest(config),
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    write_prepared_csv(output_dir / "classified.csv", classified)
    write_prepared_xlsx(output_dir / "classified.xlsx", classified)
    return classified, resolved_checkpoint


def classify_mapped_pipeline(
    *,
    prepared_path: Path,
    output_dir: Path,
    classifier_name: str = "deterministic",
    config_path: Path | None = None,
    batch_size: int = 32,
    retries: int = 2,
    backoff_seconds: float = 0.0,
    checkpoint_path: Path | None = None,
    after_batch: Callable[[int, int], str] | None = None,
    eligible_record_ids: set[str] | None = None,
) -> tuple[list[PreparedRecord], Path]:
    """Classify a workbench batch with its mapping-bound resume digest."""

    manifest_path = prepared_path.parent / "input_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mapped_digest = str(manifest.get("mapped_preparation_digest", ""))
    if not mapped_digest:
        raise ValueError("Mapped batch manifest is missing mapped_preparation_digest.")
    config = load_config(config_path)
    records = read_prepared_csv(prepared_path, config)
    if _preparation_digest(records) != str(manifest.get("preparation_digest", "")):
        raise CheckpointMismatch("Mapped batch prepared data no longer matches its manifest.")
    classifier = build_classifier(classifier_name, config)
    resolved_checkpoint = checkpoint_path or output_dir / "checkpoint.json"
    run_digest = mapped_digest
    if eligible_record_ids is not None:
        eligible_payload = "\n".join(sorted(eligible_record_ids))
        run_digest = hashlib.sha256(f"{mapped_digest}\x1f{eligible_payload}".encode()).hexdigest()
    classified = classify_records(
        records,
        classifier,
        batch_size=batch_size,
        retries=retries,
        backoff_seconds=backoff_seconds,
        checkpoint_path=resolved_checkpoint,
        config_digest=_config_digest(config),
        input_digest_override=run_digest,
        after_batch=after_batch,
        eligible_record_ids=eligible_record_ids,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    write_prepared_csv(output_dir / "classified.csv", classified)
    write_prepared_xlsx(output_dir / "classified.xlsx", classified)
    return classified, resolved_checkpoint


def summarize_pipeline(
    *,
    classified_path: Path,
    output_dir: Path,
    manifest_path: Path | None = None,
    classifier_name: str = "deterministic-v1",
) -> dict[str, Any]:
    records = read_prepared_csv(classified_path)
    manifest: dict[str, Any] | None = None
    resolved_manifest = manifest_path or classified_path.parent / "input_manifest.json"
    if resolved_manifest.exists():
        manifest = json.loads(resolved_manifest.read_text(encoding="utf-8"))
    return build_report(
        records,
        output_dir=output_dir,
        manifest=manifest,
        classifier_name=classifier_name,
    )


def run_pipeline(
    inputs: Sequence[Path],
    *,
    output_dir: Path,
    config_path: Path | None = None,
    classifier_name: str = "deterministic",
    batch_size: int = 32,
    retries: int = 2,
    backoff_seconds: float = 0.0,
    replay_path: Path | None = None,
) -> dict[str, Any]:
    prepared, manifest = prepare_pipeline(inputs, output_dir=output_dir, config_path=config_path)
    # Use the freshly written prepared file so the CLI exercises the same
    # serialization boundary that a resumed production run will use.
    classified, checkpoint = classify_pipeline(
        prepared_path=output_dir / "prepared.csv",
        output_dir=output_dir,
        classifier_name=classifier_name,
        replay_path=replay_path,
        config_path=config_path,
        batch_size=batch_size,
        retries=retries,
        backoff_seconds=backoff_seconds,
    )
    report = summarize_pipeline(
        classified_path=output_dir / "classified.csv",
        output_dir=output_dir / "report",
        manifest_path=output_dir / "input_manifest.json",
        classifier_name=build_classifier(
            classifier_name,
            load_config(config_path),
            replay_path=replay_path,
        ).name,
    )
    return {
        "record_count": len(prepared),
        "classified_count": sum(item.classification_status == "classified" for item in classified),
        "checkpoint": str(checkpoint),
        "report": str(output_dir / "report" / "report.html"),
        "preparation_digest": manifest["preparation_digest"],
        "summary": report,
    }


def inspect_pipeline(
    inputs: Sequence[Path], *, config_path: Path | None = None
) -> dict[str, Any]:
    config = load_config(config_path)
    source_records, manifest = load_source_records(inputs, config)
    prepared = prepare_records(source_records, config)
    digest = _preparation_digest(prepared)
    manifest = {
        **manifest,
        "config_sha256": _config_digest(config),
        "preparation_digest": digest,
    }
    return {
        "manifest": manifest,
        "quality": quality_summary(prepared),
        "preparation_digest": digest,
    }
