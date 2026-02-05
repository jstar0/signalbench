from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .config import DEFAULT_CONFIG
from .models import PreparedRecord, SourceRecord

_WHITESPACE_RE = re.compile(r"\s+")
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")
_URL_ONLY_RE = re.compile(
    r"^(?:(?:https?://|www\.)\S+)(?:\s+(?:(?:https?://|www\.)\S+))*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class QualityDecision:
    label: str
    reason: str
    review_flag: bool


def normalize_text(value: object) -> str:
    """Canonicalize text without changing its visible meaning."""

    raw = "" if value is None else unicodedata.normalize("NFKC", str(value))
    raw = _ZERO_WIDTH_RE.sub("", raw)
    return _WHITESPACE_RE.sub(" ", raw).strip()


def _has_letter_or_digit(text: str) -> bool:
    return any(unicodedata.category(char)[0] in {"L", "N"} for char in text)


def _has_control_character(text: str) -> bool:
    return any(
        unicodedata.category(char) == "Cc" and char not in "\t\n\r"
        for char in text
    )


def classify_quality(value: object, config: Mapping[str, Any] | None = None) -> QualityDecision:
    rules = (config or DEFAULT_CONFIG)["quality"]
    normalized = normalize_text(value)
    if not normalized:
        return QualityDecision("invalid", "blank", False)
    if normalized in {str(item) for item in rules["display_issue_exact_values"]}:
        return QualityDecision("display_issue", "literal_zero", True)
    if any(marker in normalized for marker in rules["replacement_markers"]):
        return QualityDecision("display_issue", "encoding_marker", True)
    if _has_control_character(normalized):
        return QualityDecision("display_issue", "control_character", True)
    if any(normalized.startswith(str(prefix)) for prefix in rules["notice_prefixes"]):
        return QualityDecision("invalid", "platform_notice", False)
    folded = normalized.casefold()
    if any(str(marker).casefold() in folded for marker in rules["notice_contains"]):
        return QualityDecision("invalid", "platform_notice", False)
    if any(str(marker).casefold() in folded for marker in rules["boilerplate_contains"]):
        return QualityDecision("invalid", "boilerplate", False)
    if _URL_ONLY_RE.match(normalized):
        return QualityDecision("invalid", "link_only", False)
    if not _has_letter_or_digit(normalized):
        return QualityDecision("invalid", "symbol_or_emoji_only", False)
    signal_characters = sum(
        1 for char in normalized if unicodedata.category(char)[0] in {"L", "N"}
    )
    if signal_characters < int(rules["minimum_signal_characters"]):
        return QualityDecision("invalid", "too_short", False)
    return QualityDecision("valid", "", False)


def text_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def prepare_records(
    records: Iterable[SourceRecord], config: Mapping[str, Any] | None = None
) -> list[PreparedRecord]:
    """Apply quality decisions and deterministic duplicate grouping."""

    resolved_config = config or DEFAULT_CONFIG
    source_records = list(records)
    normalized_rows: list[tuple[SourceRecord, str, str, QualityDecision]] = []
    for source in source_records:
        normalized = normalize_text(source.text_raw)
        fingerprint = text_fingerprint(normalized) if normalized else ""
        decision = classify_quality(source.text_raw, resolved_config)
        normalized_rows.append((source, normalized, fingerprint, decision))

    group_members: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for index, (source, _normalized, fingerprint, _decision) in enumerate(normalized_rows):
        if fingerprint:
            key = (source.platform.casefold(), source.source_sheet, fingerprint)
            group_members[key].append(index)
    group_ranks: dict[int, int] = {}
    for members in group_members.values():
        for rank, member_index in enumerate(members, start=1):
            group_ranks[member_index] = rank

    prepared: list[PreparedRecord] = []
    for index, (source, normalized, fingerprint, decision) in enumerate(normalized_rows):
        key = (source.platform.casefold(), source.source_sheet, fingerprint)
        members = group_members.get(key, []) if fingerprint else []
        duplicate_count = len(members) if members else 1
        duplicate_rank = group_ranks.get(index, 1)
        duplicate_group = f"dup_{fingerprint}" if len(members) > 1 else ""
        prepared.append(
            PreparedRecord(
                record_id=source.record_id,
                source_file=source.source_file,
                source_sheet=source.source_sheet,
                source_row=source.source_row,
                platform=source.platform,
                author_hash=source.author_hash,
                text_raw=source.text_raw,
                text_normalized=normalized,
                text_fingerprint=fingerprint,
                quality_label=decision.label,
                quality_reason=decision.reason,
                review_flag=decision.review_flag,
                duplicate_group=duplicate_group,
                duplicate_rank=duplicate_rank,
                duplicate_count=duplicate_count,
                classification_status="pending" if decision.label == "valid" else "not_applicable",
            )
        )
    return prepared


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def quality_summary(records: Iterable[PreparedRecord]) -> list[dict[str, Any]]:
    """Return overall and per-source quality metrics in stable order."""

    rows = list(records)
    groups: dict[tuple[str, str], list[PreparedRecord]] = defaultdict(list)
    for record in rows:
        groups[(record.platform, record.source_sheet)].append(record)

    def summarize(scope: str, platform: str, sheet: str, group: list[PreparedRecord]) -> dict[str, Any]:
        raw_total = len(group)
        valid_total = sum(item.quality_label == "valid" for item in group)
        invalid_total = sum(item.quality_label == "invalid" for item in group)
        display_issue_total = sum(item.quality_label == "display_issue" for item in group)
        duplicate_rows = sum(item.duplicate_count > 1 for item in group)
        duplicate_valid_rows = sum(
            item.quality_label == "valid" and item.duplicate_count > 1 for item in group
        )
        valid_fingerprints: set[str] = set()
        unique_valid_total = 0
        for item in group:
            if item.quality_label != "valid":
                continue
            key = item.text_fingerprint
            if key not in valid_fingerprints:
                valid_fingerprints.add(key)
                unique_valid_total += 1
        review_total = sum(item.review_flag for item in group)
        return {
            "scope": scope,
            "platform": platform,
            "source_sheet": sheet,
            "raw_total": raw_total,
            "valid_total": valid_total,
            "invalid_total": invalid_total,
            "display_issue_total": display_issue_total,
            "duplicate_rows": duplicate_rows,
            "unique_valid_total": unique_valid_total,
            "review_total": review_total,
            "valid_rate": round(_ratio(valid_total, raw_total), 6),
            "unique_valid_rate": round(_ratio(unique_valid_total, raw_total), 6),
            "duplicate_rate_valid": round(_ratio(duplicate_valid_rows, valid_total), 6),
            "review_rate_raw": round(_ratio(review_total, raw_total), 6),
        }

    summary: list[dict[str, Any]] = [summarize("overall", "", "", rows)]
    for (platform, sheet), group in sorted(groups.items(), key=lambda item: (item[0][0].casefold(), item[0][1].casefold())):
        summary.append(summarize("source", platform, sheet, group))
    return summary


def preparation_digest(records: Iterable[PreparedRecord]) -> str:
    """Digest only stable preparation fields; classification cannot invalidate it."""

    canonical = "\n".join(
        f"{record.record_id}\x1f{record.source_file}\x1f{record.source_sheet}"
        f"\x1f{record.source_row}\x1f{record.platform}\x1f{record.text_normalized}"
        f"\x1f{record.text_fingerprint}\x1f{record.quality_label}\x1f{record.duplicate_group}"
        for record in records
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def quality_reasons(records: Iterable[PreparedRecord]) -> dict[str, int]:
    return dict(sorted(Counter(record.quality_reason for record in records if record.quality_reason).items()))
