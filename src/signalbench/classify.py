from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from .config import DEFAULT_CONFIG
from .io import write_json
from .models import (
    NEGATIVE_SUBTYPES,
    SENTIMENT_LABELS,
    ClassificationResult,
    PreparedRecord,
)
from .quality import preparation_digest


class Classifier(Protocol):
    name: str

    def classify(self, records: Sequence[PreparedRecord]) -> list[ClassificationResult]:
        ...


class CheckpointMismatch(ValueError):
    """Raised when a checkpoint belongs to another prepared input."""


class InjectedFailure(RuntimeError):
    """Used by tests and demos to prove that resume works."""


class ClassificationPaused(RuntimeError):
    """Raised after a completed batch when a local job was paused."""


class ClassificationCancelled(RuntimeError):
    """Raised after a completed batch when a local job was cancelled."""


class TransientClassificationError(RuntimeError):
    """An adapter's explicitly retryable, temporary classification failure."""


_ASCII_WORD_RE = re.compile(r"(?<![A-Za-z0-9_]){term}(?![A-Za-z0-9_])", re.IGNORECASE)


def _term_present(text: str, term: str) -> bool:
    term = str(term).casefold().strip()
    if not term:
        return False
    # Word boundaries are useful for English ("good" should not match
    # "goodbye"), while direct containment is needed for CJK text.
    if any(ord(char) > 127 for char in term):
        return term in text
    pattern = _ASCII_WORD_RE.pattern.format(term=re.escape(term))
    return bool(re.search(pattern, text, flags=re.IGNORECASE))


def _matching_terms(text: str, weighted_terms: Mapping[str, float]) -> list[tuple[str, float]]:
    return [(term, float(weight)) for term, weight in weighted_terms.items() if _term_present(text, term)]


