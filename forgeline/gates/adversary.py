"""The grumpy adversary — assumes the code is broken/insecure and makes the
generator prove otherwise. Executable heuristics (no LLM needed to be
useful); an optional LLM adversary can be layered behind the same interface.
It does NOT need to be right — it needs to force proof (tests + arch completeness)."""
from __future__ import annotations
import ast, re
from pathlib import Path
from typing import Iterable

from ..source_scope import SOURCE_SUFFIXES, analyze_source, iter_source_files

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

def grumpy_review(
    src_dir: Path,
    require_tests: bool = True,
    *,
    source_paths: Iterable[Path] | None = None,
    test_paths: Iterable[Path] | None = None,
) -> tuple[bool, list[str]]:
    """Returns (satisfied, complaints). Grumpy is satisfied only when it can
    find NO obvious sin AND the generator supplied tests to prove correctness."""
    src_dir = Path(src_dir).resolve(); complaints = []
    inventory = (
        iter_source_files(src_dir, paths=source_paths)
        if source_paths is not None
        else iter_source_files(src_dir, suffixes=SOURCE_SUFFIXES)
    )
    for p in inventory:
        if p.name.startswith("test_") or p.parent.name in {"tests", "test", "__tests__"} or ".test." in p.name or ".spec." in p.name:
            continue
        parsed = analyze_source(p, src_dir)
        text = parsed["text"]
        for needle, msg in DANGER.items():
            if needle in text:
                complaints.append(f"{msg} in {p.relative_to(src_dir).as_posix()}")
        if SECRET.search(text):
            complaints.append(f"A_SECRET hard-coded credential in {p.relative_to(src_dir).as_posix()}")
        # bare except = hiding failure, which grumpy hates
        if parsed["language"] == "python" and parsed["status"] == "ok":
            for node in ast.walk(parsed["tree"]):
                if isinstance(node, ast.ExceptHandler) and node.type is None:
                    complaints.append(f"A_BARE_EXCEPT swallowed error in {p.relative_to(src_dir).as_posix()}")
        elif parsed["status"] == "syntax_error":
            complaints.append(f"A_SYNTAX {p.relative_to(src_dir).as_posix()} does not parse")
        elif parsed["status"] == "parser_unsupported":
            complaints.append(f"A_PARSER_UNSUPPORTED {p.relative_to(src_dir).as_posix()}: {parsed.get('reason', parsed['language'])}")
    if require_tests:
        test_inventory = (
            iter_source_files(src_dir, paths=test_paths)
            if test_paths is not None
            else iter_source_files(src_dir, suffixes=SOURCE_SUFFIXES)
        )
        test_files = [p for p in test_inventory if p.name.startswith("test_") or p.parent.name in {"tests", "test", "__tests__"} or ".test." in p.name or ".spec." in p.name]
        if not test_files:
            complaints.append("A_NO_PROOF no tests supplied — prove it works")
    return (len(complaints) == 0, complaints)
