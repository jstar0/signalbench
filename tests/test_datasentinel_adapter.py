from __future__ import annotations

from signalbench.adapters.datasentinel import compare_structure, validate_structure
from signalbench.models import SourceRecord


def _record(record_id: str, text: str, *, platform: str = "应用内") -> SourceRecord:
    return SourceRecord(record_id, "fixture.csv", "评论", int(record_id), platform, "", text)


def test_structure_adapter_uses_datasentinel_for_explicit_fields_only() -> None:
    report = validate_structure("candidate", [_record("1", "搜索报错"), _record("2", "价格太高")])
    assert report.valid
    assert report.row_count == 2
    assert "text_raw" not in {issue.column for issue in report.issues}


def test_empty_reference_distribution_is_incomparable_not_zero_drift() -> None:
    drift = compare_structure("empty", [], "candidate", [_record("1", "搜索报错")])
    metrics = {metric.column: metric for metric in drift.metrics}
    assert metrics["text_length"].comparable is False
    assert metrics["text_length"].distribution_score is None
    assert metrics["text_length"].reason == "reference_empty"
