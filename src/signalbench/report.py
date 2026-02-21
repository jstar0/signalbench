from __future__ import annotations

import csv
import html
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .classify import classification_summary
from .io import atomic_write_text, ensure_parent, write_json
from .models import PreparedRecord
from .quality import preparation_digest, quality_reasons, quality_summary


def write_rows_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    """Write a list of dictionaries with deterministic columns and atomic replace."""

    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                columns.append(key)
                seen.add(key)
    ensure_parent(path)
    from io import StringIO

    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        normalized = {
            key: json.dumps(value, ensure_ascii=False, sort_keys=True)
            if isinstance(value, (dict, list))
            else value
            for key, value in row.items()
        }
        writer.writerow(normalized)
    atomic_write_text(path, buffer.getvalue())


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _metric_card(label: str, value: str, detail: str = "") -> str:
    return (
        '<div class="card"><div class="label">'
        + html.escape(label)
        + '</div><div class="value">'
        + html.escape(value)
        + '</div><div class="detail">'
        + html.escape(detail)
        + "</div></div>"
    )


def _quality_table(rows: Sequence[Mapping[str, Any]]) -> str:
    body: list[str] = []
    for row in rows:
        scope = "Overall" if row.get("scope") == "overall" else f"{row.get('platform')} / {row.get('source_sheet')}"
        body.append(
            "<tr>"
            f"<td>{html.escape(scope)}</td>"
            f"<td>{row.get('raw_total', 0)}</td>"
            f"<td>{row.get('valid_total', 0)}</td>"
            f"<td>{row.get('invalid_total', 0)}</td>"
            f"<td>{row.get('display_issue_total', 0)}</td>"
            f"<td>{row.get('duplicate_rows', 0)}</td>"
            f"<td>{_percent(float(row.get('valid_rate', 0)))}</td>"
            f"<td>{_percent(float(row.get('review_rate_raw', 0)))}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Source</th><th>Raw</th><th>Valid</th>"
        "<th>Invalid</th><th>Display issue</th><th>Duplicate rows</th>"
        "<th>Valid rate</th><th>Review rate</th></tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table>"
    )


def _classification_table(rows: Sequence[Mapping[str, Any]]) -> str:
    body: list[str] = []
    for row in rows:
        scope = "Overall" if row.get("scope") == "overall" else f"{row.get('platform')} / {row.get('source_sheet')}"
        low = _percent(float(row.get("negative_rate_ci95_low", 0)))
        high = _percent(float(row.get("negative_rate_ci95_high", 0)))
        body.append(
            "<tr>"
            f"<td>{html.escape(scope)}</td>"
            f"<td>{row.get('classified_total', 0)}</td>"
            f"<td>{row.get('positive_total', 0)}</td>"
            f"<td>{row.get('neutral_total', 0)}</td>"
            f"<td>{row.get('negative_total', 0)}</td>"
            f"<td>{_percent(float(row.get('negative_rate', 0)))}</td>"
            f"<td>{low}–{high}</td>"
            f"<td>{_percent(float(row.get('review_rate', 0)))}</td>"
            f"<td>{_percent(float(row.get('mean_confidence', 0)))}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Source</th><th>Classified</th><th>Positive</th>"
        "<th>Neutral</th><th>Negative</th><th>Negative rate</th><th>95% CI</th>"
        "<th>Review rate</th><th>Mean confidence</th></tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table>"
    )


