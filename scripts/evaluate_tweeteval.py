"""Evaluate the offline baseline on a pinned public TweetEval sentiment split.

The script downloads data for evaluation only and writes aggregate evidence.
It does not copy tweet text into this repository or its output bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from collections import Counter
from pathlib import Path

from signalbench.classify import DeterministicClassifier
from signalbench.evaluation import evaluate_classifier
from signalbench.io import atomic_write_text
from signalbench.models import SourceRecord
from signalbench.quality import prepare_records

COMMIT = "4fbd22cd78421f05b1ecdb4fc5725bc7a7bd8f66"
BASE_URL = f"https://raw.githubusercontent.com/cardiffnlp/tweeteval/{COMMIT}/datasets/sentiment"
SOURCES = {
    "test_text.txt": "09a93a55c63fd93f97485ef7302889d7edb4091cd49733aa37da094f0bfa0675",
    "test_labels.txt": "6afb4afe9374d1f983bcf9a7c79b108d0f37fdf020a83f30488309bed215db9d",
    "mapping.txt": "c0572796fbe261d8ea5fd16c5061076fbba49bc69fc865d6abccaf60daacee24",
}


def download(name: str) -> bytes:
    request = urllib.request.Request(f"{BASE_URL}/{name}", headers={"User-Agent": "SignalBench-public-evaluation"})
    with urllib.request.urlopen(request, timeout=30) as response:
        content = response.read()
    digest = hashlib.sha256(content).hexdigest()
    if digest != SOURCES[name]:
        raise RuntimeError(f"TweetEval {name} digest changed: expected {SOURCES[name]}, got {digest}")
    return content


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/tweeteval"))
    args = parser.parse_args()

    texts = download("test_text.txt").decode("utf-8").splitlines()
    raw_labels = download("test_labels.txt").decode("utf-8").splitlines()
    mapping = dict(
        line.split("\t", 1) for line in download("mapping.txt").decode("utf-8").splitlines() if line
    )
    if len(texts) != len(raw_labels):
        raise RuntimeError(f"TweetEval text/label length mismatch: {len(texts)} != {len(raw_labels)}")

    labelled = [(text, mapping[label]) for text, label in zip(texts, raw_labels)]
    source = [
        SourceRecord(f"public_{index:05d}", "tweet-eval", "sentiment-test", index + 1, "tweet", "", text)
        for index, (text, _label) in enumerate(labelled)
    ]
    prepared = prepare_records(source)
    valid_examples = [
        (record.text_raw, label)
        for record, (_text, label) in zip(prepared, labelled)
        if record.quality_label == "valid"
    ]
    exclusions = Counter(record.quality_reason or "unknown" for record in prepared if record.quality_label != "valid")
    metrics = evaluate_classifier(DeterministicClassifier(), valid_examples)
    payload = {
        "schema_version": 1,
        "dataset": "TweetEval sentiment test split",
        "upstream": "https://github.com/cardiffnlp/tweeteval",
        "upstream_commit": COMMIT,
        "upstream_commit_url": f"https://github.com/cardiffnlp/tweeteval/commit/{COMMIT}",
        "citation": "Barbieri et al., TweetEval, Findings of EMNLP 2020",
        "redistribution": "Tweet text is downloaded at evaluation time and is not written to this repository or output.",
        "license_note": "TweetEval states that it is unrestricted, while original task and Twitter restrictions may apply.",
        "source_sha256": SOURCES,
        "upstream_rows": len(labelled),
        "evaluated_rows": len(valid_examples),
        "quality_excluded_rows": len(labelled) - len(valid_examples),
        "quality_exclusion_reasons": dict(sorted(exclusions.items())),
        "metrics": metrics,
        "limitations": [
            "English Twitter sentiment differs from product-feedback language.",
            "Rows rejected by SignalBench input-quality rules are reported as coverage loss and excluded from label metrics.",
            "The result describes this pinned split only and is not a production accuracy claim.",
        ],
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(args.output_dir / "metrics.json", json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    summary = f"""# TweetEval public evaluation

- Upstream commit: `{COMMIT}`
- Upstream rows: **{len(labelled)}**
- Evaluated after input-quality rules: **{len(valid_examples)}**
- Quality exclusions: **{len(labelled) - len(valid_examples)}**
- Accuracy: **{metrics['accuracy']:.3f}**
- Macro-F1: **{metrics['macro_f1']:.3f}**

This is an external, pinned evaluation on the TweetEval sentiment test split.
It is useful evidence that the rule baseline does not generalize like a trained
sentiment model. It is not a product-feedback benchmark or production claim.
Tweet text is downloaded for the run and is not copied into this output.
See `metrics.json` for hashes, coverage loss, per-label metrics and limitations.
"""
    atomic_write_text(args.output_dir / "summary.md", summary)
    print(json.dumps({"metrics": str(args.output_dir / "metrics.json"), "summary": str(args.output_dir / "summary.md"), "accuracy": metrics["accuracy"], "macro_f1": metrics["macro_f1"], "evaluated_rows": len(valid_examples)}, indent=2))


if __name__ == "__main__":
    main()
