# Error-case evaluation boundary

`examples/error_cases.csv` is an independently authored, redistribution-safe
fixture for product QA. It currently contains 16 cases and deliberately concentrates cases that a transparent
keyword baseline is expected to miss: negation, sarcasm, praise plus a concrete
failure, domain-specific complaints, Chinese phrasing outside the configured
lexicon, and valid feedback outside the four named topics.

It is not a balanced accuracy benchmark and is not sampled from customer data.
Its purpose is to keep visible failures visible. The expected label follows the
same stance rules as `examples/benchmark_annotation.md`; the slice and note are
audit fields and are not passed to the classifier.

Run it with:

```bash
uv run signalbench benchmark \
  --fixture examples/error_cases.csv \
  --output-dir outputs/error-cases
```

Interpret low accuracy here as a useful inventory of unsupported language, not
as a production error-rate estimate. The workbench should route conflicts and
questions to review, show structured `rule_hits`, and retain `general` rather
than inventing a topic. Improvements should be checked against both this set
and the balanced 45-row benchmark so a narrow fix does not silently regress
clear cases.

Current deterministic-v1 evidence: the balanced 45-row fixture reaches 0.956
accuracy / 0.955 macro-F1, while this concentrated error set reaches 0.500
accuracy / 0.376 macro-F1. The gap is the product limitation: the rule baseline
handles explicit cues and several declared conflicts, but it does not reliably
understand unseen paraphrases, Chinese sarcasm or negation of a negative cue.
