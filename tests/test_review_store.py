from __future__ import annotations

from pathlib import Path

import pytest

from signalbench.models import SourceRecord
from signalbench.quality import prepare_records
from signalbench.review_store import (
    ReviewConflict,
    ReviewStore,
    ReviewStoreError,
    ReviewUpdate,
)


def _prepared():
    return prepare_records(
        [
            SourceRecord(
                "one", "input.csv", "__csv__", 2, "网页", "", "一段有价值的反馈"
            ),
            SourceRecord(
                "two", "input.csv", "__csv__", 3, "网页", "", "https://example.com"
            ),
            SourceRecord("three", "input.csv", "__csv__", 4, "网页", "", "  "),
        ]
    )


def test_review_history_is_separate_from_source_rows_and_exclusion_changes_effective_total(
    tmp_path: Path,
) -> None:
    records = _prepared()
    store = ReviewStore(tmp_path / "reviews.sqlite")
    digest = store.create_batch("batch-1", records)
    assert digest
    assert [item.effective_include for item in store.effective_reviews("batch-1")] == [
        True,
        False,
        False,
    ]

    revision = store.apply(
        "batch-1",
        [ReviewUpdate("one", include_override="exclude", reason="与当前批次无关")],
        expected_review_revision=0,
    )
    assert revision == 1
    effective = {item.record_id: item for item in store.effective_reviews("batch-1")}
    assert effective["one"].effective_include is False
    assert effective["two"].effective_include is False
    assert records[0].quality_label == "valid"
    assert records[0].text_raw == "一段有价值的反馈"


def test_review_prevents_hard_quality_override_and_stale_writes(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "reviews.sqlite")
    store.create_batch("batch-1", _prepared())
    with pytest.raises(ReviewStoreError, match="hard quality exclusion"):
        store.apply(
            "batch-1",
            [
                ReviewUpdate(
                    "three", include_override="include", reason="尝试纳入空文本"
                )
            ],
            expected_review_revision=0,
        )


def test_soft_quality_exclusion_can_be_included_only_with_a_reason(
    tmp_path: Path,
) -> None:
    store = ReviewStore(tmp_path / "reviews.sqlite")
    store.create_batch("batch-1", _prepared())
    revision = store.apply(
        "batch-1",
        [
            ReviewUpdate(
                "two", include_override="include", reason="链接文字是用户给出的复现入口"
            )
        ],
        expected_review_revision=0,
    )
    assert revision == 1
    assert {
        item.record_id: item.effective_include
        for item in store.effective_reviews("batch-1")
    }["two"]

    store.apply(
        "batch-1",
        [ReviewUpdate("one", topic_override="reliability", reason="用户明确描述错误")],
        expected_review_revision=1,
    )
    with pytest.raises(ReviewConflict, match="changed"):
        store.apply(
            "batch-1",
            [ReviewUpdate("one", topic_override="support", reason="旧表单")],
            expected_review_revision=0,
        )


def test_keep_first_duplicates_is_atomic_above_manual_batch_limit_and_preserves_labels(
    tmp_path: Path,
) -> None:
    text = "这是同一条足够长的重复反馈内容，用于验证批量复核不会截断。"
    records = prepare_records(
        [
            SourceRecord(
                f"duplicate-{index}",
                "input.csv",
                "__csv__",
                index + 2,
                "网页",
                "",
                text,
            )
            for index in range(620)
        ]
    )
    store = ReviewStore(tmp_path / "reviews.sqlite")
    store.create_batch("batch-1", records)

    first_revision = store.apply(
        "batch-1",
        [
            ReviewUpdate(
                "duplicate-1",
                sentiment_override="negative",
                reason="人工确认该原文语气为负向",
            )
        ],
        expected_review_revision=0,
    )
    assert first_revision == 1
    revision, updated_count = store.keep_first_duplicates(
        "batch-1", expected_review_revision=1
    )

    assert (revision, updated_count) == (2, 619)
    effective = {item.record_id: item for item in store.effective_reviews("batch-1")}
    assert effective["duplicate-0"].effective_include is True
    assert effective["duplicate-1"].effective_include is False
    assert effective["duplicate-1"].sentiment_override == "negative"
    assert store.keep_first_duplicates("batch-1", expected_review_revision=2) == (2, 0)


def test_independent_review_actions_preserve_other_effective_overrides(
    tmp_path: Path,
) -> None:
    store = ReviewStore(tmp_path / "reviews.sqlite")
    store.create_batch("batch-1", _prepared())
    store.apply(
        "batch-1",
        [ReviewUpdate("one", include_override="exclude", reason="仅供客服问题复盘")],
        expected_review_revision=0,
    )
    store.apply(
        "batch-1",
        [
            ReviewUpdate(
                "one", sentiment_override="negative", reason="复核后确定是负向反馈"
            )
        ],
        expected_review_revision=1,
    )

    review = {item.record_id: item for item in store.effective_reviews("batch-1")}[
        "one"
    ]
    assert review.effective_include is False
    assert review.include_override == "exclude"
    assert review.sentiment_override == "negative"
