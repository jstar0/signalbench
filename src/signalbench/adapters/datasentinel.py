"""Small, explicit DataSentinel adapters for SignalBench import structure.

Free-form feedback text never becomes a categorical distribution.  This
adapter profiles only declared structural fields that can explain why two
imports are incomparable: source sheet, platform, optional author presence,
and text length.  It exposes DataSentinel's evidence without its admission
gate, which is intentionally not part of a feedback-analysis workflow.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

try:
    from datasentinel import (
        ColumnSpec,
        TableContract,
        TableSnapshot,
        profile_drift,
        validate_snapshot,
    )
except ImportError:  # A built wheel must not bind to an unrelated PyPI namesake.
    ColumnSpec = TableContract = TableSnapshot = None  # type: ignore[assignment,misc]
    profile_drift = validate_snapshot = None  # type: ignore[assignment]

from ..models import SourceRecord

_CONTRACT = (
    TableContract(
        columns=(
            ColumnSpec("row_index", "integer"),
            ColumnSpec("platform", "string"),
            ColumnSpec("source_sheet", "string"),
            ColumnSpec("has_author", "boolean"),
            ColumnSpec("text_length", "integer", min_value=0),
        ),
        primary_key="row_index",
    )
    if TableContract is not None and ColumnSpec is not None
    else None
)


@dataclass(frozen=True, slots=True)
class StructuralDriftMetric:
    column: str
    kind: str
    distribution_score: float | None
    missingness_delta: float
    score: float | None
    comparable: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class StructuralDriftReport:
    reference_name: str
    candidate_name: str
    metrics: tuple[StructuralDriftMetric, ...]


@dataclass(frozen=True, slots=True)
class _ValidationIssue:
    row_index: int
    column: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class _ValidationReport:
    snapshot_name: str
    row_count: int
    checked_cells: int
    issues: tuple[_ValidationIssue, ...]
    duplicate_rows: int = 0
    unknown_columns: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.issues


def _rows(records: Sequence[SourceRecord]) -> list[dict[str, Any]]:
    return [
        {
            "row_index": index,
            "platform": record.platform,
            "source_sheet": record.source_sheet,
            "has_author": bool(record.author_hash),
            "text_length": len(record.text_raw),
        }
        for index, record in enumerate(records)
    ]


def _snapshot(name: str, records: Sequence[SourceRecord]):
    if TableSnapshot is None:
        return _rows(records)
    return TableSnapshot.from_rows(name, _rows(records))


def _categorical_js(reference: list[Any], candidate: list[Any]) -> float:
    support = sorted({repr(value) for value in (*reference, *candidate)})
    ref_counts = Counter(repr(value) for value in reference)
    cand_counts = Counter(repr(value) for value in candidate)
    epsilon = 1e-9
    ref_total = len(reference) + epsilon * len(support)
    cand_total = len(candidate) + epsilon * len(support)
    value = 0.0
    for key in support:
        ref_probability = (ref_counts[key] + epsilon) / ref_total
        cand_probability = (cand_counts[key] + epsilon) / cand_total
        midpoint = 0.5 * (ref_probability + cand_probability)
        value += 0.5 * ref_probability * math.log(ref_probability / midpoint)
        value += 0.5 * cand_probability * math.log(cand_probability / midpoint)
    return round(max(0.0, value), 8)


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _numeric_psi(reference: list[Any], candidate: list[Any], bins: int = 10) -> float:
    ref = [float(value) for value in reference if type(value) in (int, float) and math.isfinite(float(value))]
    cand = [float(value) for value in candidate if type(value) in (int, float) and math.isfinite(float(value))]
    edges = sorted({_quantile(ref, index / bins) for index in range(bins + 1)})
    if len(edges) < 3:
        center = sum(ref) / len(ref)
        spread = max(abs(center) * 0.05, 1.0)
        edges = [-math.inf, center - spread, center + spread, math.inf]
    else:
        edges[0], edges[-1] = -math.inf, math.inf

    def counts(values: list[float]) -> list[int]:
        result = [0] * (len(edges) - 1)
        for value in values:
            for index, (lower, upper) in enumerate(pairwise(edges)):
                if lower <= value < upper or index == len(result) - 1 and value == upper:
                    result[index] += 1
                    break
        return result

    ref_counts, cand_counts = counts(ref), counts(cand)
    epsilon = 1e-6
    ref_total = sum(ref_counts) + epsilon * len(ref_counts)
    cand_total = sum(cand_counts) + epsilon * len(cand_counts)
    value = 0.0
    for ref_count, cand_count in zip(ref_counts, cand_counts):
        ref_share = (ref_count + epsilon) / ref_total
        cand_share = (cand_count + epsilon) / cand_total
        value += (cand_share - ref_share) * math.log(cand_share / ref_share)
    return round(max(0.0, value), 8)


def validate_structure(name: str, records: Sequence[SourceRecord]):
    """Return row-level DataSentinel structural issues for an import."""

    snapshot = _snapshot(name, records)
    if validate_snapshot is not None and _CONTRACT is not None:
        return validate_snapshot(snapshot, _CONTRACT)
    issues: list[_ValidationIssue] = []
    expected = {"row_index": int, "platform": str, "source_sheet": str, "has_author": bool, "text_length": int}
    for row_index, row in enumerate(snapshot):
        for column, value_type in expected.items():
            if type(row[column]) is not value_type:
                issues.append(_ValidationIssue(row_index, column, "type_mismatch", f"{column} has the wrong type"))
        if type(row["text_length"]) is int and row["text_length"] < 0:
            issues.append(_ValidationIssue(row_index, "text_length", "range_violation", "text_length is below 0"))
    return _ValidationReport(name, len(snapshot), len(snapshot) * len(expected), tuple(issues))


def compare_structure(
    reference_name: str,
    reference_records: Sequence[SourceRecord],
    candidate_name: str,
    candidate_records: Sequence[SourceRecord],
) -> StructuralDriftReport:
    """Compare declared structural fields and label empty distributions honestly."""

    reference = _snapshot(reference_name, reference_records)
    candidate = _snapshot(candidate_name, candidate_records)
    if profile_drift is None or _CONTRACT is None:
        metrics = []
        reference_rows, candidate_rows = reference, candidate
        for column, kind in (("platform", "categorical"), ("source_sheet", "categorical"), ("has_author", "categorical"), ("text_length", "numeric")):
            reference_values = [row[column] for row in reference_rows if row[column] is not None]
            candidate_values = [row[column] for row in candidate_rows if row[column] is not None]
            if not reference_values or not candidate_values:
                metrics.append(StructuralDriftMetric(column, kind, None, 0.0, None, False, "reference_empty" if not reference_values else "candidate_empty"))
                continue
            distribution = _numeric_psi(reference_values, candidate_values) if kind == "numeric" else _categorical_js(reference_values, candidate_values)
            metrics.append(StructuralDriftMetric(column, kind, distribution, 0.0, distribution, True))
        return StructuralDriftReport(reference_name, candidate_name, tuple(metrics))

    report = profile_drift(reference, candidate, _CONTRACT)
    metrics: list[StructuralDriftMetric] = []
    for metric in report.metrics:
        reference_values = [value for value in reference.values(metric.column) if value is not None]
        candidate_values = [value for value in candidate.values(metric.column) if value is not None]
        if not reference_values or not candidate_values:
            metrics.append(
                StructuralDriftMetric(
                    column=metric.column,
                    kind=metric.kind,
                    distribution_score=None,
                    missingness_delta=metric.missingness_delta,
                    score=None,
                    comparable=False,
                    reason="reference_empty" if not reference_values else "candidate_empty",
                )
            )
            continue
        metrics.append(
            StructuralDriftMetric(
                column=metric.column,
                kind=metric.kind,
                distribution_score=metric.distribution_score,
                missingness_delta=metric.missingness_delta,
                score=metric.score,
                comparable=True,
            )
        )
    return StructuralDriftReport(reference_name, candidate_name, tuple(metrics))