class DeterministicClassifier:
    """A transparent baseline classifier with stable outputs and no network calls."""

    name = "deterministic-v1"

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        classification = (config or DEFAULT_CONFIG)["classification"]
        self.positive = {str(k): float(v) for k, v in classification["positive"].items()}
        self.negative = {str(k): float(v) for k, v in classification["negative"].items()}
        self.topics = {
            str(topic): tuple(str(term) for term in terms)
            for topic, terms in classification["topics"].items()
        }
        self.subtypes = {
            str(subtype): tuple(str(term) for term in terms)
            for subtype, terms in classification["negative_subtypes"].items()
        }
        self.meta_feedback = tuple(str(term) for term in classification.get("meta_feedback", ()))
        self.positive_threshold = float(classification.get("positive_threshold", 0.15))
        self.negative_threshold = float(classification.get("negative_threshold", -0.15))

    def classify(self, records: Sequence[PreparedRecord]) -> list[ClassificationResult]:
        results: list[ClassificationResult] = []
        for record in records:
            text = record.text_normalized.casefold()
            positive_hits = _matching_terms(text, self.positive)
            negative_hits = _matching_terms(text, self.negative)
            topic_counts = self._topic_counts(text)
            topic_hits = [
                f"topic:{topic}:{term}"
                for topic, terms in self.topics.items()
                for term in terms
                if _term_present(text, term)
            ]
            rule_hits = tuple(
                [*(f"positive:{term}" for term, _weight in positive_hits)]
                + [*(f"negative:{term}" for term, _weight in negative_hits)]
                + topic_hits
            )
            raw_score = sum(weight for _term, weight in positive_hits) + sum(
                weight for _term, weight in negative_hits
            )
            score = max(-1.0, min(1.0, math.tanh(raw_score / 1.35)))
            if score >= self.positive_threshold:
                label = "positive"
            elif score <= self.negative_threshold:
                label = "negative"
            else:
                label = "neutral"

            conflict = bool(positive_hits and negative_hits)
            if conflict:
                # In product feedback, a concrete configured failure alongside
                # praise is treated as the dominant complaint and still routed
                # to review. This is a transparent policy, not sentiment
                # understanding.
                label = "negative"
                score = min(score, self.negative_threshold)
            question_mark = "?" in record.text_normalized or "？" in record.text_normalized
            meta_only = (
                bool(positive_hits)
                and not negative_hits
                and not any(topic_counts.values())
                and any(_term_present(text, term) for term in self.meta_feedback)
            )
            if meta_only:
                label = "neutral"
                score = 0.0
            review_flag = conflict or question_mark
            evidence_count = len(positive_hits) + len(negative_hits)
            confidence = 0.42 + min(0.30, 0.10 * evidence_count) + 0.24 * abs(score)
            if conflict:
                confidence *= 0.62
            if question_mark:
                confidence *= 0.82
            if meta_only:
                confidence *= 0.75
            confidence = max(0.0, min(0.99, confidence))
            topic = self._choose_topic(text)
            subtype = self._choose_subtype(text) if label == "negative" else ""
            rationale = self._rationale(
                label,
                positive_hits,
                negative_hits,
                conflict,
                question_mark,
                meta_only,
            )
            results.append(
                ClassificationResult(
                    record_id=record.record_id,
                    sentiment_label=label,
                    sentiment_score=score,
                    confidence=confidence,
                    topic=topic,
                    negative_subtype=subtype,
                    review_flag=review_flag,
                    decision_note=rationale,
                    rule_hits=rule_hits,
                )
            )
        return results

    def _choose_topic(self, text: str) -> str:
        counts = self._topic_counts(text)
        positive = [(count, topic) for topic, count in counts.items() if count]
        return max(positive, key=lambda item: (item[0], item[1]))[1] if positive else "general"

    def _topic_counts(self, text: str) -> dict[str, int]:
        return {
            topic: sum(_term_present(text, term) for term in terms)
            for topic, terms in self.topics.items()
        }

    def _choose_subtype(self, text: str) -> str:
        priority = ("sarcasm_attack", "distrust_suspicion", "risk_concern")
        for subtype in priority:
            if any(_term_present(text, term) for term in self.subtypes.get(subtype, ())):
                return subtype
        return "criticism_blame"

    @staticmethod
    def _rationale(
        label: str,
        positive_hits: Sequence[tuple[str, float]],
        negative_hits: Sequence[tuple[str, float]],
        conflict: bool,
        question_mark: bool,
        meta_only: bool,
    ) -> str:
        if conflict:
            return "conflicting sentiment cues"
        if question_mark:
            return "question needs review"
        if meta_only:
            return "meta feedback, no topic stance"
        hits = positive_hits if label == "positive" else negative_hits
        if hits:
            return f"matched {hits[0][0]}"
        return "no strong sentiment cue"


class ReplayClassifier:
    """Offline adapter for replaying a cleared model run by stable record ID.

    Replay files intentionally contain decisions, not source text.  This gives
    a remote/model-backed workflow a deterministic, auditable representation
    for tests and reports without putting credentials or prompts in the repo.
    """

    name = "replay-v1"

    def __init__(self, path: Path) -> None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot read replay file {path}: {exc}") from exc
        raw_results = payload.get("results") if isinstance(payload, dict) else None
        if raw_results is None and isinstance(payload, dict):
            raw_results = payload
        if not isinstance(raw_results, dict):
            raise TypeError("Replay file must contain a JSON object of results.")
        self.results: dict[str, ClassificationResult] = {}
        for record_id, raw_result in raw_results.items():
            if not isinstance(raw_result, dict):
                raise TypeError(f"Replay result for {record_id} must be an object.")
            self.results[str(record_id)] = _result_from_payload(str(record_id), raw_result)

    def classify(self, records: Sequence[PreparedRecord]) -> list[ClassificationResult]:
        missing = [record.record_id for record in records if record.record_id not in self.results]
        if missing:
            raise ValueError(f"Replay file is missing result(s): {', '.join(missing)}")
        return [self.results[record.record_id] for record in records]

    def validate_ids(self, record_ids: Sequence[str]) -> None:
        expected = set(record_ids)
        actual = set(self.results)
        missing = expected - actual
        extra = actual - expected
        if missing or extra:
            raise ValueError(
                "Replay IDs do not exactly match prepared valid rows; "
                f"missing={sorted(missing)}, extra={sorted(extra)}"
            )


