from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from .audit import audit_payload
from .config import load_config
from .evaluation import evaluate_gold_file, write_benchmark_artifacts
from .pipeline import (
    build_classifier,
    classify_pipeline,
    inspect_pipeline,
    prepare_pipeline,
    run_pipeline,
    summarize_pipeline,
)


def _common_input_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="CSV, TSV, XLSX files, or directories containing them",
    )
    parser.add_argument("--config", type=Path, default=None, help="Optional JSON rules file")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="signalbench",
        description="Evidence-first, resumable signal extraction from comment tables.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="ingest, validate, normalize, and deduplicate")
    _common_input_parser(prepare)
    prepare.add_argument("--output-dir", type=Path, default=Path("outputs/signalbench"))

    inspect = subparsers.add_parser("inspect", help="show the quality gate without writing outputs")
    _common_input_parser(inspect)

    classify = subparsers.add_parser("classify", help="classify valid rows with resume support")
    classify.add_argument("--prepared", type=Path, required=True, help="prepared.csv from prepare")
    classify.add_argument("--output-dir", type=Path, default=None)
    classify.add_argument("--classifier", default="deterministic", choices=["deterministic", "mock", "replay"])
    classify.add_argument("--replay", type=Path, default=None, help="JSON decisions for the replay classifier")
    classify.add_argument("--config", type=Path, default=None, help="Optional JSON rules file")
    classify.add_argument("--batch-size", type=int, default=32)
    classify.add_argument("--retries", type=int, default=2)
    classify.add_argument("--backoff-seconds", type=float, default=0.0)
    classify.add_argument("--checkpoint", type=Path, default=None)
    classify.add_argument("--fail-after-batches", type=int, default=None, help=argparse.SUPPRESS)

    summarize = subparsers.add_parser("summarize", help="write aggregate CSV, JSON, and HTML reports")
    summarize.add_argument("--classified", type=Path, required=True, help="classified.csv")
    summarize.add_argument("--output-dir", type=Path, default=None)
    summarize.add_argument("--manifest", type=Path, default=None)
    summarize.add_argument("--classifier", default="deterministic-v1")

    run = subparsers.add_parser("run", help="execute prepare, classify, and summarize")
    _common_input_parser(run)
    run.add_argument("--output-dir", type=Path, default=Path("outputs/signalbench"))
    run.add_argument("--classifier", default="deterministic", choices=["deterministic", "mock", "replay"])
    run.add_argument("--replay", type=Path, default=None, help="JSON decisions for the replay classifier")
    run.add_argument("--batch-size", type=int, default=32)
    run.add_argument("--retries", type=int, default=2)
    run.add_argument("--backoff-seconds", type=float, default=0.0)

    demo = subparsers.add_parser(
        "demo",
        help="run the bundled synthetic fixture and print a reviewable result",
    )
    demo.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs/demo"),
        help="directory for generated demo artifacts",
    )
    demo.add_argument("--batch-size", type=int, default=2)

    evaluate = subparsers.add_parser("evaluate", help="score a classifier on a cleared labeled fixture")
    evaluate.add_argument("--gold", type=Path, required=True, help="CSV with text and label columns")
    evaluate.add_argument("--output", type=Path, default=None, help="Optional JSON metrics path")
    evaluate.add_argument("--classifier", default="deterministic", choices=["deterministic", "mock"])
    evaluate.add_argument("--config", type=Path, default=None, help="Optional JSON rules file")

    benchmark = subparsers.add_parser(
        "benchmark",
        help="run the bundled cleared fixture and write reviewable evidence",
    )
    benchmark.add_argument(
        "--fixture",
        type=Path,
        default=None,
        help="CSV with text and label columns (defaults to examples/benchmark.csv in a source checkout)",
    )
    benchmark.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs/benchmark"),
        help="directory for metrics.json and summary.md",
    )
    benchmark.add_argument("--classifier", default="deterministic", choices=["deterministic", "mock"])
    benchmark.add_argument("--config", type=Path, default=None, help="Optional JSON rules file")

    audit = subparsers.add_parser("audit", help="scan a tree for secret-like values and forbidden terms")
    audit.add_argument("--root", type=Path, default=Path("."))
    audit.add_argument(
        "--forbid",
        action="append",
        default=[],
        help="Term to flag (repeatable; case-insensitive)",
    )

    serve = subparsers.add_parser("serve", help="run the local SignalBench workbench API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=5201)
    serve.add_argument("--runtime-dir", type=Path, default=Path("runtime/signalbench"))
    return parser


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        import uvicorn

        from .app.main import create_app

        uvicorn.run(create_app(args.runtime_dir), host=args.host, port=args.port)
        return
    if args.command == "inspect":
        _print_json(inspect_pipeline(args.inputs, config_path=args.config))
        return
    if args.command == "prepare":
        prepared, manifest = prepare_pipeline(
            args.inputs,
            output_dir=args.output_dir,
            config_path=args.config,
        )
        _print_json(
            {
                "prepared": str(args.output_dir / "prepared.csv"),
                "record_count": len(prepared),
                "preparation_digest": manifest["preparation_digest"],
            }
        )
        return
    if args.command == "classify":
        output_dir = args.output_dir or args.prepared.parent
        classified, checkpoint = classify_pipeline(
            prepared_path=args.prepared,
            output_dir=output_dir,
            classifier_name=args.classifier,
            config_path=args.config,
            batch_size=args.batch_size,
            retries=args.retries,
            backoff_seconds=args.backoff_seconds,
            checkpoint_path=args.checkpoint,
            fail_after_batches=args.fail_after_batches,
            replay_path=args.replay,
        )
        _print_json(
            {
                "classified": str(output_dir / "classified.csv"),
                "checkpoint": str(checkpoint),
                "classified_count": sum(item.classification_status == "classified" for item in classified),
            }
        )
        return
    if args.command == "summarize":
        output_dir = args.output_dir or args.classified.parent / "report"
        report = summarize_pipeline(
            classified_path=args.classified,
            output_dir=output_dir,
            manifest_path=args.manifest,
            classifier_name=args.classifier,
        )
        _print_json(
            {
                "report_json": str(output_dir / "report.json"),
                "report_html": str(output_dir / "report.html"),
                "preparation_digest": report["preparation_digest"],
            }
        )
        return
    if args.command == "run":
        result = run_pipeline(
            args.inputs,
            output_dir=args.output_dir,
            config_path=args.config,
            classifier_name=args.classifier,
            batch_size=args.batch_size,
            retries=args.retries,
            backoff_seconds=args.backoff_seconds,
            replay_path=args.replay,
        )
        # The aggregate report is large; the CLI returns paths and headline
        # counts while preserving the full report on disk.
        result.pop("summary", None)
        _print_json(result)
        return
    if args.command == "demo":
        # Resolve the fixture relative to the installed source tree so the
        # command remains useful from any working directory after `uvx` or a
        # wheel install.  An explicit output directory is still required to
        # keep generated artifacts out of the package itself.
        fixture = Path(__file__).resolve().parent / "data" / "demo_comments.csv"
        if not fixture.exists():
            raise SystemExit(
                "The bundled demo fixture is not present in this installation; "
                "run `signalbench run` with your own CSV/XLSX input instead."
            )
        result = run_pipeline(
            [fixture],
            output_dir=args.output_dir,
            batch_size=args.batch_size,
        )
        summary = result.get("summary", {})
        overall_quality = (summary.get("quality") or [{}])[0]
        overall_classification = (summary.get("classification") or [{}])[0]
        _print_json(
            {
                "demo": True,
                "input": "bundled synthetic fixture",
                "output_dir": str(args.output_dir),
                "report": str(args.output_dir / "report" / "report.html"),
                "raw_rows": overall_quality.get("raw_total", 0),
                "valid_rows": overall_quality.get("valid_total", 0),
                "classified_rows": overall_classification.get("classified_total", 0),
                "quality_exclusions": overall_quality.get("raw_total", 0)
                - overall_quality.get("valid_total", 0),
                "classifier": "deterministic-v1",
                "note": "Synthetic fixture only; not a real-world accuracy claim.",
            }
        )
        return
    if args.command == "evaluate":
        config = load_config(args.config)
        payload = evaluate_gold_file(
            args.gold,
            classifier=build_classifier(args.classifier, config),
            config=config,
            output_path=args.output,
        )
        _print_json(payload)
        return
    if args.command == "benchmark":
        fixture = args.fixture or Path(__file__).resolve().parents[2] / "examples" / "benchmark.csv"
        if not fixture.exists():
            raise SystemExit(
                "Benchmark fixture is not present in this installation; "
                "pass --fixture with an independently cleared CSV."
            )
        config = load_config(args.config)
        result = write_benchmark_artifacts(
            fixture,
            output_dir=args.output_dir,
            classifier=build_classifier(args.classifier, config),
            config=config,
        )
        _print_json(result)
        return
    if args.command == "audit":
        payload = audit_payload(args.root, args.forbid)
        _print_json(payload)
        if not payload["clean"]:
            raise SystemExit(1)
        return
    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    main()
