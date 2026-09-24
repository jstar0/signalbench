from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from signalbench.app.main import _csv_safe, create_app


def test_spreadsheet_export_cells_escape_formula_prefixes_without_changing_plain_text() -> (
    None
):
    assert _csv_safe('=HYPERLINK("bad")') == '\'=HYPERLINK("bad")'
    assert _csv_safe("@SUM(A1:A2)") == "'@SUM(A1:A2)"
    assert _csv_safe("普通反馈") == "普通反馈"


def _wait_for_terminal(client: TestClient, job_id: str) -> dict:
    for _ in range(100):
        payload = client.get(f"/api/v1/jobs/{job_id}").json()
        if payload["status"] in {"succeeded", "failed", "cancelled"}:
            return payload
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def _create_batch(
    client: TestClient, *, filename: str, content: str, columns: dict[str, str]
) -> str:
    uploaded = client.post(
        "/api/v1/imports",
        files=[("files", (filename, content, "text/csv"))],
    )
    assert uploaded.status_code == 201
    payload = uploaded.json()
    sheet = payload["sheets"][0]
    mapped = client.patch(
        f"/api/v1/imports/{payload['import_id']}/mapping",
        headers={"If-Match": "1"},
        json={
            "sheets": [
                {
                    "asset_id": sheet["asset_id"],
                    "source_sha256": sheet["source_sha256"],
                    "sheet": sheet["sheet"],
                    "columns": columns,
                }
            ]
        },
    )
    assert mapped.status_code == 200
    batch = client.post(
        "/api/v1/batches",
        json={"import_id": payload["import_id"], "revision": mapped.json()["revision"]},
    )
    assert batch.status_code == 201
    return batch.json()["batch_id"]


def test_asset_bound_import_mapping_batch_and_real_analysis_job(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "runtime"))
    uploaded = client.post(
        "/api/v1/imports",
        files=[
            (
                "files",
                (
                    "反馈.csv",
                    "正文,来源,日期\n搜索报错,应用内,2026-09-08\n",
                    "text/csv",
                ),
            )
        ],
    )
    assert uploaded.status_code == 201
    payload = uploaded.json()
    sheet = payload["sheets"][0]
    mapped = client.patch(
        f"/api/v1/imports/{payload['import_id']}/mapping",
        headers={"If-Match": "1"},
        json={
            "sheets": [
                {
                    "asset_id": sheet["asset_id"],
                    "source_sha256": sheet["source_sha256"],
                    "sheet": sheet["sheet"],
                    "columns": {
                        "text": "正文",
                        "platform": "来源",
                        "created_at": "日期",
                    },
                    "date_format": "yyyy-mm-dd",
                }
            ]
        },
    )
    assert mapped.status_code == 200
    batch = client.post(
        "/api/v1/batches", json={"import_id": payload["import_id"], "revision": 2}
    )
    assert batch.status_code == 201
    run = client.post(
        "/api/v1/analysis-runs", json={"batch_id": batch.json()["batch_id"]}
    )
    assert run.status_code == 202
    terminal = _wait_for_terminal(client, run.json()["job_id"])
    assert terminal["status"] == "succeeded"
    assert terminal["progress"] == {"done": 1, "total": 1, "unit": "records"}
    dated = client.get(
        f"/api/v1/analysis-runs/{run.json()['job_id']}/records?date_from=2026-09-09"
    ).json()
    assert dated["date_filter_available"] is True
    assert dated["filtered_total"] == 0
    assert (
        client.get(
            f"/api/v1/analysis-runs/{run.json()['job_id']}/records?date_from=not-a-date"
        ).status_code
        == 422
    )
    events = client.get(f"/api/v1/jobs/{run.json()['job_id']}/events").text
    assert "event: queued" in events
    assert "event: succeeded" in events
    resumed_events = client.get(
        f"/api/v1/jobs/{run.json()['job_id']}/events", headers={"Last-Event-ID": "1"}
    ).text
    assert "event: queued" not in resumed_events
    assert "event: succeeded" in resumed_events