def _result_payload(result: ClassificationResult, *, attempts: int) -> dict[str, Any]:
    return {
        "sentiment_label": result.sentiment_label,
        "sentiment_score": round(float(result.sentiment_score), 6),
        "confidence": round(float(result.confidence), 6),
        "topic": result.topic,
        "negative_subtype": result.negative_subtype,
        "review_flag": bool(result.review_flag),
        "decision_note": result.decision_note,
        "rule_hits": list(result.rule_hits),
        "attempts": int(attempts),
    }


def _result_from_payload(record_id: str, payload: Mapping[str, Any]) -> ClassificationResult:
    if not isinstance(payload, Mapping):
        raise CheckpointMismatch(f"Checkpoint result for {record_id} must be an object.")
    label = str(payload.get("sentiment_label", ""))
    if label not in SENTIMENT_LABELS:
        raise CheckpointMismatch(f"Invalid sentiment label in checkpoint for {record_id}.")
    subtype = str(payload.get("negative_subtype", ""))
    if label == "negative" and subtype not in NEGATIVE_SUBTYPES:
        raise CheckpointMismatch(f"Invalid negative subtype in checkpoint for {record_id}.")
    try:
        score = float(payload.get("sentiment_score", 0.0))
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError) as exc:
        raise CheckpointMismatch(f"Non-numeric score in checkpoint for {record_id}.") from exc
    if not math.isfinite(score) or not math.isfinite(confidence):
        raise CheckpointMismatch(f"Non-finite score in checkpoint for {record_id}.")
    raw_rule_hits = payload.get("rule_hits", ())
    rule_hits = (
        tuple(str(item) for item in raw_rule_hits)
        if isinstance(raw_rule_hits, (list, tuple))
        else ()
    )
    return ClassificationResult(
        record_id=record_id,
        sentiment_label=label,
        sentiment_score=score,
        confidence=confidence,
        topic=str(payload.get("topic", "general")),
        negative_subtype=subtype if label == "negative" else "",
        review_flag=bool(payload.get("review_flag", False)),
        decision_note=str(payload.get("decision_note", "")),
        rule_hits=rule_hits,
    )


def _load_checkpoint(
    path: Path,
    expected_digest: str,
    classifier_name: str,
    expected_config_digest: str = "",
) -> dict[str, Any]:
    import json

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckpointMismatch(f"Cannot read checkpoint {path}: {exc}") from exc
    if payload.get("schema_version") != 1:
        raise CheckpointMismatch("Unsupported checkpoint schema version.")
    if payload.get("input_digest") != expected_digest:
        raise CheckpointMismatch("Checkpoint input digest does not match prepared data.")
    if payload.get("classifier") != classifier_name:
        raise CheckpointMismatch("Checkpoint classifier does not match the requested classifier.")
    if expected_config_digest and payload.get("config_sha256") != expected_config_digest:
        raise CheckpointMismatch("Checkpoint configuration digest does not match the requested rules.")
    if not isinstance(payload.get("completed"), dict):
        raise CheckpointMismatch("Checkpoint completed field must be an object.")
    return payload


def _write_checkpoint(
    path: Path,
    input_digest: str,
    classifier_name: str,
    completed: Mapping[str, Any],
    config_digest: str = "",
) -> None:
    write_json(
        path,
        {
            "schema_version": 1,
            "input_digest": input_digest,
            "classifier": classifier_name,
            "config_sha256": config_digest,
            "completed": dict(sorted(completed.items())),
        },
    )


def _chunks(items: Sequence[PreparedRecord], size: int) -> Iterable[list[PreparedRecord]]:
    if size < 1:
        raise ValueError("batch_size must be at least 1")
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def _classify_with_retry(
    classifier: Classifier,
    batch: Sequence[PreparedRecord],
    retries: int,
    backoff_seconds: float,
) -> tuple[list[ClassificationResult], int]:
    if retries < 0:
        raise ValueError("retries must be non-negative")
    attempts = 0
    while True:
        attempts += 1
        try:
            return classifier.classify(batch), attempts
        except (TransientClassificationError, TimeoutError, ConnectionError, OSError):
            if attempts > retries:
                raise
            if backoff_seconds > 0:
                time.sleep(min(backoff_seconds * (2 ** (attempts - 1)), 30.0))


