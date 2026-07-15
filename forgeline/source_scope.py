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
from typing import Iterable


IGNORED_DIRECTORIES = frozenset({
    ".git", ".pnpm", "node_modules", ".next", ".nuxt", ".forge", ".venv",
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
            try:
                candidate.relative_to(root)
                if candidate.is_file() and candidate.suffix.lower() in wanted:
                    result.append(candidate)
            except (FileNotFoundError, NotADirectoryError, OSError, ValueError) as error:
                skipped.append(f"{candidate}: {type(error).__name__}")
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
            path = Path(current) / name
            try:
                if path.suffix.lower() in wanted and path.is_file():
                    result.append(path)
            except (FileNotFoundError, NotADirectoryError, OSError) as error:
                skipped.append(f"{path}: {type(error).__name__}")
    return result


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
const ts = require('typescript');
const file = process.argv[1];
const mode = process.argv[2];
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
function complexity(node) {
  let total = 1;
  function walk(n, nested) {
    if (nested && (ts.isFunctionDeclaration(n) || ts.isMethodDeclaration(n) || ts.isArrowFunction(n) || ts.isFunctionExpression(n))) return;
    if (branches.has(n.kind)) total += 1;
    if (ts.isBinaryExpression(n) && (n.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken || n.operatorToken.kind === ts.SyntaxKind.BarBarToken)) total += 1;
    ts.forEachChild(n, child => walk(child, true));
  }
  ts.forEachChild(node, child => walk(child, false));
  return total;
}
const functions = [];
function visit(node) {
  if (ts.isFunctionDeclaration(node) || ts.isMethodDeclaration(node) || ts.isArrowFunction(node) || ts.isFunctionExpression(node)) {
    const comments = ts.getLeadingCommentRanges(source, node.getFullStart()) || [];
    functions.push({name: nameOf(node), complexity: complexity(node), documented: comments.some(c => source.slice(c.pos, c.end).startsWith('/**'))});
  }
  ts.forEachChild(node, visit);
}
visit(tree);
console.log(JSON.stringify({errors, functions}));
"""


def _typescript_compiler_parse(node: str, path: Path, root: Path) -> dict | None:
    """Use the TypeScript compiler API when the active project provides it."""
    try:
        completed = subprocess.run(
            [node, "-e", _NODE_TYPESCRIPT_AST, str(path), "typescript"], cwd=root,
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None


def _node_syntax_check(node: str, path: Path, root: Path, *, typescript: bool = False) -> subprocess.CompletedProcess[str]:
    args = [node]
    if typescript:
        # Node 22 can parse and erase ordinary TypeScript annotations without
        # an npm dependency. It is the portable fallback for .ts source.
        args.append("--experimental-strip-types")
    args.extend(["--check", str(path)])
    return subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=10, check=False)


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
            name = match.group(1)
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
            })
    return functions


def _typescript_functions(source: str) -> list[dict]:
    """Inventory conservative TS symbols after Node has validated syntax.

    This is intentionally not a parser. The compiler API remains the preferred
    source of rich symbols; the scanner keeps clean Node 22 environments from
    collapsing to a false zero-symbol result when ``typescript`` is absent.
    """
    patterns = (
        re.compile(r"(?:^|\n)\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\([^)]*\)[^{]*\{", re.MULTILINE),
        re.compile(r"(?:^|\n)\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)(?:\s*:\s*[^=;\n]+)?\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)(?:\s*:\s*[^=;\n]+)?\s*=>", re.MULTILINE),
        re.compile(r"(?:^|\n)\s*(?:public\s+|private\s+|protected\s+)?(?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*:\s*[^{};]+\{", re.MULTILINE),
    )
    return _functions_from_patterns(source, patterns)


def validate_generated_script(source: str, target: Path) -> str:
    """Validate generated JS/TS before the scaffold transaction changes files.

    TypeScript uses the compiler API when it is resolvable from the target's
    project. Node 22's type-strip parser is the self-contained fallback for
    ``.ts`` files. ``.tsx`` intentionally fails closed without the compiler,
    because Node's type-strip mode does not parse TSX.
    """
    target = Path(target)
    language = language_for(target)
    if language not in {"javascript", "typescript"}:
        raise ValueError(f"no script validator for {target}")
    # Node's --check intentionally accepts an incomplete function body in
    # check-only mode. Generated stubs have no brace-bearing literals, so this
    # small structural guard closes that gap before invoking the real parser.
    if re.search(r"^\s*def\s+", source, flags=re.MULTILINE) or source.count("{") != source.count("}"):
        raise ValueError(f"generated invalid {language} structure for {target}")
    node = shutil.which("node")
    if node is None:
        raise ValueError(f"Node.js is required to validate generated {language} for {target}")
    root = _nearest_existing_directory(target.parent)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=target.suffix, delete=False) as handle:
        handle.write(source)
        temporary = Path(handle.name)
    try:
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
    finally:
        temporary.unlink(missing_ok=True)


def analyze_source(path: Path, root: Path) -> dict:
    """Parse one file with its matching parser; unsupported never means syntax error."""
    path = Path(path)
    language = language_for(path)
    skipped: list[str] = []
    text = read_text(path, skipped)
    if text is None:
        return {
            "status": "parser_unsupported",
            "language": language,
            "text": "",
            "reason": "source disappeared during scan",
        }
    if language == "python":
        try:
            return {"status": "ok", "language": language, "tree": ast.parse(text), "text": text}
        except SyntaxError as error:
            return {"status": "syntax_error", "language": language, "error": str(error), "text": text}
    if language not in {"javascript", "typescript"}:
        return {"status": "parser_unsupported", "language": language, "text": text}
    if text.count("{") != text.count("}"):
        return {"status": "syntax_error", "language": language, "text": text, "error": "unbalanced braces"}
    node = shutil.which("node")
    if node is None:
        return {"status": "parser_unsupported", "language": language, "text": text, "reason": "node is unavailable"}
    # Node's parser is authoritative for JavaScript/ESM and does not need the
    # optional TypeScript package. That makes .mjs support deterministic in a
    # normal Node installation instead of silently producing zero symbols.
    if language == "javascript":
        try:
            checked = _node_syntax_check(node, path, Path(root))
        except (OSError, subprocess.TimeoutExpired) as error:
            return {"status": "parser_unsupported", "language": language, "text": text, "reason": type(error).__name__}
        if checked.returncode != 0:
            return {"status": "syntax_error", "language": language, "text": text, "error": checked.stderr.strip()}
        return {"status": "ok", "language": language, "text": text, "functions": _javascript_functions(text), "parser": "node-check"}
    compiler = _typescript_compiler_parse(node, path, Path(root))
    if compiler is not None:
        if compiler["errors"]:
            return {"status": "syntax_error", "language": language, "text": text, "error": "; ".join(compiler["errors"])}
        return {"status": "ok", "language": language, "text": text, "functions": compiler["functions"], "parser": "typescript-compiler"}
    if path.suffix.lower() == ".tsx":
        return {"status": "parser_unsupported", "language": language, "text": text, "reason": "TypeScript compiler is required for TSX"}
    try:
        checked = _node_syntax_check(node, path, Path(root), typescript=True)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"status": "parser_unsupported", "language": language, "text": text, "reason": type(error).__name__}
    if checked.returncode == 0:
        return {"status": "ok", "language": language, "text": text, "functions": _typescript_functions(text), "parser": "node-strip-types"}
    message = checked.stderr.strip()
    if "bad option" in message or "unknown option" in message:
        return {"status": "parser_unsupported", "language": language, "text": text, "reason": "Node 22+ or the TypeScript compiler is required"}
    return {"status": "syntax_error", "language": language, "text": text, "error": message or "invalid TypeScript syntax"}


def _javascript_functions(source: str) -> list[dict]:
    """Extract named ESM/local functions after Node has validated the source.

    This is intentionally an inventory, not a second syntax parser. Node owns
    syntax validity; the conservative scanner supplies stable symbols and
    branch counts for feature-scoped QA without requiring an npm dependency.
    """
    patterns = (
        re.compile(r"(?:^|\n)\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\([^)]*\)[^{]*\{", re.MULTILINE),
        re.compile(r"(?:^|\n)\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>", re.MULTILINE),
    )
    return _functions_from_patterns(source, patterns)
