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
    coverage_intent: float | None = 0.0
    max_complexity: int = 0
    security_score: int = 100
    doc_ratio: float | None = 0.0
    grade: str = "F"
    findings: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    function_metrics: list[dict] = field(default_factory=list)
    advisory_function_metrics: list[dict] = field(default_factory=list)
    function_inventory: list[dict] = field(default_factory=list)
    skipped_paths: list[str] = field(default_factory=list)
    scope: dict = field(default_factory=dict)
    function_inventory_complete: bool = True
    unknown_security_calls: list[str] = field(default_factory=list)

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
            if finding.startswith(("QA_PARSER_UNSUPPORTED", "QA_METRICS_INCOMPLETE")):
                units.append(UnitResult(f"qa_audit:{finding.split()[1]}", "qa_audit", False, finding, FailureClass.PARSER_UNSUPPORTED))
            elif finding.startswith("QA_SYNTAX"):
                units.append(UnitResult(f"qa_audit:{finding.split()[1]}", "qa_audit", False, finding, FailureClass.SYNTAX_ERROR))
        return Attribution("qa_audit", len(units), sum(unit.passed for unit in units), units)


def _complexity(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
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


@dataclass
class _PythonSecurityScope:
    kind: str
    parent: _PythonSecurityScope | None = None
    imports: dict[str, str] = field(default_factory=dict)
    shadowed: set[str] = field(default_factory=set)
    calls: list[ast.Call] = field(default_factory=list)
    assignments: list[ast.AST] = field(default_factory=list)
    children: list[_PythonSecurityScope] = field(default_factory=list)

    def resolve(self, name: str) -> str | None:
        first, *tail = name.split(".")
        scope: _PythonSecurityScope | None = self
        while scope is not None:
            if first in scope.shadowed:
                return None
            if first in scope.imports:
                return ".".join((scope.imports[first], *tail))
            scope = scope.parent
        if name in {"eval", "exec"}:
            return name
        return None


class _PythonSecurityBindings(ast.NodeVisitor):
    """Collect calls/import bindings per Python lexical scope, without taint inference."""

    def __init__(self) -> None:
        self.root = _PythonSecurityScope("module")
        self.scope = self.root

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.scope.shadowed.add(node.id)

    def visit_Import(self, node: ast.Import) -> None:
        for item in node.names:
            bound = item.asname or item.name.split(".")[0]
            target = item.name if item.asname else item.name.split(".")[0]
            self.scope.imports[bound] = target

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for item in node.names:
            if item.name != "*":
                bound = item.asname or item.name
                if node.level:
                    self.scope.shadowed.add(bound)
                elif node.module:
                    self.scope.imports[bound] = f"{node.module}.{item.name}"

    def visit_Call(self, node: ast.Call) -> None:
        self.scope.calls.append(node)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.scope.assignments.append(node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.scope.assignments.append(node)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.scope.assignments.append(node)
        self.generic_visit(node)

    def _visit_outer_expressions(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        annotations = [argument.annotation for argument in (*node.args.posonlyargs, *node.args.args,
                                                             *node.args.kwonlyargs) if argument.annotation]
        if node.args.vararg and node.args.vararg.annotation:
            annotations.append(node.args.vararg.annotation)
        if node.args.kwarg and node.args.kwarg.annotation:
            annotations.append(node.args.kwarg.annotation)
        returned = getattr(node, "returns", None)
        expressions = (*getattr(node, "decorator_list", []), *node.args.defaults, *node.args.kw_defaults,
                       *annotations, *((returned,) if returned else ()))
        for expression in expressions:
            if expression is not None:
                self.visit(expression)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        self.scope.shadowed.add(node.name) if hasattr(node, "name") else None
        self._visit_outer_expressions(node)
        parent = self.scope
        if parent.kind == "class":
            parent = parent.parent or parent
        child = _PythonSecurityScope("function", parent)
        child.shadowed.update(arg.arg for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs))
        if node.args.vararg:
            child.shadowed.add(node.args.vararg.arg)
        if node.args.kwarg:
            child.shadowed.add(node.args.kwarg.arg)
        self.scope.children.append(child)
        previous = self.scope
        self.scope = child
        if isinstance(node, ast.Lambda):
            self.visit(node.body)
        else:
            for statement in node.body:
                self.visit(statement)
        self.scope = previous

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.shadowed.add(node.name)
        for expression in (*node.decorator_list, *node.bases, *(keyword.value for keyword in node.keywords)):
            self.visit(expression)
        child = _PythonSecurityScope("class", self.scope)
        self.scope.children.append(child)
        previous = self.scope
        self.scope = child
        for statement in node.body:
            self.visit(statement)
        self.scope = previous

    def _visit_comprehension(self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp) -> None:
        parent = self.scope
        generators = node.generators
        if generators:
            self.visit(generators[0].iter)
        child = _PythonSecurityScope("comprehension", parent)
        parent.children.append(child)
        self.scope = child
        for index, generator in enumerate(generators):
            if index:
                self.visit(generator.iter)
            self.visit(generator.target)
            for condition in generator.ifs:
                self.visit(condition)
        for expression in (getattr(node, "key", None), getattr(node, "value", None), getattr(node, "elt", None)):
            if expression is not None:
                self.visit(expression)
        self.scope = parent

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node)


def _python_security_text(tree: ast.AST) -> tuple[str, list[str]]:
    """Scan resolved executable calls and credential assignments, not quoted fixtures.

    This is a bounded pattern inventory. It leaves unresolved calls explicit and
    makes no claim of general taint or security analysis.
    """
    bindings = _PythonSecurityBindings()
    bindings.visit(tree)
    calls: list[str] = []
    unknown: list[str] = []

    def keyword_text(call: ast.Call) -> str:
        return ", ".join(f"{item.arg}={item.value.value!r}" for item in call.keywords
                          if item.arg and isinstance(item.value, ast.Constant))

    def collect(scope: _PythonSecurityScope) -> None:
        for call in scope.calls:
            original = ast.unparse(call.func)
            resolved = scope.resolve(original)
            keywords = keyword_text(call)
            if resolved is None:
                unknown.append(original)
                if keywords:
                    calls.append(f"<unknown-call>({keywords})")
                continue
            calls.append(f"{resolved}({keywords})")
        for child in scope.children:
            collect(child)

    collect(bindings.root)
    assignments = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            assignments.extend(f"{ast.unparse(target)} = {node.value.value!r}" for target in targets)
    return "\n".join(calls + assignments), sorted(set(unknown))


def _is_test(path: Path) -> bool:
    return path.name.startswith("test_") or path.parent.name in {"tests", "test", "__tests__"} or ".test." in path.name or ".spec." in path.name


def _metric(report: QAReport, path: Path, name: str, complexity: int, documented: bool, test_text: str, root: Path) -> None:
    tested = bool(re.search(rf"\b{re.escape(name)}\b", test_text)) and not name.startswith("<anonymous")
    report.function_metrics.append({
        "function": f"{path.relative_to(root).as_posix()}:{name}", "complexity": complexity,
        "tested": tested, "documented": documented,
    })


def _source_paths(root: Path, source_paths: Iterable[Path] | None, report: QAReport) -> tuple[list[Path], list[Path]]:
    if source_paths is None:
        inventory = iter_source_files(root, suffixes=SOURCE_SUFFIXES, skipped=report.skipped_paths)
        tests = [path for path in inventory if _is_test(path)]
    else:
        inventory = iter_source_files(root, paths=source_paths, skipped=report.skipped_paths)
        tests = [path for path in iter_source_files(root, suffixes=SOURCE_SUFFIXES, skipped=report.skipped_paths)
                 if _is_test(path)]
    code = [path for path in inventory if not _is_test(path)]
    return code, tests


def _record_parser_problem(report: QAReport, path: Path, parsed: dict, parser: str, root: Path) -> None:
    relative = path.relative_to(root).as_posix()
    status = parsed["status"]
    report.function_inventory_complete = False
    report.function_inventory.append({"path": relative, "parser": parser, "status": status, "symbols": 0})
    if parsed.get("read_error"):
        read_error = str(parsed["read_error"])
        report.skipped_paths.append(read_error)
        report.findings.append(f"QA_SOURCE_READ_ERROR {relative}: {read_error}")
    elif status == "parser_unsupported":
        report.findings.append(f"QA_PARSER_UNSUPPORTED {relative}: {parsed.get('reason', parsed['language'])}")
    else:
        report.findings.append(f"QA_SYNTAX {relative}: {parsed['error']}")


def _python_public_nodes(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    public: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            public.append(node)
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            public.extend(member for member in node.body
                          if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and not member.name.startswith("_"))
    return public


def _record_functions(report: QAReport, path: Path, parsed: dict, test_text: str, root: Path) -> None:
    relative = path.relative_to(root).as_posix()
    if parsed["language"] == "python":
        functions = _python_public_nodes(parsed["tree"])
        report.function_inventory.append({"path": relative, "parser": "python-ast", "status": "exact",
                                          "symbols": len(functions)})
        for node in functions:
            _metric(report, path, node.name, _complexity(node), bool(ast.get_docstring(node)), test_text, root)
        return
    parser = parsed.get("parser", "unavailable")
    functions = parsed.get("functions", [])
    metric_status = parsed.get("function_metrics_status", "advisory")
    report.function_inventory.append({"path": relative, "parser": parser, "status": metric_status,
                                      "symbols": len(functions)})
    if metric_status != "exact":
        report.function_inventory_complete = False
        report.findings.append(f"QA_METRICS_INCOMPLETE {relative}: parser={parser}; function inventory is advisory")
    for function in functions:
        if not function.get("public", False):
            continue
        if metric_status == "exact":
            _metric(report, path, function["name"], function["complexity"], bool(function["documented"]), test_text, root)
        else:
            report.advisory_function_metrics.append({
                "function": f"{relative}:{function['name']}",
                "complexity_hint": function["complexity"], "parser": parser,
            })


def _record_security(report: QAReport, path: Path, parsed: dict, text: str, root: Path) -> None:
    relative = path.relative_to(root).as_posix()
    if parsed["language"] == "python":
        security_text, unknown_calls = _python_security_text(parsed["tree"])
        report.unknown_security_calls.extend(f"{relative}:{name}" for name in unknown_calls)
    else:
        security_text = text
    for pattern, (severity, deduction, message) in SEC_PATTERNS.items():
        if re.search(pattern, security_text):
            report.security_score -= deduction
            report.findings.append(f"QA_SEC[{severity}] {message} in {relative}")


def _record_source_file(report: QAReport, path: Path, root: Path, test_text: str) -> None:
    parsed = analyze_source(path, root)
    parser = parsed.get("parser", "python-ast" if parsed.get("language") == "python" else "unavailable")
    if parsed["status"] != "ok":
        _record_parser_problem(report, path, parsed, parser, root)
        return
    _record_functions(report, path, parsed, test_text, root)
    _record_security(report, path, parsed, parsed["text"], root)


def _finalize_report(report: QAReport) -> None:
    count = len(report.function_metrics)
    report.coverage_intent = round(sum(metric["tested"] for metric in report.function_metrics) / count, 2) if count else None
    report.doc_ratio = round(sum(metric["documented"] for metric in report.function_metrics) / count, 2) if count else None
    report.max_complexity = max((metric["complexity"] for metric in report.function_metrics), default=0)
    report.security_score = max(report.security_score, 0)
    for metric in report.function_metrics:
        if metric["complexity"] > COMPLEXITY_LIMIT:
            report.findings.append(f"QA_COMPLEXITY {metric['function']} complexity {metric['complexity']} > {COMPLEXITY_LIMIT}; policy=hard")

    # A source slice with no declared functions has no coverage *claim*. It is
    # syntax/security inventory only, rather than a synthetic zero-coverage D.
    score = 35 * report.coverage_intent if report.coverage_intent is not None else 35
    score += 25 * (1 if report.max_complexity <= COMPLEXITY_LIMIT else max(0, 1 - (report.max_complexity - COMPLEXITY_LIMIT) / 10))
    score += 25 * (report.security_score / 100)
    score += 15 * report.doc_ratio if report.doc_ratio is not None else 15
    report.grade = "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 55 else "D" if score >= 40 else "F"
    # No executable symbols means no behavioral proof, only a syntax/security
    # inventory. Do not surface that inventory as an aggregate green result.
    if not count and report.grade == "A":
        report.grade = "B"
    if report.max_complexity > COMPLEXITY_LIMIT and report.grade in {"A", "B"}:
        report.grade = "C"
    if any(finding.startswith(("QA_SYNTAX", "QA_PARSER_UNSUPPORTED", "QA_METRICS_INCOMPLETE")) for finding in report.findings):
        report.grade = "F"
    if report.skipped_paths:
        report.findings.append(f"QA_SOURCE_INVENTORY_INCOMPLETE skipped={len(report.skipped_paths)}")
        report.grade = "F"
    report.metrics = {
        "coverage_intent": report.coverage_intent, "max_complexity": report.max_complexity,
        "security_score": report.security_score, "doc_ratio": report.doc_ratio,
        "composite": round(score, 1), "complexity_policy": "hard",
        "coverage_assessment": "static_name_match" if count else "not_applicable_no_declared_functions",
        "behavioral_proof_status": "not_measured_static_only" if count else "unavailable_no_declared_functions",
        "scope": report.scope, "skipped_paths": report.skipped_paths,
        "inventory_complete": not report.skipped_paths,
        "function_inventory_complete": report.function_inventory_complete,
        "function_inventory": report.function_inventory,
        "advisory_function_metrics": report.advisory_function_metrics,
        "unknown_security_calls": sorted(set(report.unknown_security_calls)),
        "security_analysis": "bounded_pattern_inventory",
    }


def qa_audit(src_dir: Path, *, source_paths: Iterable[Path] | None = None) -> QAReport:
    """Grade a reviewed feature slice, or an explicitly requested pruned repository scan."""
    root = Path(src_dir).resolve()
    report = QAReport(scope={"kind": "feature" if source_paths is not None else "repo_wide"})
    code_files, test_files = _source_paths(root, source_paths, report)
    report.scope["code_files"] = [path.relative_to(root).as_posix() for path in code_files]
    test_text = "\n".join(text for path in test_files if (text := read_text(path, report.skipped_paths)) is not None)
    for path in code_files:
        _record_source_file(report, path, root, test_text)
    _finalize_report(report)
    return report
