from __future__ import annotations

import json
from pathlib import Path

from signalbench.classify import DeterministicClassifier
from signalbench.cli import main
from signalbench.evaluation import evaluate_gold_file, write_benchmark_artifacts


def test_gold_evaluation_reports_accuracy_and_confusion(tmp_path: Path) -> None:
    gold = Path(__file__).resolve().parents[1] / "examples" / "gold.csv"
    output = tmp_path / "metrics.json"
    payload = evaluate_gold_file(gold, classifier=DeterministicClassifier(), output_path=output)
    assert payload["examples"] == 12
    assert payload["accuracy"] >= 0.8
    assert set(payload["confusion"]) == {"positive", "neutral", "negative"}
    assert json.loads(output.read_text(encoding="utf-8"))["fixture_sha256"]
    assert payload["actual_distribution"]["positive"]["support"] == 2
    assert len(payload["confidence_calibration"]["bins"]) == 4
    assert 0 <= payload["confidence_calibration"]["ece"] <= 1


def test_evaluate_cli_writes_metrics(tmp_path: Path, capsys) -> None:
    gold = Path(__file__).resolve().parents[1] / "examples" / "gold.csv"
    output = tmp_path / "metrics.json"
    main(["evaluate", "--gold", str(gold), "--output", str(output)])
    capsys.readouterr()
    assert output.exists()


def test_benchmark_artifacts_are_reviewable_and_hash_bound(tmp_path: Path) -> None:
    fixture = Path(__file__).resolve().parents[1] / "examples" / "benchmark.csv"
    result = write_benchmark_artifacts(
        fixture,
        output_dir=tmp_path / "benchmark",
        classifier=DeterministicClassifier(),
    )
    metrics = json.loads(Path(result["metrics"]).read_text(encoding="utf-8"))
    summary = Path(result["summary"]).read_text(encoding="utf-8")
    assert metrics["benchmark"]["fixture"] == "benchmark.csv"
    assert metrics["examples"] == 45
    assert metrics["actual_distribution"]["positive"]["support"] == 15
    assert "Expected calibration error" in summary
    assert metrics["fixture_sha256"] == result["fixture_sha256"]
