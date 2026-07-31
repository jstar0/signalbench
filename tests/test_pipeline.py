from __future__ import annotations

import json
from pathlib import Path

import pytest

from signalbench.cli import main
from signalbench.io import InputContractError, read_prepared_csv
from signalbench.pipeline import prepare_pipeline, run_pipeline


def test_run_pipeline_writes_verifiable_artifacts(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "examples" / "comments.csv"
    output = tmp_path / "outputs"
    result = run_pipeline([source], output_dir=output, batch_size=2)
    assert result["record_count"] == 10
    assert result["classified_count"] == 7
    assert (output / "prepared.csv").exists()
    assert (output / "prepared.xlsx").exists()
    assert (output / "classified.csv").exists()
    assert (output / "classified.xlsx").exists()
    assert (output / "checkpoint.json").exists()
    assert (output / "report" / "report.html").exists()
    report = json.loads((output / "report" / "report.json").read_text(encoding="utf-8"))
    assert report["schema_version"] == 1
    assert report["quality"][0]["raw_total"] == 10
    assert report["classification"][0]["classified_total"] == 7


def test_cli_prepare_and_summarize(tmp_path: Path, capsys) -> None:
    source = Path(__file__).resolve().parents[1] / "examples" / "comments.csv"
    output = tmp_path / "outputs"
    main(["prepare", str(source), "--output-dir", str(output)])
    prepare_stdout = capsys.readouterr().out
    assert "prepared.csv" in prepare_stdout
    main(["classify", "--prepared", str(output / "prepared.csv"), "--output-dir", str(output)])
    capsys.readouterr()
    main(["summarize", "--classified", str(output / "classified.csv")])
    summarize_stdout = capsys.readouterr().out
    assert "report.html" in summarize_stdout


def test_edited_quality_label_is_rejected_rather_than_classified(tmp_path: Path) -> None:
    """The gate is recomputed on read, not trusted from the file.

    Verifying only the text fingerprint let a hand-edited row flip `invalid` to
    `valid` and reach the classifier, defeating the quality gate entirely.
    """
    source = tmp_path / "comments.csv"
    source.write_text(
        "text\nA genuinely useful and clear explanation here\nhttps://example.com/only-a-link\n",
        encoding="utf-8",
    )
    output = tmp_path / "out"
    prepare_pipeline([source], output_dir=output)

    prepared = output / "prepared.csv"
    rows = prepared.read_text(encoding="utf-8")
    assert "link_only" in rows
    tampered = rows.replace("invalid,link_only", "valid,").replace("not_applicable", "pending")
    prepared.write_text(tampered, encoding="utf-8")

    with pytest.raises(InputContractError, match="does not match its text"):
        read_prepared_csv(prepared, None)


def test_shared_report_does_not_leak_a_filename_when_platform_is_missing(tmp_path: Path) -> None:
    source = tmp_path / "AcmeCorp_Q3_churn.csv"
    source.write_text("text\nA genuinely useful and clear explanation here\n", encoding="utf-8")
    output = tmp_path / "out"

    run_pipeline([source], output_dir=output)

    report = (output / "report" / "report.html").read_text(encoding="utf-8")
    assert "AcmeCorp" not in report
    assert "source-" in report
