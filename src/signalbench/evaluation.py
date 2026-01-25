from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .classify import Classifier
from .io import InputContractError, atomic_write_text
from .models import SENTIMENT_LABELS, SourceRecord
from .quality import prepare_records

_CALIBRATION_BINS: tuple[tuple[float, float, str], ...] = (
    (0.0, 0.50, "[0.00, 0.50)"),
    (0.50, 0.70, "[0.50, 0.70)"),
    (0.70, 0.85, "[0.70, 0.85)"),
    (0.85, 1.000001, "[0.85, 1.00]"),
)


def _read_gold(path: Path) -> list[tuple[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise InputContractError("Gold fixture has no header row.")
        fields = {field.casefold(): field for field in reader.fieldnames}
        text_column = (
            fields.get("text")
            or fields.get("comment")
            or fields.get("评论")
            or fields.get("评论内容")
        )
        label_column = (
            fields.get("label")
            or fields.get("sentiment")
            or fields.get("情感")
            or fields.get("情感标签")
        )
        if not text_column or not label_column:
            raise InputContractError("Gold fixture needs text/comment and label/sentiment columns.")
        examples: list[tuple[str, str]] = []
        for row_number, row in enumerate(reader, start=2):
            text = str(row.get(text_column, "") or "").strip()
            label = str(row.get(label_column, "") or "").strip().casefold()
            if not text:
                raise InputContractError(f"Gold fixture row {row_number} has blank text.")
            if label not in SENTIMENT_LABELS:
                raise InputContractError(
                    f"Gold fixture row {row_number} has unsupported label {label!r}."
                )
            examples.append((text, label))
    if not examples:
        raise InputContractError("Gold fixture contains no examples.")
    return examples


def _f1(precision: float, recall: float) -> float:
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _distribution(values: Sequence[str], labels: Sequence[str]) -> dict[str, dict[str, float | int]]:
    """Return stable support/rate pairs for each label."""

    total = len(values)
    return {
        label: {
            "support": values.count(label),
            "rate": round(values.count(label) / total, 6) if total else 0.0,
        }
        for label in labels
    }


def _confidence_calibration(
    predictions: Sequence[Any],
    expected_map: Mapping[str, str],
) -> dict[str, Any]:
    """Summarize whether confidence tracks observed correctness.

    This deliberately uses coarse, deterministic bins rather than presenting
    the tiny fixture as a statistically calibrated model.  The resulting ECE
    is a diagnostic for adapter authors and is safe to compare across runs.
    """

    total = len(predictions)
    ece = 0.0
    bins: list[dict[str, Any]] = []
    for lower, upper, display in _CALIBRATION_BINS:
        selected = [
            prediction
            for prediction in predictions
            if lower <= float(prediction.confidence) < upper
        ]
        count = len(selected)
        if count:
            accuracy = sum(
                prediction.sentiment_label == expected_map[prediction.record_id]
                for prediction in selected
            ) / count
            mean_confidence = sum(float(prediction.confidence) for prediction in selected) / count
            ece += (count / total) * abs(accuracy - mean_confidence)
        else:
            accuracy = 0.0
            mean_confidence = 0.0
        bins.append(
            {
                "range": display,
                "count": count,
                "accuracy": round(accuracy, 6),
                "mean_confidence": round(mean_confidence, 6),
            }
        )
    return {"ece": round(ece, 6), "bins": bins}


def evaluate_classifier(
    classifier: Classifier,
    examples: Sequence[tuple[str, str]],
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source_records = [
        SourceRecord(
            record_id=f"gold_{index:04d}",
            source_file="synthetic-gold.csv",
            source_sheet="gold",
            source_row=index + 2,
            platform="synthetic",
            author_hash="",
            text_raw=text,
        )
        for index, (text, _label) in enumerate(examples)
    ]
    prepared = prepare_records(source_records, config)
    invalid = [record.record_id for record in prepared if record.quality_label != "valid"]
    if invalid:
        raise ValueError(f"Gold fixture contains rows rejected by the quality gate: {invalid}")
    predictions = classifier.classify(prepared)
    prediction_map = {item.record_id: item.sentiment_label for item in predictions}
    expected_map = {record.record_id: label for record, (_text, label) in zip(prepared, examples)}
    if len(prediction_map) != len(predictions) or set(prediction_map) != set(expected_map):
        raise ValueError("Classifier did not return one prediction per gold row.")
    unsupported = sorted(set(prediction_map.values()) - set(SENTIMENT_LABELS))
    if unsupported:
        raise ValueError(f"Classifier returned unsupported label(s): {unsupported}")

    confusion = {actual: {predicted: 0 for predicted in SENTIMENT_LABELS} for actual in SENTIMENT_LABELS}
    for record_id, actual in expected_map.items():
        confusion[actual][prediction_map[record_id]] += 1
    per_label: dict[str, dict[str, float | int]] = {}
    for label in SENTIMENT_LABELS:
        true_positive = confusion[label][label]
        false_positive = sum(confusion[actual][label] for actual in SENTIMENT_LABELS if actual != label)
        false_negative = sum(confusion[label][predicted] for predicted in SENTIMENT_LABELS if predicted != label)
        precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
        recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
        per_label[label] = {
            "precision": round(precision, 6),
            "recall": round(recall, 6),
            "f1": round(_f1(precision, recall), 6),
            "support": sum(confusion[label].values()),
        }
    total = len(examples)
    correct = sum(confusion[label][label] for label in SENTIMENT_LABELS)
    actual_labels = [expected_map[record.record_id] for record in prepared]
    predicted_labels = [prediction_map[record.record_id] for record in prepared]
    return {
        "schema_version": 1,
        "classifier": classifier.name,
        "examples": total,
        "accuracy": round(correct / total, 6),
        "macro_f1": round(sum(float(per_label[label]["f1"]) for label in SENTIMENT_LABELS) / len(SENTIMENT_LABELS), 6),
        "labels": list(SENTIMENT_LABELS),
        "per_label": per_label,
        "confusion": confusion,
        "actual_distribution": _distribution(actual_labels, SENTIMENT_LABELS),
        "predicted_distribution": _distribution(predicted_labels, SENTIMENT_LABELS),
        "confidence_calibration": _confidence_calibration(predictions, expected_map),
    }


def evaluate_gold_file(
    gold_path: Path,
    *,
    classifier: Classifier,
    config: Mapping[str, Any] | None = None,
    output_path: Path | None = None,
) -> dict[str, Any]:
    examples = _read_gold(gold_path)
    payload = evaluate_classifier(classifier, examples, config=config)
    payload["fixture_sha256"] = hashlib.sha256(gold_path.read_bytes()).hexdigest()
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(output_path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return payload


def write_benchmark_artifacts(
    gold_path: Path,
    *,
    output_dir: Path,
    classifier: Classifier,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a cleared fixture and write a compact, reviewable evidence bundle.

    The JSON is the machine-readable source of truth.  The Markdown file is a
    deliberately small human-facing summary that can be attached to a code
    review record without exposing row-level text.
    """

    payload = evaluate_gold_file(gold_path, classifier=classifier, config=config)
    payload["benchmark"] = {
        "fixture": gold_path.name,
        "fixture_sha256": payload["fixture_sha256"],
        "disclaimer": "Synthetic or independently cleared fixture; not a production accuracy claim.",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = output_dir / "metrics.json"
    atomic_write_text(metrics_path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    table_rows = []
    for label in payload["labels"]:
        metrics = payload["per_label"][label]
        table_rows.append(
            f"| {label} | {metrics['support']} | {metrics['precision']:.3f} | "
            f"{metrics['recall']:.3f} | {metrics['f1']:.3f} |"
        )
    calibration_rows = []
    for bucket in payload["confidence_calibration"]["bins"]:
        calibration_rows.append(
            f"| {bucket['range']} | {bucket['count']} | {bucket['accuracy']:.3f} | "
            f"{bucket['mean_confidence']:.3f} |"
        )
    summary = "\n".join(
        [
            "# SignalBench benchmark evidence",
            "",
            f"- Fixture: `{gold_path.name}`",
            f"- Fixture SHA-256: `{payload['fixture_sha256']}`",
            f"- Classifier: `{payload['classifier']}`",
            f"- Examples: **{payload['examples']}**",
            f"- Accuracy: **{payload['accuracy']:.3f}**",
            f"- Macro-F1: **{payload['macro_f1']:.3f}**",
            f"- Expected calibration error (ECE): **{payload['confidence_calibration']['ece']:.3f}**",
            "",
            "> This is a deterministic, synthetic/cleared-fixture baseline. It is an engineering contract and regression signal, not a claim about real-world sentiment accuracy.",
            "",
            "## Per-label metrics",
            "",
            "| Label | Support | Precision | Recall | F1 |",
            "|---|---:|---:|---:|---:|",
            *table_rows,
            "",
            "## Confidence calibration",
            "",
            "| Confidence bin | Count | Observed accuracy | Mean confidence |",
            "|---|---:|---:|---:|",
            *calibration_rows,
            "",
            "The full confusion matrix, distributions, and fixture hash are in `metrics.json`.",
            "",
        ]
    )
    summary_path = output_dir / "summary.md"
    atomic_write_text(summary_path, summary)
    return {
        "metrics": str(metrics_path),
        "summary": str(summary_path),
        "fixture_sha256": payload["fixture_sha256"],
        "examples": payload["examples"],
        "accuracy": payload["accuracy"],
        "macro_f1": payload["macro_f1"],
        "ece": payload["confidence_calibration"]["ece"],
    }
