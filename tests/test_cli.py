from __future__ import annotations

import json
from pathlib import Path

from signalbench.cli import main


def test_demo_command_uses_bundled_fixture(tmp_path: Path, capsys) -> None:
    output_dir = tmp_path / "demo"

    main(["demo", "--output-dir", str(output_dir)])

    payload = json.loads(capsys.readouterr().out)
    assert payload["demo"] is True
    assert payload["raw_rows"] == 10
    assert payload["valid_rows"] == 6
    assert payload["classified_rows"] == 6
    assert Path(payload["report"]).exists()


def test_benchmark_command_writes_metrics_and_summary(tmp_path: Path, capsys) -> None:
    main(["benchmark", "--output-dir", str(tmp_path / "benchmark")])

    payload = json.loads(capsys.readouterr().out)
    assert payload["examples"] == 45
    assert payload["accuracy"] >= 0.7
    assert Path(payload["metrics"]).exists()
    assert Path(payload["summary"]).exists()
