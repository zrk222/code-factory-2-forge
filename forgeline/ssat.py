"""Semantic Software Architecture Tree: executable, language-aware contracts."""
from __future__ import annotations

import ast
import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml


@dataclass
class ArchViolation:
    code: str
    message: str
    where: str = ""


class ScaffoldError(RuntimeError):
    """A safe scaffold failure with a machine-readable report."""

    def __init__(self, message: str, report: "ScaffoldReport"):
        super().__init__(message)
        self.report = report


@dataclass
class ScaffoldFile:
    path: str
    action: str
    before_sha256: str | None = None
    after_sha256: str | None = None
    backup: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "action": self.action,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "backup": self.backup,
            "detail": self.detail,
        }


@dataclass
class ScaffoldReport:
    dry_run: bool
    created: list[ScaffoldFile] = field(default_factory=list)
    adopted: list[ScaffoldFile] = field(default_factory=list)
    skipped: list[ScaffoldFile] = field(default_factory=list)
    conflicts: list[ScaffoldFile] = field(default_factory=list)
    overwritten: list[ScaffoldFile] = field(default_factory=list)
    rollback_performed: bool = False

    @property
    def created_paths(self) -> list[Path]:
        return [Path(item.path) for item in self.created]

    def __len__(self) -> int:
        """Compatibility for callers that previously received ``list[Path]``."""
        return len(self.created)

    def __iter__(self):
        return iter(self.created_paths)

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "created": [item.to_dict() for item in self.created],
            "adopted": [item.to_dict() for item in self.adopted],
            "skipped": [item.to_dict() for item in self.skipped],
            "conflicts": [item.to_dict() for item in self.conflicts],
            "overwritten": [item.to_dict() for item in self.overwritten],
            "rollback_performed": self.rollback_performed,
        }


_LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
}


