"""Feature-scoped, language-aware QA evidence with explicit hard thresholds."""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..source_scope import SOURCE_SUFFIXES, analyze_source, iter_source_files, read_text

COMPLEXITY_LIMIT = 10


@dataclass
class QAReport:
    coverage_intent: float = 0.0
    max_complexity: int = 0
    security_score: int = 100
    doc_ratio: float = 0.0
    grade: str = "F"
    findings: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    function_metrics: list[dict] = field(default_factory=list)
    skipped_paths: list[str] = field(default_factory=list)
    scope: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.grade in ("A", "B") and self.security_score >= 80 and self.max_complexity <= COMPLEXITY_LIMIT

    @property
    def attribution(self):
        from ..attribution import Attribution, FailureClass, UnitResult
        units = []
        for metric in self.function_metrics:
            failures = []
            failure_class = metric.get("failure_class")
            if metric["complexity"] > COMPLEXITY_LIMIT:
                failures.append(f"complexity={metric['complexity']}; threshold={COMPLEXITY_LIMIT}; policy=hard")
                failure_class = FailureClass.COMPLEXITY_EXCEEDED
            if not metric["tested"]:
                failures.append("coverage_intent=0; required=1")
                failure_class = failure_class or FailureClass.INCONSISTENT_LOGIC
            units.append(UnitResult(
                unit=f"qa_audit:{metric['function']}", stage="qa_audit", passed=not failures,
                evidence="metrics within thresholds" if not failures else "; ".join(failures),
                failure_class=failure_class,
            ))
        for finding in self.findings:
            if finding.startswith("QA_PARSER_UNSUPPORTED"):
                units.append(UnitResult(f"qa_audit:{finding.split()[1]}", "qa_audit", False, finding, FailureClass.PARSER_UNSUPPORTED))
            elif finding.startswith("QA_SYNTAX"):
                units.append(UnitResult(f"qa_audit:{finding.split()[1]}", "qa_audit", False, finding, FailureClass.SYNTAX_ERROR))
        if not units:
            units.append(UnitResult("qa_audit:<no-public-functions>", "qa_audit", False,
                                    "no supported public functions were available to grade", FailureClass.PARSER_UNSUPPORTED))
        return Attribution("qa_audit", len(units), sum(unit.passed for unit in units), units)


def _complexity(node: ast.FunctionDef) -> int:
    value = 1
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.For, ast.While, ast.ExceptHandler, ast.With, ast.Assert)):
            value += 1
        elif isinstance(child, ast.BoolOp):
            value += len(child.values) - 1
        elif isinstance(child, ast.IfExp):
            value += 1
    return value


SEC_PATTERNS = {
    r"\beval\(": ("CRITICAL", 40, "eval() - arbitrary code execution"),
    r"\bexec\(": ("CRITICAL", 40, "exec() - arbitrary code execution"),
    r"shell\s*=\s*True": ("HIGH", 25, "shell=True - command injection surface"),
    r"pickle\.loads?\(": ("HIGH", 20, "pickle - unsafe deserialization"),
    r"verify\s*=\s*False": ("HIGH", 20, "TLS verification disabled"),
    r"(?i)(password|secret|api_key|token)\s*=\s*['\"][^'\"]{8,}": ("CRITICAL", 40, "hard-coded credential"),
    r"md5\(": ("MEDIUM", 10, "MD5 - weak hash"),
    r"random\.random\(\)": ("LOW", 5, "non-cryptographic randomness"),
}


def _is_test(path: Path) -> bool:
    return path.name.startswith("test_") or path.parent.name in {"tests", "test", "__tests__"} or ".test." in path.name or ".spec." in path.name


def _metric(report: QAReport, path: Path, name: str, complexity: int, documented: bool, test_text: str, root: Path) -> None:
    tested = bool(re.search(rf"\b{re.escape(name)}\b", test_text)) and not name.startswith("<anonymous")
    report.function_metrics.append({
        "function": f"{path.relative_to(root).as_posix()}:{name}", "complexity": complexity,
        "tested": tested, "documented": documented,
    })


