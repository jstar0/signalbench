"""Record the real SignalBench workbench API into web/src/replay/recording.json.

Starts the real backend against a throw-away runtime directory, then drives it
with the same requests that web/src/main.tsx sends for each bundled example
file (upload -> mapping -> batch -> run -> results -> exports -> comparison).
Every response stored in the recording comes straight from the server.

    uv run python scripts/record_replay.py
"""

from __future__ import annotations

import base64
import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "web" / "src" / "replay" / "recording.json"

# The mapping a user would pick in the UI for each example file.
SAMPLES = [
    {
        "id": "comments",
        "label": "comments.csv · 论坛评论（含重复、无效与表情行）",
        "path": ROOT / "examples" / "comments.csv",
        "ui_mapping": {"text": "comment", "platform": "platform", "author": "author", "record_id": "id", "created_at": "", "date_format": "yyyy-mm-dd"},
    },
    {
        "id": "clean_feedback",
        "label": "clean_feedback.csv · 带日期的产品反馈",
        "path": ROOT / "examples" / "clean_feedback.csv",
        "ui_mapping": {"text": "text", "platform": "platform", "author": "", "record_id": "record_id", "created_at": "created_at", "date_format": "yyyy-mm-dd"},
    },
]

SENTIMENTS = ["positive", "neutral", "negative"]
TOPICS = ["pricing", "reliability", "support", "safety", "general"]
QUALITY_SORTS = ["position", "source", "quality", "duplicate", "text", "include"]
RESULT_SORTS = ["source", "sentiment", "topic", "date", "text"]


def canonical(params: dict[str, str]) -> str:
    return urllib.parse.urlencode(sorted((k, v) for k, v in params.items() if k != "cursor"))


class Api:
    def __init__(self, base: str) -> None:
        self.base = base

    def call(self, method: str, path: str, *, json_body=None, headers=None, data=None):
        headers = dict(headers or {})
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{method} {path} -> {exc.code} {exc.read()!r}") from exc

    def json(self, method: str, path: str, **kwargs):
        _, body, _ = self.call(method, path, **kwargs)
        return json.loads(body)


def multipart(name: str, content: bytes) -> tuple[bytes, str]:
    boundary = "----signalbench" + uuid.uuid4().hex
    head = f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\nContent-Type: text/csv\r\n\r\n'
    return head.encode() + content + f"\r\n--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def read_events(api: Api, job_id: str) -> list[dict]:
    events: list[dict] = []
    request = urllib.request.Request(f"{api.base}/api/v1/jobs/{job_id}/events")
    with urllib.request.urlopen(request, timeout=60) as response:
        kind = None
        for raw in response:
            line = raw.decode().rstrip("\n")
            if line.startswith("event: "):
                kind = line[7:]
            elif line.startswith("data: "):
                events.append({"event": kind, "data": json.loads(line[6:])})
    return events


