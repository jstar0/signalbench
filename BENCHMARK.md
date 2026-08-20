# Benchmark boundary

SignalBench now ships two deliberately different evaluation fixtures:

- `examples/gold.csv` is a twelve-row smoke fixture for unit-level contract
  checks.
- `examples/benchmark.csv` is a 45-row, balanced, independently authored
  synthetic fixture with English/Chinese, factual, question, mixed-signal, and
  negation-edge slices. Its annotation rules and source notes are recorded in
  [`examples/benchmark_annotation.md`](examples/benchmark_annotation.md) and
  [`examples/benchmark_manifest.json`](examples/benchmark_manifest.json).

Neither fixture is a real-world accuracy claim. The benchmark is valuable
because it makes regressions and confidence behavior visible while keeping
customer text, provider responses, and credentials out of the repository.

`examples/error_cases.csv` is a separate, deliberately difficult 16-row set
covering negation, sarcasm, mixed stance, unknown positive wording and unknown
topics. See [`ERROR_CASES.md`](ERROR_CASES.md). It is evaluated separately so
its concentrated failures do not get blended into the balanced headline.

## Reproducible benchmark command

From a source checkout:

```bash
uv run signalbench benchmark --output-dir outputs/benchmark
```

The command writes:

```text
outputs/benchmark/
├── metrics.json   # machine-readable source of truth
└── summary.md     # review-friendly aggregate summary
```

`metrics.json` records the fixture SHA-256, classifier version, accuracy,
macro-F1, per-label support/precision/recall/F1, confusion matrix, predicted
versus actual distributions, and coarse confidence calibration (ECE plus
reliability bins). The summary contains no row-level text.

To evaluate a separately cleared fixture, pass it explicitly:

```bash
uv run signalbench benchmark \
  --fixture /path/to/cleared.csv \
  --output-dir /tmp/signalbench-benchmark
```

The lower-level command remains available for one-off contract checks:

```bash
uv run signalbench evaluate \
  --gold examples/gold.csv \
  --output /tmp/signalbench-gold.json
```

## Interpreting the baseline

The bundled deterministic classifier is intentionally transparent and
keyword-based. It should do well on explicit cues and expose failures on
negation, sarcasm, and domain-specific phrasing. Do not tune the fixture to
hide those failures. A future model adapter should report the same fixture
hash and metrics before any accuracy comparison is treated as meaningful.
