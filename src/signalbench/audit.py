from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

_EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "outputs",
    "dist",
    "build",
}
_TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".csv",
    ".tsv",
    ".xml",
    ".ini",
    ".cfg",
    ".html",
    ".css",
    ".js",
    ".ts",
    ".lock",
}
_TEXT_FILENAMES = {"Dockerfile", "Makefile", "LICENSE", ".gitignore"}
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("openai_like_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "assigned_secret",
        re.compile(
            r"\b(?:api[_-]?key|access[_-]?token|secret|password)\b\s*[:=]\s*['\"]?[A-Za-z0-9_./+=:-]{16,}",
            re.IGNORECASE,
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class AuditFinding:
    path: str
    line: int
    rule: str
    detail: str


def _iter_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*"), key=lambda item: str(item).casefold()):
        if not path.is_file() or (
            path.suffix.casefold() not in _TEXT_SUFFIXES
            and path.name not in _TEXT_FILENAMES
            and not path.name.startswith(".env")
        ):
            continue
        try:
            relative_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part in _EXCLUDED_DIRS for part in relative_parts):
            continue
        yield path


def audit_tree(root: Path, forbidden_terms: Sequence[str] = ()) -> list[AuditFinding]:
    """Find high-confidence secret patterns and explicitly forbidden terms."""

    root = root.expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)
    findings: list[AuditFinding] = []
    forbidden = tuple(term.casefold() for term in forbidden_terms if term)
    for path in _iter_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        relative = str(path.relative_to(root))
        for line_number, line in enumerate(text.splitlines(), start=1):
            for rule, pattern in _SECRET_PATTERNS:
                match = pattern.search(line)
                if match:
                    findings.append(
                        AuditFinding(
                            relative,
                            line_number,
                            rule,
                            "[redacted secret-like value]",
                        )
                    )
            folded = line.casefold()
            for term in forbidden:
                if term in folded:
                    # The caller already knows its own forbidden list; echoing the
                    # matched term would put it into CI logs, which is exactly what
                    # the redaction above exists to prevent.
                    findings.append(
                        AuditFinding(
                            relative,
                            line_number,
                            "forbidden_term",
                            f"[redacted forbidden term #{forbidden.index(term) + 1}]",
                        )
                    )
    return findings


def audit_payload(root: Path, forbidden_terms: Sequence[str] = ()) -> dict[str, object]:
    findings = audit_tree(root, forbidden_terms)
    return {
        "clean": not findings,
        "root": str(root.expanduser().resolve()),
        "files_scanned": sum(1 for _ in _iter_files(root.expanduser().resolve())),
        "findings": [asdict(finding) for finding in findings],
    }