def test_mapping_rejects_asset_from_another_import(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "runtime"))
    first = client.post(
        "/api/v1/imports", files=[("files", ("a.csv", "正文\n内容\n", "text/csv"))]
    ).json()
    second = client.post(
        "/api/v1/imports", files=[("files", ("b.csv", "正文\n内容\n", "text/csv"))]
    ).json()
    foreign_sheet = second["sheets"][0]
    rejected = client.patch(
        f"/api/v1/imports/{first['import_id']}/mapping",
        headers={"If-Match": "1"},
        json={
            "sheets": [
                {
                    "asset_id": foreign_sheet["asset_id"],
                    "source_sha256": foreign_sheet["source_sha256"],
                    "sheet": foreign_sheet["sheet"],
                    "columns": {"text": "正文"},
                }
            ]
        },
    )
    assert rejected.status_code == 404


def test_partial_upload_keeps_valid_file_and_reports_invalid_one(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "runtime"))
    response = client.post(
        "/api/v1/imports",
        files=[
            ("files", ("valid.csv", "正文\n一条有效反馈\n", "text/csv")),
            ("files", ("broken.csv", "", "text/csv")),
        ],
    )
    assert response.status_code == 201
    payload = response.json()
    assert len(payload["assets"]) == 1
    assert len(payload["sheets"]) == 1
    assert payload["errors"][0]["display_name"] == "broken.csv"


def test_review_is_versioned_and_reaggregates_completed_analysis_without_rewriting_source(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "runtime"))
    uploaded = client.post(
        "/api/v1/imports",
        files=[
            (
                "files",
                (
                    "反馈.csv",
                    "正文,来源\n价格太高,应用内\n优秀的客服支持,应用内\n",
                    "text/csv",
                ),
            )
        ],
    ).json()
    sheet = uploaded["sheets"][0]
    mapped = client.patch(
        f"/api/v1/imports/{uploaded['import_id']}/mapping",
        headers={"If-Match": "1"},
        json={
            "sheets": [
                {
                    "asset_id": sheet["asset_id"],
                    "source_sha256": sheet["source_sha256"],
                    "sheet": sheet["sheet"],
                    "columns": {"text": "正文", "platform": "来源"},
                }
            ]
        },
    ).json()
    batch_id = client.post(
        "/api/v1/batches",
        json={"import_id": uploaded["import_id"], "revision": mapped["revision"]},
    ).json()["batch_id"]
    rows_before = client.get(f"/api/v1/batches/{batch_id}/records").json()["rows"]
    first = rows_before[0]
    review = client.patch(
        f"/api/v1/batches/{batch_id}/reviews",
        headers={"If-Match": "0"},
        json={
            "updates": [
                {
                    "record_id": first["record_id"],
                    "sentiment_override": "positive",
                    "topic_override": "support",
                    "reason": "人工复核：描述的是客服处理后的结果",
                }
            ]
        },
    )
    assert review.status_code == 200
    rows_after = client.get(f"/api/v1/batches/{batch_id}/records").json()["rows"]
    assert rows_after[0]["text_raw"] == first["text_raw"]
    assert rows_after[0]["sentiment_override"] == "positive"
    stale = client.patch(
        f"/api/v1/batches/{batch_id}/reviews",
        headers={"If-Match": "0"},
        json={
            "updates": [
                {
                    "record_id": first["record_id"],
                    "include_override": "exclude",
                    "reason": "旧版本",
                }
            ]
        },
    )
    assert stale.status_code == 409

    job_id = client.post("/api/v1/analysis-runs", json={"batch_id": batch_id}).json()[
        "job_id"
    ]
    terminal = _wait_for_terminal(client, job_id)
    assert terminal["status"] == "succeeded"
    overall = client.get(f"/api/v1/analysis-runs/{job_id}/summary").json()["summary"][0]
    assert overall["positive_total"] == 2
    assert overall["negative_total"] == 0
    result_rows = client.get(f"/api/v1/analysis-runs/{job_id}/records").json()["rows"]
    assert result_rows[0]["algorithm_sentiment"] == "neutral"
    assert result_rows[0]["sentiment"] == "positive"
    assert result_rows[0]["review_reason"].startswith("人工复核")

    summary_export = client.post(
        f"/api/v1/analysis-runs/{job_id}/exports",
        json={"scope": "summary", "format": "html"},
    )
    assert summary_export.status_code == 201
    shared = client.get(
        f"/api/v1/artifacts/{summary_export.json()['artifact_id']}/download"
    )
    assert shared.status_code == 200
    assert "价格太高" not in shared.text
    detail_export = client.post(
        f"/api/v1/analysis-runs/{job_id}/exports",
        json={"scope": "detail", "format": "csv"},
    )
    assert detail_export.status_code == 201
    detail = client.get(
        f"/api/v1/artifacts/{detail_export.json()['artifact_id']}/download"
    )
    assert "价格太高" in detail.text
    assert "人工复核" in detail.text
    frozen_shared = shared.content
    post_run_review = client.patch(
        f"/api/v1/analysis-runs/{job_id}/reviews",
        headers={"If-Match": "1"},
        json={
            "updates": [
                {
                    "record_id": result_rows[1]["record_id"],
                    "sentiment_override": "negative",
                    "reason": "完成后复核：这条表达了负向结果",
                }
            ]
        },
    )
    assert post_run_review.status_code == 200
    reviewed_rows = client.get(
        f"/api/v1/analysis-runs/{job_id}/records?review_status=reviewed&limit=1"
    ).json()
    unreviewed_rows = client.get(
        f"/api/v1/analysis-runs/{job_id}/records?review_status=unreviewed"
    ).json()
    assert reviewed_rows["filtered_total"] == 2
    assert len(reviewed_rows["rows"]) == 1
    assert unreviewed_rows["filtered_total"] == 0
    revised = client.get(f"/api/v1/analysis-runs/{job_id}/summary").json()["summary"][0]
    assert revised["positive_total"] == 1
    assert revised["negative_total"] == 1
    filtered_export = client.post(
        f"/api/v1/analysis-runs/{job_id}/exports",
        json={"scope": "detail", "format": "csv", "sentiment": "negative"},
    )
    assert filtered_export.status_code == 201
    filtered_detail = client.get(
        f"/api/v1/artifacts/{filtered_export.json()['artifact_id']}/download"
    )
    assert "优秀的客服支持" in filtered_detail.text
    assert "价格太高" not in filtered_detail.text
    assert filtered_export.json()["filename"].endswith("-filtered.csv")
    searched_export = client.post(
        f"/api/v1/analysis-runs/{job_id}/exports",
        json={
            "scope": "detail",
            "format": "csv",
            "search": "客服",
            "review_status": "reviewed",
        },
    )
    assert searched_export.status_code == 201
    searched_detail = client.get(
        f"/api/v1/artifacts/{searched_export.json()['artifact_id']}/download"
    )
    assert "优秀的客服支持" in searched_detail.text
    assert "价格太高" not in searched_detail.text
    assert (
        client.get(
            f"/api/v1/artifacts/{summary_export.json()['artifact_id']}/download"
        ).content
        == frozen_shared
    )
    comparison = client.post(
        "/api/v1/comparisons", json={"reference_run": job_id, "current_run": job_id}
    )
    assert comparison.status_code == 200
    assert comparison.json()["reliable"] is False
    assert comparison.json()["limitations"] == ["sample_lt_30"]
    assert comparison.json()["structure"]["metrics"][0]["comparable"] is True