def load_ssat(path: Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _language_for(path: Path) -> str:
    language = _LANGUAGE_BY_SUFFIX.get(path.suffix.lower())
    if language is None:
        raise ValueError(f"unsupported SSAT module extension: {path.suffix or '<none>'}")
    return language


def _python_import(imp: str) -> str:
    return f"import {imp}" if "." not in imp else f"from {imp.rsplit('.', 1)[0]} import {imp.rsplit('.', 1)[1]}"


def _typescript_import(imp: str) -> str:
    if imp.startswith("import "):
        return imp
    alias = re.sub(r"[^A-Za-z0-9_]", "_", imp.rsplit("/", 1)[-1]) or "module_"
    source = imp if imp.startswith((".", "/")) else f"./{imp}"
    return f'import * as {alias} from "{source}";'


def _render_python(module: dict) -> str:
    lines = ['"""AUTO-SCAFFOLD from SSAT. Fill bodies only; do not change signatures."""']
    lines.extend(_python_import(imp) for imp in module.get("imports", []))
    lines.append("")
    for function in module.get("functions", []):
        args = ", ".join(function.get("args", []))
        returns = function.get("returns", "None")
        lines.extend([
            f"def {function['name']}({args}) -> {returns}:",
            f'    """{function.get("doc", "TODO")}"""',
            "    raise NotImplementedError  # FILL",
            "",
        ])
    return "\n".join(lines)


def _render_typescript(module: dict) -> str:
    lines = ["/** AUTO-SCAFFOLD from SSAT. Fill bodies only; do not change signatures. */"]
    lines.extend(_typescript_import(imp) for imp in module.get("imports", []))
    lines.append("")
    for function in module.get("functions", []):
        args = ", ".join(function.get("args", []))
        returns = function.get("returns", "void")
        lines.extend([
            f"/** {function.get('doc', 'TODO')} */",
            f"export function {function['name']}({args}): {returns} {{",
            '  throw new Error("NotImplementedError: FILL");',
            "}",
            "",
        ])
    return "\n".join(lines)


def _javascript_args(args: list[str]) -> str:
    """Remove TypeScript-only annotations while preserving ordinary JS syntax."""
    cleaned: list[str] = []
    for arg in args:
        value = arg.split(":", 1)[0].strip().rstrip("?")
        if not value:
            raise ValueError("JavaScript SSAT arguments must include a parameter name")
        cleaned.append(value)
    return ", ".join(cleaned)


def _render_javascript(module: dict) -> str:
    lines = ["/** AUTO-SCAFFOLD from SSAT. Fill bodies only; do not change signatures. */"]
    lines.extend(_typescript_import(imp) for imp in module.get("imports", []))
    lines.append("")
    for function in module.get("functions", []):
        lines.extend([
            f"/** {function.get('doc', 'TODO')} */",
            f"export function {function['name']}({_javascript_args(function.get('args', []))}) {{",
            '  throw new Error("NotImplementedError: FILL");',
            "}",
            "",
        ])
    return "\n".join(lines)


def _render_module(module: dict, target: Path) -> str:
    language = _language_for(target)
    if language == "python":
        return _render_python(module)
    if language == "typescript":
        return _render_typescript(module)
    return _render_javascript(module)


def _validate_generated_source(source: str, target: Path) -> None:
    language = _language_for(target)
    if language == "python":
        ast.parse(source)
        return
    if re.search(r"^\s*def\s+", source, flags=re.MULTILINE) or source.count("{") != source.count("}"):
        raise ValueError(f"generated invalid {language} for {target}")
    for line in source.splitlines():
        if "export function " not in line:
            continue
        signature = (
            r"export function \w+\(.*\):\s*[^\s]+\s*\{"
            if language == "typescript"
            else r"export function \w+\(.*\)\s*\{"
        )
        if not re.search(signature, line):
            raise ValueError(f"generated invalid {language} signature for {target}")


def scaffold_from_ssat(
    ssat: dict,
    out_dir: Path,
    *,
    force: bool = False,
    adopt_existing: bool = False,
    dry_run: bool = False,
    backup_root: Path | None = None,
) -> ScaffoldReport:
    """Plan, validate, and atomically materialize a language-aware SSAT scaffold.

    Existing files are conflicts by default. ``force=True`` creates a timestamped
    backup before an atomic replacement. ``adopt_existing=True`` validates and
    hash-records existing targets without changing them, while scaffolding only
    modules that are absent. No target is changed until every module has rendered
    and validated; any write failure restores every earlier target.
    """
    out_dir = Path(out_dir).resolve()
    report = ScaffoldReport(dry_run=dry_run)
    if force and adopt_existing:
        raise ScaffoldError("--force and --adopt-existing are mutually exclusive", report)

    plan: list[tuple[dict, Path, str, bytes | None]] = []
    seen: set[Path] = set()
    names = {module["name"]: module for module in ssat.get("modules", [])}
    allowed = {(edge["from"], edge["to"]) for edge in ssat.get("dependencies", [])}

    try:
        for module in ssat.get("modules", []):
            target = (out_dir / module["path"]).resolve()
            if out_dir not in target.parents and target != out_dir:
                raise ValueError(f"SSAT module path escapes root: {module['path']}")
            if target in seen:
                raise ValueError(f"SSAT declares duplicate module path: {module['path']}")
            seen.add(target)
            source = _render_module(module, target)
            _validate_generated_source(source, target)
            previous = target.read_bytes() if target.exists() else None
            action = "overwrite" if previous is not None and force else "adopt" if previous is not None and adopt_existing else "conflict" if previous is not None else "create"
            item = ScaffoldFile(
                path=str(target),
                action=action,
                before_sha256=_sha256_bytes(previous) if previous is not None else None,
                after_sha256=_sha256_bytes(source.encode("utf-8")),
            )
            if action == "adopt":
                violations = _module_erosion(module, target, names, allowed)
                if violations:
                    item.action = "conflict"
                    item.detail = " | ".join(f"{violation.code} {violation.message}" for violation in violations)
                    report.conflicts.append(item)
                else:
                    item.after_sha256 = item.before_sha256
                    item.detail = "existing target verified against SSAT; bytes retained"
                    report.adopted.append(item)
            elif action == "conflict":
                report.conflicts.append(item)
            elif action == "overwrite":
                report.overwritten.append(item)
            else:
                report.created.append(item)
            if action in {"create", "overwrite"}:
                plan.append((module, target, source, previous))
    except (KeyError, TypeError, ValueError) as exc:
        raise ScaffoldError(str(exc), report) from exc

    if report.conflicts:
        if adopt_existing:
            raise ScaffoldError("existing SSAT targets do not satisfy the declared contract", report)
        raise ScaffoldError("existing SSAT targets require --force or --adopt-existing", report)
    if dry_run:
        return report

    temp_paths: list[tuple[Path, Path]] = []
    backups: dict[Path, Path] = {}
    created_targets = [target for _, target, _, previous in plan if previous is None]
    try:
        # Validate every temp file before changing a single target.
        for _, target, source, _ in plan:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".forge-tmp", dir=target.parent)
            temp = Path(temp_name)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(source)
                handle.flush()
                os.fsync(handle.fileno())
            temp_paths.append((temp, target))

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_root = Path(backup_root) if backup_root else out_dir / ".forge" / "scaffold-backups" / timestamp
        for _, target, _, previous in plan:
            if previous is not None:
                backup = backup_root / target.relative_to(out_dir)
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
                backups[target] = backup
                next(item for item in report.overwritten if item.path == str(target)).backup = str(backup)

        for temp, target in temp_paths:
            os.replace(temp, target)
        if adopt_existing:
            violations = check_erosion(ssat, out_dir)
            if violations:
                detail = " | ".join(f"{violation.code} {violation.message}" for violation in violations)
                report.adopted.clear()
                raise ValueError(f"adopted targets do not satisfy SSAT: {detail}")
        return report
    except Exception as exc:
        for target, backup in backups.items():
            if backup.exists():
                shutil.copy2(backup, target)
        for target in created_targets:
            if target.exists() and target not in backups:
                target.unlink()
        report.rollback_performed = bool(backups or created_targets)
        if adopt_existing and isinstance(exc, ValueError) and str(exc).startswith("adopted targets do not satisfy SSAT:"):
            raise ScaffoldError(f"SSAT adoption validation failed: {exc}", report) from exc
        raise ScaffoldError(f"scaffold transaction rolled back: {exc}", report) from exc
    finally:
        for temp, _ in temp_paths:
            if temp.exists():
                temp.unlink()


