# Security and data handling

SignalBench is intended to be run on data that the operator is authorized to
process. The repository ships synthetic examples only.

- Keep raw exports, credentials, model responses, and generated `outputs/`
  outside version control.
- Ingestion stores an author digest rather than the raw author value. It is
  unsalted pseudonymisation, not anonymity; treat prepared CSV files as local
  artifacts. A keyed HMAC deployment needs an operator-held secret and is not
  configured by this repository.
- The default classifier is offline and deterministic. Any future remote
  adapter must document endpoint, retention, redaction, and failure behavior
  before it is enabled.
- Reports contain aggregates and source-sheet labels, not comment text. Review
  the generated manifest before sharing it because input filenames can still
  reveal context.
- When platform is absent, report grouping uses an opaque content-digest label,
  never the source filename. The local prepared CSV and full input manifest
  still retain filenames for traceability and must remain local artifacts.
- The v1 ingestion boundary rejects more than 100 files, a source file larger
  than 16 MiB, more than 128 columns, more than 10,000 non-empty records, or
  oversized text/metadata fields before the in-memory pipeline expands them.
  This is a resource boundary, not a substitute for a streaming deployment.

The built-in audit is a high-confidence heuristic, not a guarantee; inspect
new adapters and generated artifacts manually before publication.

For a suspected vulnerability, do not attach private datasets or secrets to a
public issue. Contact the repository owner through an agreed private channel.