def _apply_result(record: PreparedRecord, result: ClassificationResult, attempts: int) -> None:
    if result.sentiment_label not in SENTIMENT_LABELS:
        raise ValueError(f"Unsupported sentiment label for {record.record_id}: {result.sentiment_label}")
    if result.sentiment_label == "negative" and result.negative_subtype not in NEGATIVE_SUBTYPES:
        raise ValueError(f"Unsupported negative subtype for {record.record_id}: {result.negative_subtype}")
    if not math.isfinite(float(result.sentiment_score)) or not math.isfinite(float(result.confidence)):
        raise ValueError(f"Non-finite score for {record.record_id}")
    record.sentiment_label = result.sentiment_label
    record.sentiment_score = max(-1.0, min(1.0, float(result.sentiment_score)))
    record.confidence = max(0.0, min(0.99, float(result.confidence)))
    record.topic = result.topic or "general"
    record.negative_subtype = result.negative_subtype if result.sentiment_label == "negative" else ""
    record.review_flag = record.review_flag or result.review_flag
    record.decision_note = result.decision_note
    record.rule_hits = result.rule_hits
    record.classification_status = "classified"
    record.attempts += attempts


def classify_records(
    records: Iterable[PreparedRecord],
    classifier: Classifier,
    *,
    batch_size: int = 32,
    retries: int = 2,
    backoff_seconds: float = 0.0,
    checkpoint_path: Path | None = None,
    fail_after_batches: int | None = None,
    config_digest: str = "",
    input_digest_override: str | None = None,
    after_batch: Callable[[int, int], str] | None = None,
    eligible_record_ids: set[str] | None = None,
) -> list[PreparedRecord]:
    """Classify valid rows with atomic, digest-bound resume support."""

    prepared = list(records)
    if retries < 0:
        raise ValueError("retries must be non-negative")
    if fail_after_batches is not None and fail_after_batches < 1:
        raise ValueError("fail_after_batches must be at least 1")
    digest = input_digest_override or preparation_digest(prepared)
    eligible = (
        {record.record_id for record in prepared if record.quality_label == "valid"}
        if eligible_record_ids is None
        else set(eligible_record_ids)
    )
    known_ids = {record.record_id for record in prepared}
    if not eligible <= known_ids:
        raise ValueError(f"Eligible IDs are absent from prepared data: {sorted(eligible - known_ids)}")
    valid_ids = [record.record_id for record in prepared if record.record_id in eligible]
    validate_ids = getattr(classifier, "validate_ids", None)
    if callable(validate_ids):
        validate_ids(valid_ids)
    completed: dict[str, Any] = {}
    if checkpoint_path and checkpoint_path.exists():
        checkpoint = _load_checkpoint(
            checkpoint_path,
            digest,
            classifier.name,
            expected_config_digest=config_digest,
        )
        completed = dict(checkpoint["completed"])
        unknown = set(completed) - known_ids
        if unknown:
            raise CheckpointMismatch("Checkpoint contains records absent from prepared data.")
        invalid_ids = known_ids - eligible
        if set(completed) & invalid_ids:
            raise CheckpointMismatch("Checkpoint contains a decision for a non-valid record.")
        for record in prepared:
            payload = completed.get(record.record_id)
            if payload is not None and record.record_id in eligible:
                if not isinstance(payload, Mapping):
                    raise CheckpointMismatch(
                        f"Checkpoint result for {record.record_id} must be an object."
                    )
                _apply_result(
                    record,
                    _result_from_payload(record.record_id, payload),
                    attempts=max(0, int(payload.get("attempts", 1))),
                )

    pending = [
        record
        for record in prepared
        if record.record_id in eligible and record.classification_status != "classified"
    ]
    batches = list(_chunks(pending, batch_size))
    for finished_batches, batch in enumerate(batches, start=1):
        results, attempts = _classify_with_retry(
            classifier,
            batch,
            retries=retries,
            backoff_seconds=backoff_seconds,
        )
        if len(results) != len(batch):
            raise ValueError(
                f"Classifier returned {len(results)} result(s) for a batch of {len(batch)}."
            )
        result_map = {result.record_id: result for result in results}
        expected_ids = {record.record_id for record in batch}
        if set(result_map) != expected_ids:
            missing = expected_ids - set(result_map)
            extra = set(result_map) - expected_ids
            raise ValueError(f"Classifier result mismatch; missing={sorted(missing)}, extra={sorted(extra)}")
        for record in batch:
            result = result_map[record.record_id]
            _apply_result(record, result, attempts=attempts)
            completed[record.record_id] = _result_payload(result, attempts=record.attempts)
        if checkpoint_path:
            _write_checkpoint(
                checkpoint_path,
                digest,
                classifier.name,
                completed,
                config_digest=config_digest,
            )
        if after_batch is not None:
            # `completed` includes resumed checkpoint records, so persisted job
            # progress always describes the full input rather than this worker
            # invocation's remaining subset.
            action = after_batch(len(completed), len(valid_ids))
            if action == "pause":
                raise ClassificationPaused(f"Paused after {finished_batches} completed batch(es).")
            if action == "cancel":
                raise ClassificationCancelled(f"Cancelled after {finished_batches} completed batch(es).")
            if action != "continue":
                raise ValueError(f"Unsupported batch action: {action!r}")
        if fail_after_batches is not None and finished_batches >= fail_after_batches:
            raise InjectedFailure(f"Injected failure after {finished_batches} batch(es).")

    if checkpoint_path and not checkpoint_path.exists():
        _write_checkpoint(
            checkpoint_path,
            digest,
            classifier.name,
            completed,
            config_digest=config_digest,
        )
    return prepared