def _module_of(path: str, ssat: dict) -> str | None:
    for module in ssat.get("modules", []):
        if path.endswith(module["path"]):
            return module["name"]
    return None


def _javascript_erosion(module: dict, path: Path, names: dict, allowed: set[tuple[str, str]]) -> list[ArchViolation]:
    """A JavaScript/TypeScript structure check. Python's AST is never used here."""
    source = path.read_text(encoding="utf-8")
    violations: list[ArchViolation] = []
    if re.search(r"^\s*def\s+", source, flags=re.MULTILINE) or source.count("{") != source.count("}"):
        return [ArchViolation("E_TS_SYNTAX", "invalid TypeScript structure", module["path"])]
    for function in module.get("functions", []):
        pattern = re.compile(rf"(?:export\s+)?(?:async\s+)?function\s+{re.escape(function['name'])}\s*\(([^)]*)\)", re.MULTILINE)
        match = pattern.search(source)
        if match is None:
            violations.append(ArchViolation("E_SIG_MISSING", f"{function['name']} declared but not implemented", module["path"]))
            continue
        got_args = [part.split(":", 1)[0].strip().rstrip("?") for part in match.group(1).split(",") if part.strip()]
        want_args = [arg.split(":", 1)[0].strip().rstrip("?") for arg in function.get("args", [])]
        if got_args != want_args:
            violations.append(ArchViolation("E_SIG_DRIFT", f"{function['name']}({got_args}) != SSAT ({want_args})", module["path"]))
    this = module["name"]
    for imported in re.findall(r"(?:from\s+|import\s+)['\"]([^'\"]+)['\"]", source):
        target = names.get(Path(imported).stem) or names.get(imported)
        target_name = target["name"] if target else None
        if target_name and target_name != this and (this, target_name) not in allowed:
            violations.append(ArchViolation("E_ILLEGAL_DEP", f"{this} -> {target_name} not in SSAT dependency allowlist", module["path"]))
    return violations


