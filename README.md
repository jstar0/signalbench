# SignalBench

**Turn messy CSV/TSV/XLSX comments into quality-gated, resumable reports.**

SignalBench turns heterogeneous CSV/TSV/XLSX exports into a traceable analysis
artifact:

```text
source tables → schema contract → quality gate → stable fingerprints
             → resumable classification → aggregate report (CSV/JSON/HTML)
```

The bundled demo is deliberately small and synthetic. It is meant to show the
complete user path in one command, not to imply production accuracy:

```bash
uv run signalbench demo --output-dir outputs/demo
open outputs/demo/report/report.html
```

It is deliberately safe as a baseline: the default classifier is deterministic,
offline, and explainable. No network call is made and no raw author value is
written to the prepared output (only a short audit hash).

## Why this is more than a sentiment demo

- **Input contract** — column aliases are resolved explicitly; missing text
  columns fail with an actionable error instead of silently producing an empty
  dataset.
- **Quality before inference** — blank, link-only, boilerplate, platform-notice,
  encoding-corrupted, symbol-only, and too-short rows are separated from valid
  evidence.
- **Stable evidence IDs** — every row gets a deterministic ID and text
  fingerprint for the same source locations, so a result can be traced back to
  source file/sheet/row without exposing an absolute path.
- **Resume without guesswork** — checkpoints carry a preparation digest and
  classifier version. A checkpoint from another input is rejected rather than
  merged accidentally; writes are atomic.
- **Uncertainty is visible** — review flags, confidence, and a Wilson 95%
  interval accompany the negative-rate headline.
- **Publishable artifacts** — row-level CSV and the full input manifest stay
  local. The aggregate CSV/JSON and the self-contained HTML report carry counts,
  rates, and digests only: `report.json` records file hashes and counts, never
  source file names or the original column headers.
