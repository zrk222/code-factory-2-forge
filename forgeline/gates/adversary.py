"""The grumpy adversary — assumes the code is broken/insecure and makes the
generator prove otherwise. Executable heuristics (no LLM needed to be
useful); an optional LLM adversary can be layered behind the same interface.
It does NOT need to be right — it needs to force proof (tests + arch completeness)."""
from __future__ import annotations
import ast, re
from pathlib import Path

DANGER = {
    "eval(": "A_EVAL arbitrary eval",
    "exec(": "A_EXEC arbitrary exec",
    "os.system": "A_SHELL shell execution",
    "subprocess": "A_SUBPROC subprocess use",
    "pickle.load": "A_PICKLE unsafe deserialization",
    "verify=False": "A_TLS TLS verification disabled",
    "shell=True": "A_SHELL_TRUE shell=True injection surface",
}
SECRET = re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*=\s*['\"][A-Za-z0-9/\+_-]{12,}['\"]")

def grumpy_review(src_dir: Path, require_tests: bool = True) -> tuple[bool, list[str]]:
    """Returns (satisfied, complaints). Grumpy is satisfied only when it can
    find NO obvious sin AND the generator supplied tests to prove correctness."""
    src_dir = Path(src_dir); complaints = []
    py = list(src_dir.rglob("*.py"))
    for p in py:
        if p.name.startswith("test_") or p.parent.name == "tests":
            continue
        text = p.read_text()
        for needle, msg in DANGER.items():
            if needle in text:
                complaints.append(f"{msg} in {p.name}")
        if SECRET.search(text):
            complaints.append(f"A_SECRET hard-coded credential in {p.name}")
        # bare except = hiding failure, which grumpy hates
        try:
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.ExceptHandler) and node.type is None:
                    complaints.append(f"A_BARE_EXCEPT swallowed error in {p.name}")
        except SyntaxError:
            complaints.append(f"A_SYNTAX {p.name} does not parse")
    if require_tests:
        test_files = [p for p in py if p.name.startswith("test_") or p.parent.name == "tests"]
        if not test_files:
            complaints.append("A_NO_PROOF no tests supplied — prove it works")
    return (len(complaints) == 0, complaints)
