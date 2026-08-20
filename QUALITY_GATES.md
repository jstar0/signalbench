# SignalBench quality gates

Status: **PROFILE_READY** on the local gate protocol (all eight gates backed by
a reproducible artifact). Publication itself is a separate
decision and is not authorized yet.

The project is not called “神作” because it has many files. It earns that label
only when the following evidence is repeatable from a clean checkout.

| Gate | Current evidence | Status |
| --- | --- | --- |
| Positioning | README states the user problem, boundaries, and output contract | PASS |
| Boundary / data hygiene | Synthetic fixture only; author values become hashes; private ledger records exclusions | PASS |
| Vertical slice | `uv run signalbench run examples/comments.csv --output-dir ...` writes prepared, classified, checkpoint, and report artifacts | PASS |
| Technical proof | 43 tests cover schema mapping, deterministic XLSX export, Unicode quality, duplicates, classifier/replay behavior, retries, digest mismatch, evaluation metrics and calibration, benchmark artifacts, directory isolation, empty-input failures, report output, and the bundled demo command | PASS |
| Engineering quality | `ruff check` passes; CI runs install, lint, tests, end-to-end run, evaluation, and public-hygiene audit | PASS |
| Reliability | Injected failure after one batch leaves an atomic checkpoint; rerun classifies only the remaining IDs | PASS |
| Reproducibility | Stable record IDs, preparation/config digests, deterministic classifier, sorted JSON/CSV output, and an isolated wheel run | PASS |
| Public hygiene | `uv run signalbench audit --root .` is clean; no credentials, raw source exports, absolute input paths, or generated outputs in the package | PASS |
| Adversarial questioning | [docs/design-decisions.md](docs/design-decisions.md) records ten decisions with the alternative each rejected, and names ten weaknesses including the unsalted author hash, the absent negation model, the hand-tuned confidence constants, and the duplicate-inflated Wilson interval | PASS |
| Isolated checkout | `scripts/clean_room_verify.sh signalbench` exports only tracked files into a pristine tree, then installs from the lockfile, runs the suite, lints, builds, and runs the README quick-start command; the tracked-file manifest is hashed | PASS |

## Reproduce the evidence

```bash
uv sync --extra dev
uv run pytest
uv run signalbench run examples/comments.csv --output-dir /tmp/signalbench-proof
```

To verify recovery explicitly:

```bash
uv run signalbench prepare examples/comments.csv --output-dir /tmp/signalbench-recovery
uv run signalbench classify \
  --prepared /tmp/signalbench-recovery/prepared.csv \
  --output-dir /tmp/signalbench-recovery \
  --batch-size 2 \
  --fail-after-batches 1   # expected non-zero exit; checkpoint is still valid
uv run signalbench classify \
  --prepared /tmp/signalbench-recovery/prepared.csv \
  --output-dir /tmp/signalbench-recovery \
  --batch-size 2
```

The last command must finish with all valid rows classified. A checkpoint with
an altered prepared digest or rules digest is rejected rather than silently
reused.

## Before profile publication

1. If a production-style claim is ever needed, add an independently labeled,
   synthetic-or-cleared benchmark beyond the bundled regression fixture and
   report accuracy, macro-F1, and calibration—not only a screenshot.
2. Add a model-backed provider behind the same `Classifier` protocol and test both
   against the benchmark.
3. Repeat the isolated verification on a *different machine*. The current
   check runs in an isolated tree on this machine, which catches missing files,
   stale lockfiles, and README drift, but not platform assumptions.
4. Re-run the public-hygiene scan after any real-data evaluation; never commit
   client rows, model responses, credentials, or uncleared third-party assets.
