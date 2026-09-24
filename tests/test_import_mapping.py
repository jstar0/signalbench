from __future__ import annotations

from pathlib import Path

import pytest

from signalbench.adapters.imports import (
    DATE_FORMAT_ISO8601,
    DATE_FORMAT_YMD,
    SheetMapping,
    inspect_inputs,
    load_mapped_records,
)
from signalbench.classify import CheckpointMismatch
from signalbench.io import InputContractError
from signalbench.pipeline import classify_mapped_pipeline, prepare_mapped_pipeline


def _workbook(path: Path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    feedback = workbook.active
    feedback.title = "反馈"
    feedback.append(["编号", "正文", "渠道", "作者", "日期"])
    feedback.append(["a-01", "搜索一直报错", "应用内", "小王", "2026-09-08"])
    feedback.append(["a-02", "价格有点贵", "应用内", "小李", "2026-09-08"])
    notes = workbook.create_sheet("说明")
    notes.append(["字段", "含义"])
    notes.append(["正文", "用户反馈正文"])
    workbook.save(path)
    workbook.close()


def test_explicit_mapping_uses_only_selected_xlsx_sheet_and_keeps_date_metadata(tmp_path: Path) -> None:
    path = tmp_path / "九月反馈.xlsx"
    _workbook(path)
    inspection = inspect_inputs([path])
    assert [table.sheet for table in inspection.tables] == ["反馈", "说明"]
    selected = inspection.tables[0]

    mapped = load_mapped_records(
        [path],
        [
            SheetMapping(
                selected.source_sha256,
                "反馈",
                {"text": "正文", "platform": "渠道", "author": "作者", "record_id": "编号", "created_at": "日期"},
                DATE_FORMAT_YMD,
            )
        ],
    )

    assert [record.source_sheet for record in mapped.records] == ["反馈", "反馈"]
    assert mapped.records[0].text_raw == "搜索一直报错"
    assert mapped.metadata[0].occurred_at == "2026-09-08"
    assert mapped.metadata[0].date_parse_status == "parsed_date"
    assert mapped.manifest["files"][0]["sheets"][1]["selected"] is False


def test_mapping_requires_real_text_column_and_preserves_invalid_date_as_metadata(tmp_path: Path) -> None:
    path = tmp_path / "反馈.csv"
    path.write_text("正文,日期\n一段真实反馈,2026/09/08\n", encoding="utf-8")
    inspected = inspect_inputs([path]).tables[0]

    with pytest.raises(InputContractError, match="requires a text column"):
        load_mapped_records(
            [path],
            [SheetMapping(inspected.source_sha256, "__csv__", {"text": None})],
        )

    mapped = load_mapped_records(
        [path],
        [
            SheetMapping(
                inspected.source_sha256,
                "__csv__",
                {"text": "正文", "created_at": "日期"},
                DATE_FORMAT_ISO8601,
            )
        ],
    )
    assert mapped.records[0].text_raw == "一段真实反馈"
    assert mapped.metadata[0].occurred_at is None
    assert mapped.metadata[0].date_parse_status == "invalid_format"


def test_mapping_is_bound_to_the_inspected_file_digest_and_stable_record_id(tmp_path: Path) -> None:
    path = tmp_path / "反馈.csv"
    path.write_text("正文,渠道\n一段真实反馈,网页\n", encoding="utf-8")
    preview = inspect_inputs([path]).tables[0]
    mapping = SheetMapping(preview.source_sha256, "__csv__", {"text": "正文", "platform": "渠道"})
    first = load_mapped_records([path], [mapping])
    second = load_mapped_records([path], [mapping])
    assert first.records[0].record_id == second.records[0].record_id

    path.write_text("正文,渠道\n已修改的反馈,网页\n", encoding="utf-8")
    with pytest.raises(InputContractError, match="changed or disappeared"):
        load_mapped_records([path], [mapping])


def test_mapped_pipeline_writes_reviewable_prepared_artifacts_and_mapping_bound_digest(tmp_path: Path) -> None:
    path = tmp_path / "反馈.csv"
    path.write_text("正文,渠道,日期\n搜索报错,网页,2026-09-08\n", encoding="utf-8")
    inspected = inspect_inputs([path]).tables[0]
    mapping = SheetMapping(
        inspected.source_sha256,
        "__csv__",
        {"text": "正文", "platform": "渠道", "created_at": "日期"},
        DATE_FORMAT_YMD,
    )
    output = tmp_path / "workbench-output"
    prepared, manifest = prepare_mapped_pipeline(
        [path], [mapping], output_dir=output, batch_id="batch-demo"
    )
    assert len(prepared) == 1
    assert (output / "prepared.csv").exists()
    assert (output / "record_metadata.json").exists()
    assert (output / "reviews.sqlite").exists()
    assert manifest["mapped_preparation_digest"] != manifest["preparation_digest"]

    changed_mapping = SheetMapping(
        inspected.source_sha256,
        "__csv__",
        {"text": "正文", "platform": "渠道", "created_at": "日期"},
        DATE_FORMAT_ISO8601,
    )
    _changed_records, changed_manifest = prepare_mapped_pipeline(
        [path], [changed_mapping], output_dir=tmp_path / "changed", batch_id="batch-changed"
    )
    assert changed_manifest["preparation_digest"] == manifest["preparation_digest"]
    assert changed_manifest["mapped_preparation_digest"] != manifest["mapped_preparation_digest"]

    prepared_path = output / "prepared.csv"
    classified, checkpoint = classify_mapped_pipeline(prepared_path=prepared_path, output_dir=output)
    assert checkpoint.exists()
    assert classified[0].classification_status == "classified"
    manifest_file = output / "input_manifest.json"
    manifest_file.write_text('{"preparation_digest":"wrong","mapped_preparation_digest":"wrong"}', encoding="utf-8")
    with pytest.raises(CheckpointMismatch, match="no longer matches"):
        classify_mapped_pipeline(prepared_path=prepared_path, output_dir=output)
