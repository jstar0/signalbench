from __future__ import annotations

import json
from pathlib import Path

import pytest

from signalbench.classify import (
    CheckpointMismatch,
    ClassificationCancelled,
    ClassificationPaused,
    DeterministicClassifier,
    InjectedFailure,
    ReplayClassifier,
    TransientClassificationError,
    classify_records,
)
from signalbench.models import ClassificationResult, PreparedRecord, SourceRecord
from signalbench.quality import prepare_records


def _records() -> list[PreparedRecord]:
    source = [
        SourceRecord("1", "fixture.csv", "__csv__", 2, "Forum", "", "Great and helpful"),
        SourceRecord("2", "fixture.csv", "__csv__", 3, "Forum", "", "The price is too high"),
        SourceRecord("3", "fixture.csv", "__csv__", 4, "Forum", "", "I like it, but is it safe?"),
        SourceRecord("4", "fixture.csv", "__csv__", 5, "Forum", "", "0"),
    ]
    return prepare_records(source)


def test_deterministic_classifier_is_explainable() -> None:
    records = _records()
    results = DeterministicClassifier().classify(records[:3])
    assert [result.sentiment_label for result in results] == ["positive", "negative", "positive"]
    assert results[1].topic == "pricing"
    assert "negative:high" in results[1].rule_hits
    assert "topic:pricing:price" in results[1].rule_hits
    assert results[2].review_flag is True
    assert results[2].decision_note == "question needs review"


def test_meta_feedback_without_topic_stance_is_neutral() -> None:
    record = prepare_records(
        [SourceRecord("meta", "fixture.csv", "__csv__", 2, "Forum", "", "Great explanation")]
    )[0]
    result = DeterministicClassifier().classify([record])[0]
    assert result.sentiment_label == "neutral"
    assert result.decision_note == "meta feedback, no topic stance"


class _FlakyClassifier:
    name = "flaky-test"

    def __init__(self) -> None:
        self.calls = 0

    def classify(self, records):
        self.calls += 1
        if self.calls == 1:
            raise TransientClassificationError("temporary")
        return [
            ClassificationResult(
                record_id=record.record_id,
                sentiment_label="neutral",
                sentiment_score=0,
                confidence=0.5,
                topic="general",
                negative_subtype="",
                review_flag=False,
                decision_note="test",
            )
            for record in records
        ]


def test_retry_then_success() -> None:
    classifier = _FlakyClassifier()
    result = classify_records(_records(), classifier, retries=1, backoff_seconds=0)
    assert classifier.calls == 2
    assert all(record.classification_status == "classified" for record in result if record.quality_label == "valid")
    assert all(record.attempts == 2 for record in result if record.quality_label == "valid")


def test_contract_error_is_not_retried_as_a_temporary_failure() -> None:
    class BrokenClassifier:
        name = "broken-test"

        def __init__(self) -> None:
            self.calls = 0

        def classify(self, _records):
            self.calls += 1
            raise TypeError("adapter bug")

    classifier = BrokenClassifier()
    with pytest.raises(TypeError, match="adapter bug"):
        classify_records(_records(), classifier, retries=2, backoff_seconds=0)
    assert classifier.calls == 1


def test_checkpoint_resume_only_calls_remaining_batches(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint.json"
    with pytest.raises(InjectedFailure):
        classify_records(
            _records(),
            DeterministicClassifier(),
            batch_size=1,
            checkpoint_path=checkpoint,
            fail_after_batches=1,
        )
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert len(payload["completed"]) == 1

    class CountingClassifier(DeterministicClassifier):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def classify(self, records):
            self.calls += 1
            return super().classify(records)

    classifier = CountingClassifier()
    result = classify_records(
        _records(), classifier, batch_size=1, checkpoint_path=checkpoint
    )
    assert classifier.calls == 2  # three valid rows total; one was checkpointed
    assert all(record.classification_status != "pending" for record in result)


def test_checkpoint_mismatch_is_rejected(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint.json"
    classify_records(_records(), DeterministicClassifier(), checkpoint_path=checkpoint)
    changed = _records()
    changed[0].text_normalized = "different evidence"
    with pytest.raises(CheckpointMismatch, match="digest"):
        classify_records(changed, DeterministicClassifier(), checkpoint_path=checkpoint)

    changed = _records()
    changed[0].platform = "another-source"
    with pytest.raises(CheckpointMismatch, match="digest"):
        classify_records(changed, DeterministicClassifier(), checkpoint_path=checkpoint)


def test_checkpoint_rules_digest_is_bound(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint.json"
    classify_records(
        _records(),
        DeterministicClassifier(),
        checkpoint_path=checkpoint,
        config_digest="rules-a",
    )
    with pytest.raises(CheckpointMismatch, match="configuration digest"):
        classify_records(
            _records(),
            DeterministicClassifier(),
            checkpoint_path=checkpoint,
            config_digest="rules-b",
        )


def test_pause_and_cancel_take_effect_only_after_a_checkpointed_batch(tmp_path: Path) -> None:
    paused_checkpoint = tmp_path / "paused.json"
    with pytest.raises(ClassificationPaused, match="after 1 completed"):
        classify_records(
            _records(),
            DeterministicClassifier(),
            batch_size=1,
            checkpoint_path=paused_checkpoint,
            after_batch=lambda _done, _total: "pause",
        )
    assert len(json.loads(paused_checkpoint.read_text(encoding="utf-8"))["completed"]) == 1
    resumed = classify_records(_records(), DeterministicClassifier(), batch_size=1, checkpoint_path=paused_checkpoint)
    assert sum(item.classification_status == "classified" for item in resumed) == 3

    cancelled_checkpoint = tmp_path / "cancelled.json"
    with pytest.raises(ClassificationCancelled, match="after 1 completed"):
        classify_records(
            _records(),
            DeterministicClassifier(),
            batch_size=1,
            checkpoint_path=cancelled_checkpoint,
            after_batch=lambda _done, _total: "cancel",
        )
    assert len(json.loads(cancelled_checkpoint.read_text(encoding="utf-8"))["completed"]) == 1


def test_replay_classifier_is_strict_and_offline(tmp_path: Path) -> None:
    records = _records()
    replay = tmp_path / "replay.json"
    replay.write_text(
        json.dumps(
            {
                "results": {
                    records[0].record_id: {
                        "sentiment_label": "positive",
                        "sentiment_score": 0.8,
                        "confidence": 0.9,
                        "topic": "general",
                        "negative_subtype": "",
                        "review_flag": False,
                        "decision_note": "replayed",
                    },
                    "unexpected": {
                        "sentiment_label": "neutral",
                        "sentiment_score": 0,
                        "confidence": 0.5,
                        "topic": "general",
                        "negative_subtype": "",
                        "review_flag": False,
                        "decision_note": "extra",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    classifier = ReplayClassifier(replay)
    assert classifier.classify([records[0]])[0].decision_note == "replayed"
    with pytest.raises(ValueError, match="missing result"):
        classifier.classify([records[1]])
    with pytest.raises(ValueError, match="extra"):
        classifier.validate_ids([records[0].record_id])
