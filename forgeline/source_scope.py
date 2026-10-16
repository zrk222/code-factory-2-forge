"""Safe, language-aware source inventory shared by ForgeLine gates."""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from functools import lru_cache
from typing import Iterable


IGNORED_DIRECTORIES = frozenset({
    ".git", ".pnpm", "node_modules", ".next", ".nuxt", ".forge", ".factory",
    "ignored.factory", ".venv",
    "venv", "dist", "build", "coverage", ".cache", "cache", "out", "output",
    "target", "vendor", "__pycache__",
})
SOURCE_SUFFIXES = frozenset({".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"})


def language_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix in {".js", ".jsx", ".mjs", ".cjs"}:
        return "javascript"
    if suffix in {".ts", ".tsx"}:
        return "typescript"
    try:
        first = path.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
    except (FileNotFoundError, NotADirectoryError, OSError, IndexError):
        return "unsupported"
    return "javascript" if first.startswith("#!") and "node" in first else "unsupported"


def read_text(path: Path, skipped: list[str]) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except (FileNotFoundError, NotADirectoryError, OSError) as error:
        skipped.append(f"{path}: {type(error).__name__}")
        return None


def iter_source_files(
    root: Path,
    *,
    suffixes: Iterable[str] | None = None,
    paths: Iterable[Path] | None = None,
    skipped: list[str] | None = None,
) -> list[Path]:
    """Return a stable, pruned source inventory without following dependency trees."""
    root = Path(root).resolve()
    wanted = {suffix.lower() for suffix in (suffixes or SOURCE_SUFFIXES)}
    skipped = skipped if skipped is not None else []
    if paths is not None:
        result = []
        for path in paths:
            candidate = (root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
            _append_source_candidate(candidate, root, wanted, result, skipped)
        return sorted(set(result))

    result: list[Path] = []

    def on_error(error: OSError) -> None:
        skipped.append(f"{getattr(error, 'filename', root)}: {type(error).__name__}")

    for current, dirs, names in os.walk(root, topdown=True, onerror=on_error, followlinks=False):
        try:
            dirs[:] = sorted(name for name in dirs if name not in IGNORED_DIRECTORIES)
        except (FileNotFoundError, NotADirectoryError, OSError) as error:
            skipped.append(f"{current}: {type(error).__name__}")
            continue
        for name in sorted(names):
            _append_source_candidate(Path(current) / name, root, wanted, result, skipped)
    return result


def _append_source_candidate(path: Path, root: Path, wanted: set[str], result: list[Path], skipped: list[str]) -> None:
    try:
        path.relative_to(root)
        if path.is_file() and path.suffix.lower() in wanted:
            result.append(path)
    except (FileNotFoundError, NotADirectoryError, OSError, ValueError) as error:
        skipped.append(f"{path}: {type(error).__name__}")


def declared_paths(root: Path, ssat: dict) -> list[Path]:
    """Resolve only reviewed SSAT module paths, refusing scope escape."""
    root = Path(root).resolve()
    paths: list[Path] = []
    for module in ssat.get("modules", []):
        candidate = (root / module["path"]).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        paths.append(candidate)
    return paths


_NODE_TYPESCRIPT_AST = r"""
const fs = require('fs');
const ts = require(process.env.FORGELINE_TYPESCRIPT_PATH || 'typescript');
const argOffset = process.argv[1] && process.argv[1].endsWith('.cjs') ? 2 : 1;
const file = process.argv[argOffset];
const mode = process.argv[argOffset + 1];
const source = fs.readFileSync(file, 'utf8');
const kind = mode === 'typescript'
  ? (file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS)
  : (file.endsWith('.jsx') ? ts.ScriptKind.JSX : ts.ScriptKind.JS);
const tree = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, kind);
const errors = tree.parseDiagnostics.map(d => ts.flattenDiagnosticMessageText(d.messageText, '\\n'));
const branches = new Set([
  ts.SyntaxKind.IfStatement, ts.SyntaxKind.ForStatement, ts.SyntaxKind.ForInStatement,
  ts.SyntaxKind.ForOfStatement, ts.SyntaxKind.WhileStatement, ts.SyntaxKind.DoStatement,
  ts.SyntaxKind.CaseClause, ts.SyntaxKind.CatchClause, ts.SyntaxKind.ConditionalExpression,
]);
function nameOf(node) {
  if (node.name && node.name.text) return node.name.text;
  if (node.parent && ts.isVariableDeclaration(node.parent) && node.parent.name) return node.parent.name.getText(tree);
  return '<anonymous@' + tree.getLineAndCharacterOfPosition(node.pos).line + '>';
}
function hasModifier(node, kind) {
  return Boolean(node.modifiers && node.modifiers.some(modifier => modifier.kind === kind));
}
function exportedVariable(node) {
  let current = node.parent;
  while (current && !ts.isVariableStatement(current) && current !== tree) current = current.parent;
  return Boolean(current && ts.isVariableStatement(current) && hasModifier(current, ts.SyntaxKind.ExportKeyword));
}
const exportedLocals = new Set();
for (const statement of tree.statements) {
  if (ts.isExportDeclaration(statement) && !statement.moduleSpecifier && statement.exportClause && ts.isNamedExports(statement.exportClause)) {
    for (const element of statement.exportClause.elements) exportedLocals.add((element.propertyName || element.name).text);
  }
  if (ts.isExportAssignment(statement) && !statement.isExportEquals && ts.isIdentifier(statement.expression)) {
    exportedLocals.add(statement.expression.text);
  }
}
function publicSurface(node) {
  if (ts.isFunctionDeclaration(node)) return hasModifier(node, ts.SyntaxKind.ExportKeyword) || (node.parent === tree && exportedLocals.has(nameOf(node)));
  if (ts.isArrowFunction(node) || ts.isFunctionExpression(node)) {
    const declaration = node.parent;
    const variableList = declaration && ts.isVariableDeclaration(declaration) ? declaration.parent : null;
    const statement = variableList && ts.isVariableDeclarationList(variableList) ? variableList.parent : null;
    const exportedBinding = statement && ts.isVariableStatement(statement) && statement.parent === tree
      && exportedLocals.has(declaration.name.getText(tree));
    return exportedVariable(node) || Boolean(exportedBinding);
  }
  if (ts.isMethodDeclaration(node)) {
    const owner = node.parent;
    const ownerExported = Boolean(owner && ts.isClassDeclaration(owner) && (hasModifier(owner, ts.SyntaxKind.ExportKeyword) || (owner.parent === tree && exportedLocals.has(nameOf(owner)))));
    const hidden = hasModifier(node, ts.SyntaxKind.PrivateKeyword) || hasModifier(node, ts.SyntaxKind.ProtectedKeyword);
    return ownerExported && !hidden;
  }
  return false;
}
function complexity(node) {
  let total = 1;
  function walk(n, nested) {
    if (nested && (ts.isFunctionDeclaration(n) || ts.isMethodDeclaration(n) || ts.isArrowFunction(n) || ts.isFunctionExpression(n))) return;
    if (branches.has(n.kind)) total += 1;
    if (ts.isBinaryExpression(n) && (n.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken || n.operatorToken.kind === ts.SyntaxKind.BarBarToken)) total += 1;
    ts.forEachChild(n, child => walk(child, true));
  }
  ts.forEachChild(node, child => walk(child, true));
  return total;
}
const functions = [];
function visit(node) {
  if (ts.isFunctionDeclaration(node) || ts.isMethodDeclaration(node) || ts.isArrowFunction(node) || ts.isFunctionExpression(node)) {
    const comments = ts.getLeadingCommentRanges(source, node.getFullStart()) || [];
    functions.push({name: nameOf(node), complexity: complexity(node), documented: comments.some(c => source.slice(c.pos, c.end).startsWith('/**')), public: publicSurface(node)});
  }
  ts.forEachChild(node, visit);
}
visit(tree);
console.log(JSON.stringify({errors, functions}));
"""


def _typescript_module_path(node: str, root: Path) -> str | None:
    """Resolve a project's TypeScript compiler from the project, not ForgeLine."""
    resolver = "try { process.stdout.write(require.resolve('typescript')); } catch (_) { process.exit(1); }"
    for candidate in (Path(root).resolve(), *Path(root).resolve().parents):
        try:
            completed = subprocess.run(
                [node, "-e", resolver], cwd=candidate, capture_output=True,
                text=True, timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout.strip()
    return None


def _typescript_compiler_parse(node: str, path: Path, root: Path, *, language: str = "typescript") -> dict | None:
    """Use the nearest project compiler through a Windows-safe script file."""
    compiler = _typescript_module_path(node, root)
    if compiler is None:
        return None
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".cjs", delete=False)
    try:
        with handle:
            handle.write(_NODE_TYPESCRIPT_AST)
        completed = subprocess.run(
            [node, handle.name, str(path), language], cwd=root,
            env={**os.environ, "FORGELINE_TYPESCRIPT_PATH": compiler},
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        Path(handle.name).unlink(missing_ok=True)
    if completed.returncode != 0:
        return None
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None


@lru_cache(maxsize=8)
def _node_parser_responds(node: str) -> bool:
    """Require a real JavaScript rejection before trusting check-only success."""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".mjs", delete=False) as handle:
        handle.write("export function broken( {\n")
        target = Path(handle.name)
    try:
        result = subprocess.run([node, "--check", str(target)], capture_output=True,
                                text=True, timeout=10, check=False)
        return result.returncode != 0 and "SyntaxError" in result.stderr
    finally:
        target.unlink(missing_ok=True)


def _node_syntax_check(node: str, path: Path, root: Path, *, typescript: bool = False) -> subprocess.CompletedProcess[str]:
    if not _node_parser_responds(node):
        raise OSError("Node parser did not reject its malformed canary; select a working native runtime")
    if not typescript:
        return subprocess.run([node, "--check", str(path)], cwd=root, capture_output=True,
                              text=True, timeout=10, check=False)
    # Node --check plus type stripping can accept an unfinished TS function.
    # Erase types explicitly, then validate the resulting JS with the real parser.
    with tempfile.TemporaryDirectory(prefix="forge-ts-parse-") as temporary:
        directory = Path(temporary)
        script = directory / "strip.cjs"
        output = directory / "source.mjs"
        script.write_text("const fs = require('fs'); const {stripTypeScriptTypes} = require('node:module');\n"
                          "fs.writeFileSync(process.argv[3], stripTypeScriptTypes(fs.readFileSync(process.argv[2], 'utf8')));\n", encoding="utf-8")
        stripped = subprocess.run([node, str(script), str(path), str(output)], cwd=root,
                                  capture_output=True, text=True, timeout=10, check=False)
        if stripped.returncode != 0:
            return stripped
        if not output.is_file():
            raise OSError("TypeScript erasure produced no artifact")
        return subprocess.run([node, "--check", str(output)], cwd=root, capture_output=True,
                              text=True, timeout=10, check=False)


def _nearest_existing_directory(path: Path) -> Path:
    current = path.resolve()
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _functions_from_patterns(source: str, patterns: tuple[re.Pattern[str], ...]) -> list[dict]:
    functions: list[dict] = []
    seen: set[str] = set()
    for pattern in patterns:
        for match in pattern.finditer(source):
            name = match.group("name")
            if name in seen:
                continue
            seen.add(name)
            start = match.start()
            brace = source.find("{", match.end() - 1)
            end = len(source)
            if brace >= 0:
                depth = 0
                for index in range(brace, len(source)):
                    if source[index] == "{":
                        depth += 1
                    elif source[index] == "}":
                        depth -= 1
                        if depth == 0:
                            end = index + 1
                            break
            body = source[start:end]
            complexity = 1 + len(re.findall(r"\b(?:if|for|while|case|catch)\b|&&|\|\||\?", body))
            prefix = source[max(0, start - 400):start]
            functions.append({
                "name": name,
                "complexity": complexity,
                "documented": bool(re.search(r"/\*\*[^*]*(?:\*(?!/)[^*]*)*\*/\s*$", prefix, re.DOTALL)),
                "public": bool(match.groupdict().get("exported")),
            })
    return functions


def _typescript_functions(source: str) -> list[dict]:
    """Inventory conservative TS symbols after Node has validated syntax.

    This is intentionally not a parser. The compiler API remains the preferred
    source of rich symbols; the scanner keeps clean Node 22 environments from
    collapsing to a false zero-symbol result when ``typescript`` is absent.
    """
    patterns = (
        re.compile(r"(?:^|\n)\s*(?P<exported>export\s+)?(?:default\s+)?(?:async\s+)?function\s+(?P<name>[A-Za-z_$][\w$]*)\s*\([^)]*\)[^{]*\{", re.MULTILINE),
        re.compile(r"(?:^|\n)\s*(?P<exported>export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)(?:\s*:\s*[^=;\n]+)?\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)(?:\s*:\s*[^=;\n]+)?\s*=>", re.MULTILINE),
    )
    return _functions_from_patterns(source, patterns)


def _validate_temporary_script(node: str, temporary: Path, target: Path, root: Path, language: str) -> str:
    if language == "javascript":
        checked = _node_syntax_check(node, temporary, root)
        if checked.returncode != 0:
            raise ValueError(checked.stderr.strip() or f"generated invalid JavaScript for {target}")
        return "node-check"
    compiler = _typescript_compiler_parse(node, temporary, root)
    if compiler is not None:
        if compiler["errors"]:
            raise ValueError("; ".join(compiler["errors"]))
        return "typescript-compiler"
    if target.suffix.lower() == ".tsx":
        raise ValueError(f"TypeScript compiler is required to validate generated TSX for {target}")
    checked = _node_syntax_check(node, temporary, root, typescript=True)
    if checked.returncode == 0:
        return "node-strip-types"
    message = checked.stderr.strip()
    if "bad option" in message or "unknown option" in message:
        raise ValueError(f"Node 22+ or the TypeScript compiler is required to validate generated TypeScript for {target}")
    raise ValueError(message or f"generated invalid TypeScript for {target}")


def validate_generated_script(source: str, target: Path) -> str:
    """Validate generated JS/TS before the scaffold transaction changes files."""
    target = Path(target)
    language = language_for(target)
    if language not in {"javascript", "typescript"}:
        raise ValueError(f"no script validator for {target}")
    if re.search(r"^\s*def\s+", source, flags=re.MULTILINE):
        raise ValueError(f"generated invalid {language} structure for {target}")
    node = shutil.which("node")
    if node is None:
        raise ValueError(f"Node.js is required to validate generated {language} for {target}")
    root = _nearest_existing_directory(target.parent)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=target.suffix, delete=False) as handle:
        handle.write(source)
        temporary = Path(handle.name)
    try:
        return _validate_temporary_script(node, temporary, target, root, language)
    except OSError as exc:
        raise ValueError(f"script parser unavailable: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _analyze_javascript(path: Path, root: Path, text: str, node: str) -> dict:
    compiler = _typescript_compiler_parse(node, path, root, language="javascript")
    if compiler is not None:
        if compiler["errors"]:
            return {"status": "syntax_error", "language": "javascript", "text": text,
                    "error": "; ".join(compiler["errors"])}
        return {"status": "ok", "language": "javascript", "text": text, "functions": compiler["functions"],
                "parser": "typescript-compiler", "function_metrics_status": "exact"}
    try:
        checked = _node_syntax_check(node, path, root)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"status": "parser_unsupported", "language": "javascript", "text": text, "reason": type(error).__name__}
    if checked.returncode != 0:
        return {"status": "syntax_error", "language": "javascript", "text": text, "error": checked.stderr.strip()}
    functions = _javascript_functions(text)
    return {"status": "ok", "language": "javascript", "text": text, "functions": functions,
            "advisory_functions": functions, "parser": "node-check", "function_metrics_status": "advisory"}


