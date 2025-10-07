# Design decisions

This document exists to be interrogated. Each entry names a decision that is
visible in the code, the alternative it rejected, and the cost it accepted. The
final section lists the weaknesses a reviewer should find, so nobody has to
discover them by reading between the lines.

## 1. The quality gate runs before inference, and its order is part of the contract

`classify_quality` in [`src/signalbench/quality.py`](../src/signalbench/quality.py)
is an ordered cascade, not a set of independent flags. Blank wins over
everything; display issues (literal zero, replacement markers, control
characters) come before platform notices; boilerplate before link-only; and
`too_short` is measured in letter-or-digit characters, not in bytes or
codepoints, so a string of emoji is rejected as `symbol_or_emoji_only` rather
than counted as short text.

Rejected alternative: score every rule and pick the highest-priority match.
Equivalent output, but the first matching reason is the one a reviewer can act
on, and a cascade makes the precedence auditable.

Cost accepted: reordering the cascade changes historical `quality_reason`
values, so the order is effectively part of the output schema.

## 2. Rows are excluded, never deleted

A rejected row stays in `prepared.csv` with `quality_label`, `quality_reason`,
and `classification_status = "not_applicable"`. Only rows labelled `valid` are
sent to a classifier (`classify_records`).

Rejected alternative: filter at read time. Cheaper, but then "why is my row
count lower than my spreadsheet" has no answer in the artifacts.

## 3. Text normalisation is canonicalising, not cleaning

`normalize_text` applies NFKC, strips zero-width characters, and collapses
whitespace. It does not lowercase, strip punctuation, or remove emoji, because
those change what the row means and the downstream label has to be explainable
against text a human can read.

Casefolding happens later and locally, inside the classifier.

## 4. Duplicates are grouped, ranked, and kept

`prepare_records` groups on `(platform.casefold(), source_sheet,
text_fingerprint)` and assigns a stable `duplicate_rank` in source order. The
group is reported; no row is dropped.

Rejected alternative: deduplicate before classification. It would save calls,
but the operator loses the ability to see that 40% of a sheet is one copied
message, and the row-level artifact would no longer line up with the source.

Cost accepted, and it matters statistically: duplicates remain in the
denominator of every rate in `classification_summary`. See weakness 4.

## 5. The preparation digest deliberately excludes classification fields

`preparation_digest` hashes only `record_id`, source location, platform,
normalised text, fingerprint, quality label, and duplicate group. Sentiment,
confidence, and notes are excluded by construction.

That is what makes resume safe: a checkpoint is bound to the *input and rules*,
so partial classification results can never invalidate the digest that
authorises reusing them.

Rejected alternative: hash the whole prepared record. The digest would change
as soon as the first batch was applied, making resume impossible.

## 6. A checkpoint has to prove four things before it is reused

`_load_checkpoint` plus the guards in `classify_records` reject a checkpoint
unless all of these hold:

| Check | Failure mode it prevents |
| --- | --- |
| `schema_version == 1` | silently misreading a future format |
| `input_digest` matches | merging results from a different input or ruleset |
| `classifier` name matches | mixing a baseline run with a model run |
| `config_sha256` matches (when supplied) | reusing decisions made under different rules |
| every completed id exists in prepared data | stale ids from an edited input |
| no completed id is a non-valid row | a decision attached to an excluded row |
| each stored payload is a well-formed result | a truncated or hand-edited file |

Each stored result is re-validated through `_result_from_payload`: unknown
sentiment label, a negative row without a valid subtype, a non-numeric score, or
a non-finite score all raise `CheckpointMismatch` rather than loading.

Rejected alternative: trust the file because we wrote it. Checkpoints live on
disk between runs, get copied, and get hand-edited during debugging.

## 7. Every artifact write is atomic

`atomic_write_text` and `write_prepared_csv` write to a temporary file in the
destination directory, `flush`, `fsync`, then `os.replace`. A crash mid-write
leaves the previous complete file, never a half-written one.

Rejected alternative: write in place. Simpler, but a crash during checkpoint
write is exactly the moment the file matters most.

## 8. Retries are bounded and capped, and the batch contract is exact

`_classify_with_retry` retries only `TransientClassificationError`, timeout,
connection, and I/O failures, up to `retries` times with exponential backoff
capped at 30 seconds. Adapter contract errors and malformed results surface on
the first attempt. `classify_records` then requires returned results to map
one-to-one onto the batch, and `_apply_result` re-validates every field before
it touches a record.

