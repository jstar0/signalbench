# Benchmark annotation guide

This fixture is an independently authored, synthetic contract test for
SignalBench. It exists to make changes to the quality gate, classifier adapter,
and evaluation code observable without shipping customer text.

## Unit of annotation

Each CSV row is one short comment. Annotate the **overall stance expressed by
the comment**, not whether the sentence contains a keyword:

- `positive`: the author expresses approval, usefulness, satisfaction, or praise.
- `neutral`: the author asks a question, states a fact, or gives meta feedback without
  taking a stance on the underlying topic.
- `negative`: the author expresses a complaint, concern, distrust, or attack.

## Edge-case rules

- A mixed comment is labeled by its dominant concern. For example, praise plus a
  concrete price complaint is `negative` and should be reviewable.
- Negation is interpreted by the human label even when the transparent baseline
  intentionally misses it. This keeps the benchmark honest about the baseline's
  limitations.
- Chinese rows use the same stance definitions; they are not a separate task.
- Do not infer a label from `slice` or `annotation_note` while evaluating. Those
  columns are audit context only.

## Reproducibility contract

The fixture must remain fixed for a benchmark run. Record its SHA-256, classifier
name, per-label metrics, confusion matrix, and confidence calibration. Any edit
to a row or label creates a new fixture hash and requires an accompanying note.