def _analyze_typescript(path: Path, root: Path, text: str, node: str) -> dict:
    compiler = _typescript_compiler_parse(node, path, root)
    if compiler is not None:
        if compiler["errors"]:
            return {"status": "syntax_error", "language": "typescript", "text": text,
                    "error": "; ".join(compiler["errors"])}
        return {"status": "ok", "language": "typescript", "text": text, "functions": compiler["functions"],
                "parser": "typescript-compiler", "function_metrics_status": "exact"}
    if path.suffix.lower() == ".tsx":
        return {"status": "parser_unsupported", "language": "typescript", "text": text,
                "reason": "TypeScript compiler is required for TSX"}
    try:
        checked = _node_syntax_check(node, path, root, typescript=True)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"status": "parser_unsupported", "language": "typescript", "text": text, "reason": type(error).__name__}
    if checked.returncode == 0:
        functions = _typescript_functions(text)
        return {"status": "ok", "language": "typescript", "text": text, "functions": functions,
                "advisory_functions": functions, "parser": "node-strip-types", "function_metrics_status": "advisory"}
    message = checked.stderr.strip()
    if "bad option" in message or "unknown option" in message:
        return {"status": "parser_unsupported", "language": "typescript", "text": text,
                "reason": "Node 22+ or the TypeScript compiler is required"}
    return {"status": "syntax_error", "language": "typescript", "text": text,
            "error": message or "invalid TypeScript syntax"}