def render_html_report(
    *,
    quality_rows: Sequence[Mapping[str, Any]],
    classification_rows: Sequence[Mapping[str, Any]],
    reasons: Mapping[str, int],
    digest: str,
    classifier_name: str,
) -> str:
    overall_quality = quality_rows[0] if quality_rows else {}
    overall_classification = classification_rows[0] if classification_rows else {}
    cards = "".join(
        [
            _metric_card("Raw rows", str(overall_quality.get("raw_total", 0)), "input rows retained"),
            _metric_card("Valid rows", str(overall_quality.get("valid_total", 0)), _percent(float(overall_quality.get("valid_rate", 0))) + " of raw"),
            _metric_card("Classified", str(overall_classification.get("classified_total", 0)), "deterministic decisions"),
            _metric_card("Negative rate", _percent(float(overall_classification.get("negative_rate", 0))), "Wilson 95% interval below"),
        ]
    )
    reason_items = "".join(
        f"<li><code>{html.escape(str(reason))}</code>: {count}</li>"
        for reason, count in reasons.items()
    ) or "<li>No quality exclusions.</li>"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SignalBench report</title>
  <style>
    :root {{ color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
    body {{ margin: 0; background: #f5f7fb; color: #1f2937; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 42px 22px 70px; }}
    h1 {{ margin-bottom: 6px; letter-spacing: -0.02em; }}
    h2 {{ margin-top: 34px; }}
    .lede {{ color: #526071; max-width: 760px; }}
    .meta {{ color: #6b7280; font-size: .85rem; overflow-wrap: anywhere; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 14px; margin: 28px 0; }}
    .card {{ background: white; border: 1px solid #e3e8f0; border-radius: 14px; padding: 18px; box-shadow: 0 5px 18px rgba(31,41,55,.04); }}
    .label {{ color: #667085; font-size: .82rem; }}
    .value {{ font-size: 2rem; font-weight: 750; margin: 6px 0; }}
    .detail {{ color: #667085; font-size: .82rem; }}
    table {{ width: 100%; border-collapse: collapse; background: white; border: 1px solid #e3e8f0; border-radius: 12px; overflow: hidden; font-size: .9rem; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #edf0f5; text-align: left; white-space: nowrap; }}
    th {{ background: #eef2f8; color: #475467; font-size: .78rem; text-transform: uppercase; letter-spacing: .04em; }}
    tr:last-child td {{ border-bottom: 0; }}
    .scroll {{ overflow-x: auto; border-radius: 12px; }}
    .note {{ background: #fff8e7; border: 1px solid #f4d58d; padding: 14px 16px; border-radius: 12px; }}
    code {{ background: #eef2f8; padding: 2px 5px; border-radius: 4px; }}
    footer {{ margin-top: 42px; color: #7b8494; font-size: .8rem; }}
  </style>
</head>
<body><main>
  <h1>SignalBench report</h1>
  <p class="lede">A reproducible quality gate and signal summary for heterogeneous comment tables. The report contains aggregates only; row-level evidence stays in the local CSV.</p>
  <p class="meta">Classifier: <code>{html.escape(classifier_name)}</code> · Preparation digest: <code>{html.escape(digest)}</code></p>
  <section class="cards">{cards}</section>
  <h2>Quality gate</h2>
  <div class="scroll">{_quality_table(quality_rows)}</div>
  <h3>Exclusion reasons</h3>
  <div class="note"><ul>{reason_items}</ul></div>
  <h2>Classification summary</h2>
  <div class="scroll">{_classification_table(classification_rows)}</div>
  <footer>Generated by SignalBench · adapter source should be recorded by the caller</footer>
</main></body></html>
"""


SHAREABLE_MANIFEST_KEYS = (
    "config_sha256",
    "preparation_digest",
    "raw_row_count",
    "record_count",
    "sheet_count",
)


def _shareable_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Keep digests and counts; drop file names, paths, and source headers."""

    summary: dict[str, Any] = {
        key: manifest[key] for key in SHAREABLE_MANIFEST_KEYS if key in manifest
    }
    files = manifest.get("files")
    if isinstance(files, list):
        summary["file_count"] = len(files)
        summary["file_digests"] = sorted(
            str(entry.get("sha256", "")) for entry in files if isinstance(entry, Mapping)
        )
    return summary


def build_report(
    records: Iterable[PreparedRecord],
    *,
    output_dir: Path,
    manifest: Mapping[str, Any] | None = None,
    classifier_name: str = "deterministic-v1",
) -> dict[str, Any]:
    """Write machine-readable and human-readable aggregate reports."""

    rows = list(records)
    quality_rows = quality_summary(rows)
    classification_rows = classification_summary(rows)
    reasons = quality_reasons(rows)
    digest = preparation_digest(rows)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "preparation_digest": digest,
        "classifier": classifier_name,
        "quality": quality_rows,
        "classification": classification_rows,
        "quality_reasons": reasons,
    }
    if manifest is not None:
        # The full manifest names source files and their original column
        # headers, which a shared report should not carry. It stays in the local
        # input_manifest.json; the report keeps only the digests and counts
        # needed to tie it back to that run.
        payload["input_summary"] = _shareable_manifest(manifest)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "report.json", payload)
    write_rows_csv(output_dir / "quality_summary.csv", quality_rows)
    write_rows_csv(output_dir / "classification_summary.csv", classification_rows)
    atomic_write_text(
        output_dir / "report.html",
        render_html_report(
            quality_rows=quality_rows,
            classification_rows=classification_rows,
            reasons=reasons,
            digest=digest,
            classifier_name=classifier_name,
        ),
    )
    return payload