def classification_summary(records: Iterable[PreparedRecord]) -> list[dict[str, Any]]:
    """Compute sentiment metrics by source and overall, including review coverage."""

    rows = list(records)
    groups: dict[tuple[str, str], list[PreparedRecord]] = {}
    for record in rows:
        groups.setdefault((record.platform, record.source_sheet), []).append(record)

    def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
        if total == 0:
            return 0.0, 0.0
        p = successes / total
        denominator = 1 + z * z / total
        centre = (p + z * z / (2 * total)) / denominator
        margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
        return max(0.0, centre - margin), min(1.0, centre + margin)

    def summarize(scope: str, platform: str, sheet: str, group: list[PreparedRecord]) -> dict[str, Any]:
        classified = [item for item in group if item.classification_status == "classified"]
        counts = Counter(item.sentiment_label for item in classified)
        unknown_labels = set(counts) - set(SENTIMENT_LABELS)
        if unknown_labels:
            raise ValueError(f"Unsupported sentiment label(s) in classified data: {sorted(unknown_labels)}")
        total = len(classified)
        negative = counts.get("negative", 0)
        ci_low, ci_high = wilson(negative, total)
        confidence_values = [item.confidence for item in classified]
        return {
            "scope": scope,
            "platform": platform,
            "source_sheet": sheet,
            "classified_total": total,
            "positive_total": counts.get("positive", 0),
            "neutral_total": counts.get("neutral", 0),
            "negative_total": negative,
            "positive_rate": round(counts.get("positive", 0) / total, 6) if total else 0.0,
            "neutral_rate": round(counts.get("neutral", 0) / total, 6) if total else 0.0,
            "negative_rate": round(negative / total, 6) if total else 0.0,
            "negative_rate_ci95_low": round(ci_low, 6),
            "negative_rate_ci95_high": round(ci_high, 6),
            "review_total": sum(item.review_flag for item in classified),
            "review_rate": round(
                sum(item.review_flag for item in classified) / total, 6
            )
            if total
            else 0.0,
            "mean_confidence": round(sum(confidence_values) / len(confidence_values), 6)
            if confidence_values
            else 0.0,
            "topic_counts": dict(
                sorted(Counter(item.topic or "general" for item in classified).items())
            ),
        }

    summary = [summarize("overall", "", "", rows)]
    for (platform, sheet), group in sorted(groups.items(), key=lambda item: (item[0][0].casefold(), item[0][1].casefold())):
        summary.append(summarize("source", platform, sheet, group))
    return summary