def analyze_source(path: Path, root: Path) -> dict:
    """Parse one file; syntax success and exact symbol metrics stay distinct."""
    path = Path(path)
    language = language_for(path)
    skipped: list[str] = []
    text = read_text(path, skipped)
    if text is None:
        return {"status": "parser_unsupported", "language": language, "text": "",
                "reason": "source read failed", "read_error": skipped[0] if skipped else f"{path}: unreadable"}
    if language == "python":
        try:
            return {"status": "ok", "language": language, "tree": ast.parse(text), "text": text,
                    "function_metrics_status": "exact", "parser": "python-ast"}
        except SyntaxError as error:
            return {"status": "syntax_error", "language": language, "error": str(error), "text": text}
    if language not in {"javascript", "typescript"}:
        return {"status": "parser_unsupported", "language": language, "text": text}
    node = shutil.which("node")
    if node is None:
        return {"status": "parser_unsupported", "language": language, "text": text, "reason": "node is unavailable"}
    parser = _analyze_javascript if language == "javascript" else _analyze_typescript
    return parser(path, Path(root), text, node)


def _javascript_functions(source: str) -> list[dict]:
    """Extract named ESM/local functions after Node has validated the source.

    This is intentionally an inventory, not a second syntax parser. Node owns
    syntax validity; the conservative scanner supplies stable symbols and
    branch counts for feature-scoped QA without requiring an npm dependency.
    """
    patterns = (
        re.compile(r"(?:^|\n)\s*(?P<exported>export\s+)?(?:default\s+)?(?:async\s+)?function\s+(?P<name>[A-Za-z_$][\w$]*)\s*\([^)]*\)[^{]*\{", re.MULTILINE),
        re.compile(r"(?:^|\n)\s*(?P<exported>export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>", re.MULTILINE),
    )
    return _functions_from_patterns(source, patterns)
