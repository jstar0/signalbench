"""Aggregate statistics, including the interval the README advertises.

The negative-rate Wilson interval had no test at all while the README claimed
the suite covered it. These tests pin both the arithmetic and the property that
makes the interval easy to misread: duplicates stay in the denominator.
"""

from __future__ import annotations

import math

from signalbench.classify import classification_summary
from signalbench.models import PreparedRecord


def _record(record_id: str, label: str, text: str = "some text", *, duplicate_count: int = 1) -> PreparedRecord:
    return PreparedRecord(
        record_id=record_id,
        source_file="synthetic.csv",
        source_sheet="",
        source_row=1,
        platform="demo",
        author_hash="",
        text_raw=text,
        text_normalized=text,
        text_fingerprint="0" * 16,
        quality_label="valid",
        quality_reason="",
        review_flag=False,
        duplicate_group="dup" if duplicate_count > 1 else "",
        duplicate_rank=1,
        duplicate_count=duplicate_count,
        classification_status="classified",
        sentiment_label=label,
        sentiment_score=-0.5 if label == "negative" else 0.5,
        confidence=0.7,
        topic="general",
    )


def _wilson_reference(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def test_interval_matches_the_wilson_score_formula() -> None:
    records = [_record(f"r{i}", "negative" if i < 3 else "positive") for i in range(10)]
    overall = classification_summary(records)[0]
    low, high = _wilson_reference(3, 10)
    assert overall["negative_total"] == 3
    assert overall["classified_total"] == 10
    assert overall["negative_rate"] == 0.3
    assert overall["negative_rate_ci95_low"] == round(low, 6)
    assert overall["negative_rate_ci95_high"] == round(high, 6)


def test_interval_brackets_the_point_estimate_and_stays_in_range() -> None:
    for negatives in (0, 1, 5, 9, 10):
        records = [
            _record(f"r{i}", "negative" if i < negatives else "positive") for i in range(10)
        ]
        overall = classification_summary(records)[0]
        low = overall["negative_rate_ci95_low"]
        high = overall["negative_rate_ci95_high"]
        assert 0.0 <= low <= overall["negative_rate"] <= high <= 1.0


def test_interval_narrows_as_the_sample_grows() -> None:
    def width(n: int) -> float:
        records = [_record(f"r{i}", "negative" if i % 2 == 0 else "positive") for i in range(n)]
        overall = classification_summary(records)[0]
        return overall["negative_rate_ci95_high"] - overall["negative_rate_ci95_low"]

    assert width(10) > width(100) > width(1000)


def test_empty_input_reports_zeroes_rather_than_dividing_by_zero() -> None:
    overall = classification_summary([])[0]
    assert overall["classified_total"] == 0
    assert overall["negative_rate"] == 0.0
    assert overall["negative_rate_ci95_low"] == 0.0
    assert overall["negative_rate_ci95_high"] == 0.0


def test_duplicates_stay_in_the_denominator_and_narrow_the_interval() -> None:
    """A documented weakness, pinned so nobody mistakes it for a fixed one.

    One complaint copied twenty times is counted twenty times. The interval is
    computed correctly and read wrongly: it assumes independent trials, and the
    duplicate group breaks that assumption.
    """
    spammed = [_record(f"d{i}", "negative", "this is a scam", duplicate_count=20) for i in range(20)]
    distinct = [_record(f"p{i}", "positive", f"a distinct positive comment {i}") for i in range(5)]
    overall = classification_summary(spammed + distinct)[0]

    assert overall["classified_total"] == 25
    assert overall["negative_rate"] == 0.8
    width = overall["negative_rate_ci95_high"] - overall["negative_rate_ci95_low"]
    assert width < 0.35  # as tight as if twenty separate people had complained

    # The same headline from six genuinely distinct rows is far less certain.
    honest = [
        _record(f"u{i}", "negative", f"a distinct complaint {i}") for i in range(4)
    ] + [_record(f"v{i}", "positive", f"a distinct positive {i}") for i in range(1)]
    honest_overall = classification_summary(honest)[0]
    honest_width = (
        honest_overall["negative_rate_ci95_high"] - honest_overall["negative_rate_ci95_low"]
    )
    assert honest_width > width


def test_summary_is_grouped_per_source_and_overall() -> None:
    records = [_record("a", "negative"), _record("b", "positive")]
    rows = classification_summary(records)
    assert rows[0]["scope"] == "overall"
    assert all(row["scope"] == "source" for row in rows[1:])
    assert sum(row["classified_total"] for row in rows[1:]) == rows[0]["classified_total"]