The strict `ReplayClassifier` goes further: `validate_ids` compares the replay
file's id set against the full set of valid prepared ids and fails on *both*
missing and extra ids, so a partial export cannot pass as a complete analysis.

Rejected alternative: accept whatever the provider returns and fill gaps with
neutral. That produces a complete-looking report from an incomplete run.

## 9. The classifier boundary is a `Protocol`, not a base class

`Classifier` is a two-member structural protocol (`name`, `classify`). A model
adapter satisfies it without importing anything from this package, and the
quality gate, checkpoint validation, report schema, and tests stay unchanged.
`validate_ids` is discovered with `getattr`, so strictness is opt-in per
adapter.

## 10. The default classifier is transparent by choice, and that choice is visible

`DeterministicClassifier` is a weighted lexicon: matched positive and negative
term weights are summed, squashed with `tanh(raw / 1.35)`, and thresholded at
±0.15. Term matching is boundary-aware for ASCII (`good` does not match
`goodbye`) and containment-based for CJK, because CJK text has no word
boundaries to anchor on.

Rejected alternative: ship a small transformer. It would score better and make
the project unrunnable offline, unverifiable in CI, and impossible to explain
line by line.

Cost accepted: this is a lexicon, not a language model. See weaknesses 1-3.

## 11. Missing platform becomes a content-bound opaque source label

`_record_from_row` keeps a supplied platform exactly as the source states it. When the column is absent or blank, it uses `source-` plus the first twelve hex characters of the file SHA-256 rather than deriving a label from `path.name`. A shared HTML report can therefore preserve grouping without exposing a filename such as a client, campaign, or export topic.

Rejected alternative: use one generic `unknown` label, which would merge unrelated sources in the aggregate; or retain the filename and ask every caller to rename it. The digest label is stable for identical content after a move or rename, while the full filename remains only in local row-level artifacts.

Cost accepted: an opaque label is not meaningful to a report reader. Callers who need interpretable source groups must provide an explicit platform column.

Proof: `test_missing_platform_uses_a_content_bound_label_not_the_filename` and `test_shared_report_does_not_leak_a_filename_when_platform_is_missing` cover the source and rendered-report boundaries.

## Known weaknesses

Everything below is tied to current, reproducible behavior.

1. **The classifier is a transparent rule baseline, not general language understanding.**
   The balanced 45-row synthetic fixture scores 0.956 accuracy / 0.955 macro-F1,
   while the concentrated 16-row error fixture scores 0.500 / 0.376. Unseen
   paraphrases, Chinese sarcasm, negated negative cues, and positive wording
   outside the lexicon remain weak. On the pinned 12,284-row TweetEval sentiment
   test split it scores 0.512 / 0.365 and predicts neutral for 86.6% of rows.
   These results and fixture hashes must be reported together; TweetEval is
   Twitter data rather than a product-feedback production estimate.
2. **Confidence is a review heuristic.** The balanced fixture has ECE 0.393,
   so the numeric value must not be read as a calibrated probability. The UI
   therefore presents rule hits, conflicts, and review state instead of calling
   the value a probability.
3. **Duplicates affect statistical framing.** The workbench lets an operator
   keep all copies or explicitly keep the first in each group. If copies remain,
   the Wilson interval treats them as distinct observations and can look too
   narrow. The input is often a census of a period, so the interval is only a
   descriptive aid for an assumed ongoing stream.
4. **`author_hash` is pseudonymous.** It is an unsalted, truncated SHA-256 and
   can be brute-forced for names from a small population. Raw author values stay
   out of prepared artifacts, but a deployment needing stronger protection
   should use an operator-held HMAC key and accept losing portable joins.
5. **Record identity still depends on row position.** Moving a byte-identical
   file preserves IDs, but inserting a row changes later source-row positions.
   An explicit source record ID reduces this instability when the export offers
   one.
6. **The v1 resource envelope is deliberately bounded and in memory.** A run is
   limited to 100 files, 16 MiB per file, 128 columns, 10,000 rows, and 8,192
   text characters. Larger workloads need a separate streaming duplicate,
   checkpoint, and export design.
7. **The standalone wheel uses a narrow structure-comparison fallback.** In the
   monorepo the adapter calls DataSentinel. A wheel without that neighboring
   package computes the same declared categorical JS / numeric PSI evidence
   with the standard library so it cannot bind to an incompatible PyPI
   namesake. This fallback covers the four fields used here; it is not the full
   DataSentinel lab or its threshold-calibration surface.
