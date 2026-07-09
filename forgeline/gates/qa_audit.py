"""Deep QA audit — stricter than the grumpy adversary's heuristics. Scores
generated code on coverage-intent, cyclomatic complexity, security surface,
and documentation, producing a QA grade that gates shipping. This is the
'stricter QA audit' layer: quantitative, thresholded, receipted."""
from __future__ import annotations
import ast, re
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class QAReport:
    coverage_intent: float = 0.0     # ratio of public funcs with a matching test
    max_complexity: int = 0          # highest cyclomatic complexity found
    security_score: int = 100        # 100 = clean; deductions per finding
    doc_ratio: float = 0.0           # public funcs with docstrings
    grade: str = "F"
    findings: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    function_metrics: list = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.grade in ("A", "B") and self.security_score >= 80

    @property
    def attribution(self):
        from ..attribution import Attribution, FailureClass, UnitResult
        units = []
        for metric in self.function_metrics:
            failures = []
            failure_class = None
            if metric["complexity"] > 10:
                failures.append(f"complexity={metric['complexity']}; threshold=10")
                failure_class = FailureClass.COMPLEXITY_EXCEEDED
            if not metric["tested"]:
                failures.append("coverage_intent=0; required=1")
                failure_class = failure_class or FailureClass.INCONSISTENT_LOGIC
            units.append(UnitResult(
                unit=f"qa_audit:{metric['function']}",
                stage="qa_audit",
                passed=not failures,
                evidence="metrics within thresholds" if not failures else "; ".join(failures),
                failure_class=failure_class,
            ))
        if not units:
            units.append(UnitResult(
                "qa_audit:<no-public-functions>", "qa_audit", False,
                "no public functions were available to grade",
                FailureClass.STUB_UNFILLED,
            ))
        return Attribution("qa_audit", len(units), sum(unit.passed for unit in units), units)

def _complexity(node: ast.FunctionDef) -> int:
    """Cyclomatic complexity: 1 + branch points."""
    c = 1
    for n in ast.walk(node):
        if isinstance(n, (ast.If, ast.For, ast.While, ast.ExceptHandler, ast.With, ast.Assert)):
            c += 1
        elif isinstance(n, ast.BoolOp):
            c += len(n.values) - 1
        elif isinstance(n, ast.IfExp):
            c += 1
    return c

SEC_PATTERNS = {
    r"\beval\(": ("CRITICAL", 40, "eval() — arbitrary code execution"),
    r"\bexec\(": ("CRITICAL", 40, "exec() — arbitrary code execution"),
    r"shell\s*=\s*True": ("HIGH", 25, "shell=True — command injection surface"),
    r"pickle\.loads?\(": ("HIGH", 20, "pickle — unsafe deserialization"),
    r"verify\s*=\s*False": ("HIGH", 20, "TLS verification disabled"),
    r"(?i)(password|secret|api_key|token)\s*=\s*['\"][^'\"]{8,}": ("CRITICAL", 40, "hard-coded credential"),
    r"md5\(": ("MEDIUM", 10, "MD5 — weak hash"),
    r"random\.random\(\)": ("LOW", 5, "non-cryptographic randomness"),
}

def qa_audit(src_dir: Path) -> QAReport:
    src_dir = Path(src_dir)
    r = QAReport()
    code_files = [p for p in src_dir.rglob("*.py")
                  if not p.name.startswith("test_") and p.parent.name != "tests"]
    test_files = [p for p in src_dir.rglob("*.py")
                  if p.name.startswith("test_") or p.parent.name == "tests"]
    all_test_text = "\n".join(p.read_text() for p in test_files)

    public_funcs, tested, documented, complexities = [], 0, 0, []
    for p in code_files:
        try:
            tree = ast.parse(p.read_text())
        except SyntaxError as e:
            r.findings.append(f"QA_SYNTAX {p.name}: {e}"); continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
                public_funcs.append(node.name)
                # coverage-intent: is the function name referenced in tests?
                if re.search(rf"\b{re.escape(node.name)}\b", all_test_text):
                    tested += 1
                if ast.get_docstring(node):
                    documented += 1
                cx = _complexity(node)
                complexities.append((node.name, cx))
                r.function_metrics.append({
                    "function": f"{p.name}:{node.name}",
                    "complexity": cx,
                    "tested": bool(re.search(rf"\b{re.escape(node.name)}\b", all_test_text)),
                    "documented": bool(ast.get_docstring(node)),
                })
        # security scan
        text = p.read_text()
        for pat, (sev, deduct, msg) in SEC_PATTERNS.items():
            if re.search(pat, text):
                r.security_score -= deduct
                r.findings.append(f"QA_SEC[{sev}] {msg} in {p.name}")

    n = len(public_funcs) or 1
    r.coverage_intent = round(tested / n, 2)
    r.doc_ratio = round(documented / n, 2)
    r.max_complexity = max((c for _, c in complexities), default=0)
    r.security_score = max(r.security_score, 0)
    for name, cx in complexities:
        if cx > 10:
            r.findings.append(f"QA_COMPLEXITY {name}() complexity {cx} > 10 — refactor")

    # composite grade
    score = 0
    score += 35 * r.coverage_intent
    score += 25 * (1 if r.max_complexity <= 10 else max(0, 1 - (r.max_complexity-10)/10))
    score += 25 * (r.security_score / 100)
    score += 15 * r.doc_ratio
    r.metrics = {"coverage_intent": r.coverage_intent, "max_complexity": r.max_complexity,
                 "security_score": r.security_score, "doc_ratio": r.doc_ratio,
                 "composite": round(score, 1)}
    r.grade = ("A" if score >= 85 else "B" if score >= 70 else
               "C" if score >= 55 else "D" if score >= 40 else "F")
    return r