def record_sample(api: Api, sample: dict) -> dict:
    path: Path = sample["path"]
    content = path.read_bytes()
    body, content_type = multipart(path.name, content)
    imported = json.loads(api.call("POST", "/api/v1/imports", data=body, headers={"Content-Type": content_type})[1])
    sheet = imported["sheets"][0]
    ui = sample["ui_mapping"]
    columns = {k: v for k, v in ui.items() if k != "date_format" and v}
    mapping_request = {"sheets": [{"asset_id": sheet["asset_id"], "source_sha256": sheet["source_sha256"], "sheet": sheet["sheet"], "columns": columns, "date_format": ui["date_format"] if ui["created_at"] else None}]}
    mapped = api.json("PATCH", f"/api/v1/imports/{imported['import_id']}/mapping", json_body=mapping_request, headers={"If-Match": str(imported["revision"])})
    batch_request = {"import_id": imported["import_id"], "revision": mapped["revision"]}
    batch = api.json("POST", "/api/v1/batches", json_body=batch_request)
    batch_id = batch["batch_id"]

    quality: dict[str, dict] = {}

    def q(**extra: str) -> None:
        params = {"sort": "position", "order": "asc", "limit": "50", **extra}
        quality[canonical(params)] = api.json("GET", f"/api/v1/batches/{batch_id}/records?{urllib.parse.urlencode(params)}")

    q()
    for qual in ["valid", "invalid", "display_issue"]:
        q(quality=qual)
    for inc in ["included", "excluded", "pending"]:
        q(include=inc)
    for qual in ["valid", "invalid", "display_issue"]:
        for inc in ["included", "excluded", "pending"]:
            q(quality=qual, include=inc)
    for sort in QUALITY_SORTS:
        for order in ["asc", "desc"]:
            q(sort=sort, order=order)
    for limit in ["25", "100"]:
        q(limit=limit)

    run = api.json("POST", "/api/v1/analysis-runs", json_body={"batch_id": batch_id})
    events = read_events(api, run["job_id"])
    final_job = api.json("GET", f"/api/v1/jobs/{run['job_id']}")
    assert final_job["status"] == "succeeded", final_job
    summary = api.json("GET", f"/api/v1/analysis-runs/{run['job_id']}/summary")

    results: dict[str, dict] = {}

    def r(**extra: str) -> None:
        params = {"sort": "source", "order": "asc", "limit": "50", **extra}
        results[canonical(params)] = api.json("GET", f"/api/v1/analysis-runs/{run['job_id']}/records?{urllib.parse.urlencode(params)}")

    r()
    for value in SENTIMENTS:
        r(sentiment=value)
    for value in TOPICS:
        r(topic=value)
    for value in ["reviewed", "unreviewed"]:
        r(review_status=value)
    for sort in RESULT_SORTS:
        for order in ["asc", "desc"]:
            r(sort=sort, order=order)
    for limit in ["25", "100"]:
        r(limit=limit)

    exports: dict[str, dict] = {}
    for scope, fmt in [("summary", "html"), ("detail", "csv")]:
        artifact = api.json("POST", f"/api/v1/analysis-runs/{run['job_id']}/exports", json_body={"scope": scope, "format": fmt, "sort": "source", "order": "asc", "limit": 50})
        _, blob, headers = api.call("GET", f"/api/v1/artifacts/{artifact['artifact_id']}/download")
        exports[f"{scope}:{fmt}"] = {"artifact": artifact, "media_type": headers.get("content-type", ""), "base64": base64.b64encode(blob).decode()}

    return {
        "id": sample["id"],
        "label": sample["label"],
        "filename": path.name,
        "text": content.decode("utf-8"),
        "ui_mapping": ui,
        "import": imported,
        "mapping": {"request": mapping_request, "if_match": str(imported["revision"]), "response": mapped},
        "batch": {"request": batch_request, "response": batch},
        "quality": quality,
        "run": {"request": {"batch_id": batch_id}, "response": run, "events": events, "final_job": final_job, "summary": summary, "results": results, "exports": exports},
    }


def main() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="signalbench-replay-") as runtime:
        server = subprocess.Popen([sys.executable, "-m", "signalbench", "serve", "--host", "127.0.0.1", "--port", str(port), "--runtime-dir", runtime], cwd=ROOT)
        api = Api(f"http://127.0.0.1:{port}")
        try:
            for _ in range(100):
                try:
                    api.json("GET", "/api/v1/analysis-runs")
                    break
                except Exception:
                    time.sleep(0.2)
            else:
                raise RuntimeError("backend did not start")
            samples = [record_sample(api, sample) for sample in SAMPLES]
            runs = api.json("GET", "/api/v1/analysis-runs")["runs"]
            comparisons: dict[str, dict] = {}
            for ref in samples:
                for cur in samples:
                    if ref is cur:
                        continue
                    pair = {"reference_run": ref["run"]["response"]["job_id"], "current_run": cur["run"]["response"]["job_id"]}
                    comparisons[f"{pair['reference_run']}|{pair['current_run']}"] = api.json("POST", "/api/v1/comparisons", json_body=pair)
        finally:
            server.terminate()
            server.wait(timeout=10)
    OUT.write_text(json.dumps({"samples": samples, "runs": runs, "comparisons": comparisons}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