def test_analysis_scope_and_progress_follow_frozen_review_inclusion(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "runtime"))
    uploaded = client.post(
        "/api/v1/imports",
        files=[
            (
                "files",
                ("反馈.csv", "正文\n第一条有效反馈\n第二条有效反馈\n", "text/csv"),
            )
        ],
    ).json()
    sheet = uploaded["sheets"][0]
    revision = client.patch(
        f"/api/v1/imports/{uploaded['import_id']}/mapping",
        headers={"If-Match": "1"},
        json={
            "sheets": [
                {
                    "asset_id": sheet["asset_id"],
                    "source_sha256": sheet["source_sha256"],
                    "sheet": sheet["sheet"],
                    "columns": {"text": "正文"},
                }
            ]
        },
    ).json()["revision"]
    batch_id = client.post(
        "/api/v1/batches",
        json={"import_id": uploaded["import_id"], "revision": revision},
    ).json()["batch_id"]
    rows = client.get(f"/api/v1/batches/{batch_id}/records").json()["rows"]
    client.patch(
        f"/api/v1/batches/{batch_id}/reviews",
        headers={"If-Match": "0"},
        json={
            "updates": [
                {
                    "record_id": rows[1]["record_id"],
                    "include_override": "exclude",
                    "reason": "仅分析第一条",
                }
            ]
        },
    )
    run = client.post("/api/v1/analysis-runs", json={"batch_id": batch_id}).json()
    assert run["progress"]["total"] == 1
    terminal = _wait_for_terminal(client, run["job_id"])
    assert terminal["progress"]["done"] == 1
    overall = client.get(f"/api/v1/analysis-runs/{run['job_id']}/summary").json()[
        "summary"
    ][0]
    assert overall["classified_total"] == 1


