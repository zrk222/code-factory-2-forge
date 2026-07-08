"""Judge Agent — directory & interface consistency (executable, LLM-free).
Checks the filled code matches the SSAT scaffold: no stubs left, every
declared function implemented, imports resolve within the tree."""
from __future__ import annotations
import ast
from pathlib import Path
from ..ssat import check_erosion

def judge_consistency(ssat: dict, src_dir: Path) -> tuple[bool, list[str]]:
    findings = []
    src_dir = Path(src_dir)
    for mod in ssat.get("modules", []):
        p = src_dir/mod["path"]
        if not p.exists():
            findings.append(f"J_MISSING {mod['path']}"); continue
        text = p.read_text()
        if "raise NotImplementedError" in text or "# FILL" in text or "TODO" in text:
            findings.append(f"J_STUB unfilled body in {mod['path']}")
        try:
            ast.parse(text)
        except SyntaxError as e:
            findings.append(f"J_SYNTAX {mod['path']}: {e}")
    # structural erosion is a judge concern too
    for v in check_erosion(ssat, src_dir):
        findings.append(f"J_ARCH {v.code} {v.message}")
    return (len(findings) == 0, findings)