def qa_audit(src_dir: Path, *, source_paths: Iterable[Path] | None = None) -> QAReport:
    """Grade a reviewed feature slice, or an explicitly requested pruned repository scan."""
    root = Path(src_dir).resolve()
    report = QAReport(scope={"kind": "feature" if source_paths is not None else "repo_wide"})
    inventory = (
        iter_source_files(root, paths=source_paths, skipped=report.skipped_paths)
        if source_paths is not None
        else iter_source_files(root, suffixes=SOURCE_SUFFIXES, skipped=report.skipped_paths)
    )
    code_files = [path for path in inventory if not _is_test(path)]
    test_files = [path for path in iter_source_files(root, suffixes=SOURCE_SUFFIXES, skipped=report.skipped_paths) if _is_test(path)]
    report.scope["code_files"] = [path.relative_to(root).as_posix() for path in code_files]
    test_text = "\n".join(text for path in test_files if (text := read_text(path, report.skipped_paths)) is not None)

    for path in code_files:
        parsed = analyze_source(path, root)
        status = parsed["status"]
        if status == "parser_unsupported":
            report.findings.append(f"QA_PARSER_UNSUPPORTED {path.relative_to(root).as_posix()}: {parsed.get('reason', parsed['language'])}")
            continue
        if status == "syntax_error":
            report.findings.append(f"QA_SYNTAX {path.relative_to(root).as_posix()}: {parsed['error']}")
            continue
        text = parsed["text"]
        if parsed["language"] == "python":
            for node in ast.walk(parsed["tree"]):
                if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
                    _metric(report, path, node.name, _complexity(node), bool(ast.get_docstring(node)), test_text, root)
        else:
            for function in parsed.get("functions", []):
                _metric(report, path, function["name"], function["complexity"], bool(function["documented"]), test_text, root)
        for pattern, (severity, deduction, message) in SEC_PATTERNS.items():
            if re.search(pattern, text):
                report.security_score -= deduction
                report.findings.append(f"QA_SEC[{severity}] {message} in {path.relative_to(root).as_posix()}")

    count = len(report.function_metrics) or 1
    report.coverage_intent = round(sum(metric["tested"] for metric in report.function_metrics) / count, 2)
    report.doc_ratio = round(sum(metric["documented"] for metric in report.function_metrics) / count, 2)
    report.max_complexity = max((metric["complexity"] for metric in report.function_metrics), default=0)
    report.security_score = max(report.security_score, 0)
    for metric in report.function_metrics:
        if metric["complexity"] > COMPLEXITY_LIMIT:
            report.findings.append(f"QA_COMPLEXITY {metric['function']} complexity {metric['complexity']} > {COMPLEXITY_LIMIT}; policy=hard")

    score = 35 * report.coverage_intent
    score += 25 * (1 if report.max_complexity <= COMPLEXITY_LIMIT else max(0, 1 - (report.max_complexity - COMPLEXITY_LIMIT) / 10))
    score += 25 * (report.security_score / 100)
    score += 15 * report.doc_ratio
    report.grade = "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 55 else "D" if score >= 40 else "F"
    if report.max_complexity > COMPLEXITY_LIMIT and report.grade in {"A", "B"}:
        report.grade = "C"
    if any(finding.startswith(("QA_SYNTAX", "QA_PARSER_UNSUPPORTED")) for finding in report.findings):
        report.grade = "F"
    report.metrics = {
        "coverage_intent": report.coverage_intent, "max_complexity": report.max_complexity,
        "security_score": report.security_score, "doc_ratio": report.doc_ratio,
        "composite": round(score, 1), "complexity_policy": "hard",
        "scope": report.scope, "skipped_paths": report.skipped_paths,
    }
    return report
