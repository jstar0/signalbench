from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

import pytest

from signalbench import io
from signalbench.io import InputContractError, load_source_records, write_rows_xlsx


def test_csv_aliases_and_author_hash(tmp_path: Path) -> None:
    path = tmp_path / "comments.csv"
    path.write_text(
        "平台,评论内容,用户名\n社区,Great work,Alice\n社区,\" 另一个意见 \",Bob\n",
        encoding="utf-8",
    )
    records, manifest = load_source_records([path])
    assert len(records) == 2
    assert records[0].platform == "社区"
    assert records[0].text_raw == "Great work"
    assert records[0].author_hash
    assert "Alice" not in records[0].author_hash
    assert manifest["record_count"] == 2
    assert manifest["files"][0]["sheets"][0]["columns"] == ["平台", "评论内容", "用户名"]


def test_identical_file_moved_to_another_directory_keeps_its_record_ids(tmp_path: Path) -> None:
    source = "platform,text\nForum,A useful note\n"
    first = tmp_path / "one" / "comments.csv"
    second = tmp_path / "two" / "comments.csv"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text(source, encoding="utf-8")
    second.write_text(source, encoding="utf-8")

    first_records, _first_manifest = load_source_records([first])
    second_records, _second_manifest = load_source_records([second])

    assert [record.record_id for record in first_records] == [record.record_id for record in second_records]


def test_missing_platform_uses_a_content_bound_label_not_the_filename(tmp_path: Path) -> None:
    source = "text\nA useful note\n"
    path = tmp_path / "AcmeCorp_Q3_churn.csv"
    path.write_text(source, encoding="utf-8")

    records, manifest = load_source_records([path])

    assert records[0].platform == f"source-{manifest['files'][0]['sha256'][:12]}"
    assert "AcmeCorp" not in records[0].platform

    moved = tmp_path / "other" / "renamed.csv"
    moved.parent.mkdir()
    moved.write_text(source, encoding="utf-8")
    moved_records, _moved_manifest = load_source_records([moved])
    assert moved_records[0].platform == records[0].platform


def test_xlsx_is_loaded_sheet_by_sheet(tmp_path: Path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    path = tmp_path / "book.xlsx"
    workbook = openpyxl.Workbook()
    first = workbook.active
    first.title = "Alpha"
    first.append(["platform", "text"])
    first.append(["Forum", "First note"])
    second = workbook.create_sheet("Beta")
    second.append(["platform", "text"])
    second.append(["Forum", "Second note"])
    workbook.save(path)
    workbook.close()

    records, manifest = load_source_records([path])
    assert [record.source_sheet for record in records] == ["Alpha", "Beta"]
    assert [sheet["name"] for sheet in manifest["files"][0]["sheets"]] == ["Alpha", "Beta"]


def test_xlsx_export_is_byte_identical_across_distinct_paths(tmp_path: Path) -> None:
    rows = [{"record_id": "sb_1", "text": "A useful note", "rank": 1}]
    first = tmp_path / "first.xlsx"
    second = tmp_path / "second.xlsx"
    write_rows_xlsx(first, {"prepared": rows})
    write_rows_xlsx(second, {"prepared": rows})
    assert first.read_bytes() == second.read_bytes()
    with ZipFile(first) as workbook:
        assert b"1980-01-01T00:00:00Z" in workbook.read("docProps/core.xml")


def test_missing_text_column_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("author,score\nAlice,5\n", encoding="utf-8")
    with pytest.raises(InputContractError, match="text column"):
        load_source_records([path])


def test_empty_data_rows_fail_loudly(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    path.write_text("text\n\n", encoding="utf-8")
    with pytest.raises(InputContractError, match="no non-empty"):
        load_source_records([path])


def test_tsv_delimiter_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "comments.tsv"
    path.write_text("platform\ttext\nForum\tA useful note\n", encoding="utf-8")
    records, _manifest = load_source_records([path])
    assert records[0].text_raw == "A useful note"


def test_directory_inputs_are_sorted_and_generated_outputs_are_excluded(tmp_path: Path) -> None:
    (tmp_path / "b.csv").write_text("text\nB note\n", encoding="utf-8")
    (tmp_path / "a.csv").write_text("text\nA note\n", encoding="utf-8")
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "should_not_be_loaded.csv").write_text("text\nsecret\n", encoding="utf-8")
    records, _manifest = load_source_records([tmp_path])
    assert [record.source_file for record in records] == ["a.csv", "b.csv"]


def test_input_size_and_file_count_limits_fail_before_ingestion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    oversized = tmp_path / "oversized.csv"
    oversized.write_text("text\nA useful note\n", encoding="utf-8")
    monkeypatch.setattr(io, "MAX_INPUT_BYTES", oversized.stat().st_size - 1)
    with pytest.raises(InputContractError, match="supports files up to"):
        load_source_records([oversized])

    monkeypatch.setattr(io, "MAX_INPUT_BYTES", 1_000_000)
    monkeypatch.setattr(io, "MAX_INPUT_FILES", 1)
    second = tmp_path / "second.csv"
    second.write_text("text\nAnother useful note\n", encoding="utf-8")
    with pytest.raises(InputContractError, match="supports at most 1"):
        load_source_records([oversized, second])


def test_column_and_value_size_limits_fail_with_actionable_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wide = tmp_path / "wide.csv"
    wide.write_text("text,platform,author\nA useful note,Forum,Alice\n", encoding="utf-8")
    monkeypatch.setattr(io, "MAX_COLUMNS", 2)
    with pytest.raises(InputContractError, match="supports at most 2"):
        load_source_records([wide])

    monkeypatch.setattr(io, "MAX_COLUMNS", 128)
    monkeypatch.setattr(io, "MAX_TEXT_CHARS", 5)
    long_text = tmp_path / "long-text.csv"
    long_text.write_text("text\nLonger than five\n", encoding="utf-8")
    with pytest.raises(InputContractError, match="text exceeds"):
        load_source_records([long_text])

    monkeypatch.setattr(io, "MAX_TEXT_CHARS", 8_192)
    monkeypatch.setattr(io, "MAX_METADATA_CHARS", 8)
    long_platform = tmp_path / "long-platform.csv"
    long_platform.write_text("text,platform\nA useful note,LongPlatform\n", encoding="utf-8")
    with pytest.raises(InputContractError, match="platform exceeds"):
        load_source_records([long_platform])


def test_record_limit_applies_to_source_and_prepared_csv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "many.csv"
    source.write_text("text\nA useful note\nAnother useful note\n", encoding="utf-8")
    monkeypatch.setattr(io, "MAX_RECORDS", 1)
    with pytest.raises(InputContractError, match="record v1 limit"):
        load_source_records([source])

    prepared = tmp_path / "prepared.csv"
    prepared.write_text(
        "record_id,text_normalized,quality_label,classification_status,text_fingerprint,duplicate_rank,duplicate_count\n"
        "one,A useful note,valid,pending,24eb124b7ca3d6d1,1,1\n"
        "two,Another useful note,valid,pending,fee75b6906017564,1,1\n",
        encoding="utf-8",
    )
    with pytest.raises(InputContractError, match="Prepared file exceeds"):
        io.read_prepared_csv(prepared)
