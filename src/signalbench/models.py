from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

SENTIMENT_LABELS = ("positive", "neutral", "negative")
NEGATIVE_SUBTYPES = (
    "criticism_blame",
    "distrust_suspicion",
    "risk_concern",
    "sarcasm_attack",
)
QUALITY_LABELS = ("valid", "invalid", "display_issue")


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "是"}


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    raw = str(value or "").strip()
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return (raw,)
    return tuple(str(item) for item in parsed) if isinstance(parsed, list) else (raw,)


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """One row from an input table.

    The raw author value is intentionally not retained. A short digest preserves
    local traceability, but remains pseudonymous metadata rather than anonymity.
    """

    record_id: str
    source_file: str
    source_sheet: str
    source_row: int
    platform: str
    author_hash: str
    text_raw: str


@dataclass(slots=True)
class PreparedRecord:
    record_id: str
    source_file: str
    source_sheet: str
    source_row: int
    platform: str
    author_hash: str
    text_raw: str
    text_normalized: str
    text_fingerprint: str
    quality_label: str
    quality_reason: str
    review_flag: bool
    duplicate_group: str
    duplicate_rank: int
    duplicate_count: int
    classification_status: str = "pending"
    sentiment_label: str = ""
    sentiment_score: float = 0.0
    confidence: float = 0.0
    topic: str = ""
    negative_subtype: str = ""
    decision_note: str = ""
    rule_hits: tuple[str, ...] = ()
    attempts: int = 0

    def to_row(self) -> dict[str, str]:
        """Return a stable, CSV-friendly representation."""

        return {
            "record_id": self.record_id,
            "source_file": self.source_file,
            "source_sheet": self.source_sheet,
            "source_row": str(self.source_row),
            "platform": self.platform,
            "author_hash": self.author_hash,
            "text_raw": self.text_raw,
            "text_normalized": self.text_normalized,
            "text_fingerprint": self.text_fingerprint,
            "quality_label": self.quality_label,
            "quality_reason": self.quality_reason,
            "review_flag": str(self.review_flag).lower(),
            "duplicate_group": self.duplicate_group,
            "duplicate_rank": str(self.duplicate_rank),
            "duplicate_count": str(self.duplicate_count),
            "classification_status": self.classification_status,
            "sentiment_label": self.sentiment_label,
            "sentiment_score": f"{self.sentiment_score:.6f}",
            "confidence": f"{self.confidence:.6f}",
            "topic": self.topic,
            "negative_subtype": self.negative_subtype,
            "decision_note": self.decision_note,
            "rule_hits": json.dumps(list(self.rule_hits), ensure_ascii=False),
            "attempts": str(self.attempts),
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> PreparedRecord:
        """Rehydrate a record from a CSV row, tolerating missing optional fields."""

        quality_label = str(row.get("quality_label", "valid"))
        status = str(row.get("classification_status", "pending"))
        return cls(
            record_id=str(row.get("record_id", "")),
            source_file=str(row.get("source_file", "")),
            source_sheet=str(row.get("source_sheet", "")),
            source_row=_as_int(row.get("source_row"), 0),
            platform=str(row.get("platform", "")),
            author_hash=str(row.get("author_hash", "")),
            text_raw=str(row.get("text_raw", "")),
            text_normalized=str(row.get("text_normalized", "")),
            text_fingerprint=str(row.get("text_fingerprint", "")),
            quality_label=quality_label,
            quality_reason=str(row.get("quality_reason", "")),
            review_flag=_as_bool(row.get("review_flag", False)),
            duplicate_group=str(row.get("duplicate_group", "")),
            duplicate_rank=_as_int(row.get("duplicate_rank"), 1),
            duplicate_count=_as_int(row.get("duplicate_count"), 1),
            classification_status=status,
            sentiment_label=str(row.get("sentiment_label", "")),
            sentiment_score=_as_float(row.get("sentiment_score"), 0.0),
            confidence=_as_float(row.get("confidence"), 0.0),
            topic=str(row.get("topic", "")),
            negative_subtype=str(row.get("negative_subtype", "")),
            decision_note=str(row.get("decision_note", "")),
            rule_hits=_as_string_tuple(row.get("rule_hits", "")),
            attempts=_as_int(row.get("attempts"), 0),
        )


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    record_id: str
    sentiment_label: str
    sentiment_score: float
    confidence: float
    topic: str
    negative_subtype: str
    review_flag: bool
    decision_note: str
    rule_hits: tuple[str, ...] = ()


PREPARED_COLUMNS = [
    "record_id",
    "source_file",
    "source_sheet",
    "source_row",
    "platform",
    "author_hash",
    "text_raw",
    "text_normalized",
    "text_fingerprint",
    "quality_label",
    "quality_reason",
    "review_flag",
    "duplicate_group",
    "duplicate_rank",
    "duplicate_count",
    "classification_status",
    "sentiment_label",
    "sentiment_score",
    "confidence",
    "topic",
    "negative_subtype",
    "decision_note",
    "rule_hits",
    "attempts",
]
