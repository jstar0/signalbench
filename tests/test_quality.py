from __future__ import annotations

from signalbench.models import SourceRecord
from signalbench.quality import (
    classify_quality,
    normalize_text,
    preparation_digest,
    prepare_records,
    quality_summary,
)


def _source(record_id: str, text: str, sheet: str = "Sheet1") -> SourceRecord:
    return SourceRecord(record_id, "fixture.csv", sheet, int(record_id), "Forum", "", text)


def test_normalize_text_nfkc_and_zero_width() -> None:
    assert normalize_text("  Ｇｏｏｄ\u200b\n work  ") == "Good work"


def test_quality_gate_explains_common_non_evidence() -> None:
    assert classify_quality("   ").reason == "blank"
    assert classify_quality("0").label == "display_issue"
    assert classify_quality("��").reason == "encoding_marker"
    assert classify_quality("https://example.com").reason == "link_only"
    assert classify_quality("😀").reason == "symbol_or_emoji_only"
    assert classify_quality("x").reason == "too_short"
    assert classify_quality("已由平台置頂").reason == "platform_notice"


def test_prepare_records_groups_exact_duplicates_and_preserves_order() -> None:
    records = prepare_records(
        [
            _source("1", "Great explanation"),
            _source("2", " Great   explanation "),
            _source("3", "A useful note"),
            _source("4", "0"),
        ]
    )
    assert [record.record_id for record in records] == ["1", "2", "3", "4"]
    assert [record.duplicate_count for record in records[:2]] == [2, 2]
    assert [record.duplicate_rank for record in records[:2]] == [1, 2]
    assert records[0].duplicate_group == records[1].duplicate_group
    assert records[2].duplicate_group == ""
    assert records[3].classification_status == "not_applicable"


def test_quality_summary_has_overall_and_source_rows() -> None:
    records = prepare_records([_source("1", "Good"), _source("2", "0")])
    summary = quality_summary(records)
    assert [row["scope"] for row in summary] == ["overall", "source"]
    assert summary[0]["raw_total"] == 2
    assert summary[0]["valid_total"] == 1
    assert summary[0]["display_issue_total"] == 1


def test_unique_valid_count_is_not_lost_when_first_duplicate_is_invalid() -> None:
    records = prepare_records(
        [
            _source("1", "0"),
            _source("2", "0"),
            _source("3", "A useful note"),
        ]
    )
    # The two literal-zero rows are display issues; the valid row remains one
    # unique analyzable item regardless of raw duplicate rank.
    summary = quality_summary(records)[0]
    assert summary["unique_valid_total"] == 1


def test_preparation_digest_ignores_classification_fields() -> None:
    records = prepare_records([_source("1", "Good")])
    first = preparation_digest(records)
    records[0].sentiment_label = "positive"
    records[0].classification_status = "classified"
    assert preparation_digest(records) == first
