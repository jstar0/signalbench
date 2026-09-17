# Contributing to SignalBench

Thanks for helping improve the project. Small, reproducible changes are easier
to review than broad rewrites.

## Development

```bash
uv sync --extra dev
uv run pytest
uv run --with ruff ruff check src tests
```

Please add a focused test for behavior changes. Do not commit raw exports,
author names, model responses, API keys, or generated `outputs/` directories.

## Pull requests

Describe the user-facing problem, the behavior that changed, and the command
used to verify it. If a change affects report fields or checkpoint semantics,
call that out explicitly so downstream consumers can review the contract.