def _symbol_region(source: str, path: Path, symbol: str, max_lines: int) -> str | None:
    """Return one bounded declared function body for an invariant regex.

    Regex policy checks are deliberately never allowed to search a whole module:
    a reviewed module, symbol, and maximum span form the enforcement boundary.
    """
    lines = source.splitlines(keepends=True)
    if path.suffix.lower() == ".py":
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol:
                start, end = node.lineno, node.end_lineno or node.lineno
                if end - start + 1 > max_lines:
                    return None
                return "".join(lines[start - 1:end])
        return None
    match = re.search(
        rf"(?:export\s+)?(?:async\s+)?function\s+{re.escape(symbol)}\s*\([^)]*\)[^{{]*{{",
        source,
        flags=re.MULTILINE,
    )
    if match is None:
        return None
    start = match.start()
    brace = source.find("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                region = source[start:index + 1]
                return region if region.count("\n") + 1 <= max_lines else None
    return None


def _invariant_violations(ssat: dict, src_dir: Path, names: dict[str, dict]) -> list[ArchViolation]:
    violations: list[ArchViolation] = []
    for invariant in ssat.get("invariants", []):
        name = invariant.get("name", "unnamed_invariant")
        pattern = invariant.get("forbid_pattern")
        raw_scopes = invariant.get("scopes") or ([invariant["scope"]] if isinstance(invariant.get("scope"), dict) else [])
        if not isinstance(pattern, str) or not raw_scopes:
            violations.append(ArchViolation("E_INVARIANT_SCOPE", f"{name}: invariants require forbid_pattern and at least one bounded scope"))
            continue
        try:
            compiled = re.compile(pattern)
        except re.error as error:
            violations.append(ArchViolation("E_INVARIANT_SCOPE", f"{name}: invalid forbid_pattern: {error}"))
            continue
        for scope in raw_scopes:
            if not isinstance(scope, dict):
                violations.append(ArchViolation("E_INVARIANT_SCOPE", f"{name}: scope must be an object"))
                continue
            module_name = scope.get("module")
            module = names.get(module_name) if isinstance(module_name, str) else None
            symbol = scope.get("symbol")
            max_lines = scope.get("max_lines")
            if module is None or not isinstance(symbol, str) or not isinstance(max_lines, int) or not 1 <= max_lines <= 1000:
                violations.append(ArchViolation(
                    "E_INVARIANT_SCOPE",
                    f"{name}: scope requires declared module, symbol, and max_lines between 1 and 1000",
                    str(module_name or ""),
                ))
                continue
            path = src_dir / module["path"]
            try:
                source = path.read_text(encoding="utf-8")
            except OSError as error:
                violations.append(ArchViolation("E_INVARIANT_SCOPE", f"{name}: cannot read scoped module: {type(error).__name__}", module["path"]))
                continue
            region = _symbol_region(source, path, symbol, max_lines)
            if region is None:
                violations.append(ArchViolation(
                    "E_INVARIANT_SCOPE",
                    f"{name}: cannot resolve bounded symbol {module_name}.{symbol}",
                    module["path"],
                ))
                continue
            if compiled.search(region):
                violations.append(ArchViolation(
                    "E_INVARIANT",
                    f"{name}: forbidden pattern {pattern!r} in scoped symbol {module_name}.{symbol}",
                    module["path"],
                ))
    return violations


def _module_erosion(module: dict, path: Path, names: dict[str, dict], allowed: set[tuple[str, str]]) -> list[ArchViolation]:
    """Check one declared module without widening the scan beyond its SSAT scope."""
    if not path.exists():
        return [ArchViolation("E_MISSING_MODULE", f"{module['path']} declared in SSAT but absent")]
    try:
        language = _language_for(path)
    except ValueError as exc:
        return [ArchViolation("E_UNSUPPORTED_LANGUAGE", str(exc), module["path"])]
    if language in {"typescript", "javascript"}:
        return _javascript_erosion(module, path, names, allowed)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        return [ArchViolation("E_SYNTAX", str(exc), module["path"])]

    violations: list[ArchViolation] = []
    declared = {function["name"]: function for function in module.get("functions", [])}
    found = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    for name, spec in declared.items():
        if name not in found:
            violations.append(ArchViolation("E_SIG_MISSING", f"{name} declared but not implemented", module["path"]))
            continue
        got_args = [arg.arg for arg in found[name].args.args]
        want_args = [arg.split(":", 1)[0].strip() for arg in spec.get("args", [])]
        if got_args != want_args:
            violations.append(ArchViolation("E_SIG_DRIFT", f"{name}({got_args}) != SSAT ({want_args})", module["path"]))
    this = module["name"]
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for imported in imports:
                target = names.get(imported.split(".")[-1]) or names.get(imported)
                target_name = target["name"] if target else None
                if target_name and target_name != this and (this, target_name) not in allowed:
                    violations.append(ArchViolation("E_ILLEGAL_DEP", f"{this} -> {target_name} not in SSAT dependency allowlist", module["path"]))
    return violations


def check_erosion(ssat: dict, src_dir: Path) -> list[ArchViolation]:
    """Detect signature drift and illegal dependency edges per source language."""
    src_dir = Path(src_dir)
    allowed = {(edge["from"], edge["to"]) for edge in ssat.get("dependencies", [])}
    names = {module["name"]: module for module in ssat.get("modules", [])}
    violations = [
        violation
        for module in ssat.get("modules", [])
        for violation in _module_erosion(module, src_dir / module["path"], names, allowed)
    ]
    violations.extend(_invariant_violations(ssat, src_dir, names))
    return violations
