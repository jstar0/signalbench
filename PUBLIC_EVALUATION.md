# External public evaluation

SignalBench keeps its self-authored balanced and error fixtures for regression
and also provides a pinned external check against the TweetEval sentiment test
split. The public data is not vendored: the script downloads three files from a
fixed upstream commit, verifies their SHA-256 digests, evaluates rows that pass
the product's input-quality rules, and writes aggregate evidence only.

```bash
uv run python scripts/evaluate_tweeteval.py --output-dir outputs/tweeteval
```

The evidence records both label metrics and coverage loss. TweetEval is English
Twitter data rather than customer-feedback data, and its repository warns that
original task and Twitter restrictions may apply. The result is therefore a
generalization check for this transparent baseline, not a license to
redistribute the source text and not a production accuracy claim.

Source and citation:

- TweetEval repository: <https://github.com/cardiffnlp/tweeteval>
- Pinned commit: `4fbd22cd78421f05b1ecdb4fc5725bc7a7bd8f66`
- Barbieri et al., *TweetEval: Unified Benchmark and Comparative Evaluation for
  Tweet Classification*, Findings of EMNLP 2020.