8. **Very short jobs finish before browser pause controls are easy to observe.**
   Pause, resume, cancel, interrupted recovery, event ordering, and digest
   compatibility are verified at batch boundaries in the job tests. The bundled
   eight-row walkthrough usually completes before a person can click pause.
9. **The local API filters and cursor-pages quality and result rows.** The URL
   stores the current batch or run, filters, sort order, review state, page size,
   and cursor. A refresh restores a completed result view or an active job's
   batch/status. Each request still scans and sorts at most 10,000 bounded rows;
   pagination limits the browser payload rather than claiming streaming queries.

## What was fixed after this review

- `audit` no longer writes a matched forbidden term into its own output; it
  reports the term's position in the caller's list instead. The README claim
  that the command is safe for CI is now true for both finding kinds. Generated
  dependency trees such as `web/node_modules` are excluded as documented.
- The built wheel no longer resolves an incompatible public package named
  `datasentinel`. Monorepo development uses the neighboring package; standalone
  installs use the four-field compatibility implementation and have been run in
  a new virtual environment.
- Workbench exports now follow the active topic, sentiment, and date filters;
  result review preserves the filter, and quality totals expose the actual
  included/excluded denominator after duplicate review.
- Quality/result pagination and search now stay server-side; result review
  status and cursor survive reload, and a refresh restores an active job through
  its batch/job IDs. A dedicated “keep first duplicate” action applies beyond
  the 500-row manual review request limit in one SQLite revision.
- The production client is built into `src/signalbench/static`, included in
  the wheel, and served by `signalbench serve` after standalone installation.
- `report.json` carries `input_summary` (digests and counts) instead of the full
  `input_manifest`, so source file names and original column headers stay local.
- `read_prepared_csv` re-runs the quality gate and rejects a row whose label
  disagrees with its text. Previously only the text fingerprint was verified, so
  editing `invalid,link_only` to `valid` let an excluded row reach the
  classifier with no warning.
- The negative-rate Wilson interval has tests, which the README had claimed
  before any existed.
- The `prepare` command's help no longer promises deduplication that the code
  never performed; duplicates are grouped and tagged, not removed.
- The v1 ingestion boundary now checks file count/bytes before hashing, then
  rejects oversized headers, records, text and metadata. `prepared.csv` has the
  same record cap. The constraints are deliberately conservative because the
  duplicate grouping and artifact writers keep rows in memory.

## 11. The v1 ingestion envelope is bounded before records are materialised

SignalBench needs all prepared rows for duplicate grouping, digesting, resume, CSV/XLSX export, and reporting. That is a valid small-to-medium table tradeoff, but it cannot safely accept unbounded trees. `io.py` now limits a run to 100 files, 16 MiB per file, 128 columns, 10,000 non-empty records, 8,192 text characters per row, and 512 characters for selected metadata. It checks a file size before hashing and checks the record count as rows are appended; a later `prepared.csv` read is capped too.

Rejected alternative: call the existing generator "bounded memory" while letting an input dictate the allocation. Another rejected alternative is to silently truncate rows, which would change evidence without an operator seeing it. The limits fail closed with a file/row-specific error.

Cost accepted: this is not a streaming solution and it intentionally rejects a legitimate 20 MiB export. A future streaming implementation needs a separate duplicate, checkpoint, and artifact contract rather than raising these values.

Proof: the resource-envelope cases in [`tests/test_io.py`](../tests/test_io.py) pin file-count, byte, column, text, metadata, source-record and prepared-record rejection.

## 12. Spreadsheet exports are normalized as deterministic ZIP artifacts

Openpyxl writes wall-clock core properties and ZIP member timestamps. The
writer now fixes workbook metadata, normalizes the final `modified` field, and
rewrites ZIP entries in sorted order with a fixed timestamp and compression.
The workbook remains a normal XLSX file, but its bytes no longer change just
because it was exported later or to a different path.

Rejected alternative: hash only CSV and treat XLSX as an unverified convenience
copy. That would leave a documented output outside the reproducibility claim.

## Scope

Synthetic examples only. No client rows, author names, credentials, provider
responses, or generated `outputs/` are in this repository. Metrics in the README
are reproducible from the bundled fixtures with the recorded commands, and none
of them is a real-world accuracy claim.
