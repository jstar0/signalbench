from __future__ import annotations

from pathlib import Path

from signalbench.audit import audit_payload, audit_tree


def test_audit_clean_tree_ignores_generated_directories(tmp_path: Path) -> None:
    (tmp_path / "safe.py").write_text("print('ok')\n", encoding="utf-8")
    generated = tmp_path / "outputs"
    generated.mkdir()
    generated_secret = "s" + "k-" + "12345678901234567890"
    (generated / "secret.txt").write_text(
        "OPENAI_API_KEY=" + generated_secret + "\n", encoding="utf-8"
    )
    dependencies = tmp_path / "web" / "node_modules" / "typescript"
    dependencies.mkdir(parents=True)
    (dependencies / "lib.dom.d.ts").write_text(
        "api" + "Key=" + "library_declaration_placeholder\n", encoding="utf-8"
    )
    payload = audit_payload(tmp_path)
    assert payload["clean"] is True
    assert payload["files_scanned"] == 1


def test_audit_detects_secret_and_forbidden_term(tmp_path: Path) -> None:
    path = tmp_path / "unsafe.py"
    secret = "s" + "k-" + "12345678901234567890"
    path.write_text("token = '" + secret + "'\n# internal-client\n", encoding="utf-8")
    findings = audit_tree(tmp_path, forbidden_terms=["internal-client"])
    assert {finding.rule for finding in findings} == {"openai_like_key", "forbidden_term"}
    assert all("sk-" not in finding.detail for finding in findings)