def test_quality_and_result_records_are_filtered_and_cursor_paged_on_the_server(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "runtime"))
    csv_rows = ["正文,来源"]
    csv_rows.extend(
        f"优秀的服务体验，加载稳定，反馈编号{index:03d},{'移动端' if index % 2 == 0 else '网页'}"
        for index in range(135)
    )
    batch_id = _create_batch(
        client,
        filename="反馈.csv",
        content="\n".join(csv_rows) + "\n",
        columns={"text": "正文", "platform": "来源"},
    )

    first_quality = client.get(
        f"/api/v1/batches/{batch_id}/records?source=%E7%A7%BB%E5%8A%A8&search=%E5%8F%8D%E9%A6%88%E7%BC%96%E5%8F%B7&sort=text&order=desc&limit=20"
    ).json()
    second_quality = client.get(
        f"/api/v1/batches/{batch_id}/records",
        params={
            "source": "移动",
            "search": "反馈编号",
            "sort": "text",
            "order": "desc",
            "limit": 20,
            "cursor": first_quality["next_cursor"],
        },
    ).json()
    assert first_quality["batch_total"] == 135
    assert first_quality["filtered_total"] == 68
    assert first_quality["included_total"] == 135
    assert len(first_quality["rows"]) == len(second_quality["rows"]) == 20
    assert {row["record_id"] for row in first_quality["rows"]}.isdisjoint(
        {row["record_id"] for row in second_quality["rows"]}
    )
    assert first_quality["next_cursor"] == second_quality["cursor"]

    run = client.post("/api/v1/analysis-runs", json={"batch_id": batch_id}).json()
    assert _wait_for_terminal(client, run["job_id"])["status"] == "succeeded"
    first_results = client.get(
        f"/api/v1/analysis-runs/{run['job_id']}/records",
        params={
            "sentiment": "positive",
            "search": "编号1",
            "sort": "text",
            "order": "desc",
            "limit": 12,
        },
    ).json()
    second_results = client.get(
        f"/api/v1/analysis-runs/{run['job_id']}/records",
        params={
            "sentiment": "positive",
            "search": "编号1",
            "sort": "text",
            "order": "desc",
            "limit": 12,
            "cursor": first_results["next_cursor"],
        },
    ).json()
    assert first_results["filtered_total"] > 12
    assert len(first_results["rows"]) == len(second_results["rows"]) == 12
    assert first_results["review_revision"] == second_results["review_revision"] == 0
    assert {row["record_id"] for row in first_results["rows"]}.isdisjoint(
        {row["record_id"] for row in second_results["rows"]}
    )
    assert (
        client.get(
            f"/api/v1/analysis-runs/{run['job_id']}/records?cursor=not-a-cursor"
        ).status_code
        == 422
    )


def test_keep_first_duplicate_action_handles_more_than_500_rows_server_side(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "runtime"))
    repeated_text = "这是一条内容完整且足够长的重复反馈，用于验证大批量复核。"
    batch_id = _create_batch(
        client,
        filename="重复反馈.csv",
        content="正文\n" + (repeated_text + "\n") * 620,
        columns={"text": "正文"},
    )

    response = client.post(
        f"/api/v1/batches/{batch_id}/reviews/keep-first-duplicates",
        headers={"If-Match": "0"},
    )
    assert response.status_code == 200
    assert response.json()["updated_count"] == 619
    assert response.json()["review_revision"] == 1
    totals = client.get(
        f"/api/v1/batches/{batch_id}/records?limit=10&include=included"
    ).json()
    assert (
        totals["filtered_total"],
        totals["included_total"],
        totals["excluded_total"],
    ) == (1, 1, 619)
    repeated = client.post(
        f"/api/v1/batches/{batch_id}/reviews/keep-first-duplicates",
        headers={"If-Match": "1"},
    )
    assert repeated.status_code == 200
    assert repeated.json()["updated_count"] == 0
    assert repeated.json()["review_revision"] == 1
    stale = client.post(
        f"/api/v1/batches/{batch_id}/reviews/keep-first-duplicates",
        headers={"If-Match": "0"},
    )
    assert stale.status_code == 409
