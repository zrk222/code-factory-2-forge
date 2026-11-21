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
_UNSET = object()


def _scoped_subprocess_nodes(root, review_scopes, declared_modules, source_paths):
    """Resolve explicit adversary scopes against declared Python modules."""
    complaints = []
    if not isinstance(review_scopes, list) or not review_scopes:
        return {}, ["A_REVIEW_SCOPE review_scopes must be a non-empty list"]
    if not isinstance(declared_modules, list):
        return {}, ["A_REVIEW_SCOPE declared SSAT modules are missing or invalid"]
    modules = {}
    for module in declared_modules:
        if not isinstance(module, dict) or not isinstance(module.get("name"), str) or not isinstance(module.get("path"), str):
            complaints.append("A_REVIEW_SCOPE declared module requires name and path")
            continue
        if module["name"] in modules:
            complaints.append(f"A_REVIEW_SCOPE duplicate declared module {module['name']}")
        modules[module["name"]] = module
    inventory = set(iter_source_files(root, paths=source_paths)) if source_paths is not None else None
    by_path, seen, parsed_by_path = {}, set(), {}
    for scope in review_scopes:
        if not isinstance(scope, dict) or set(scope) != {"module", "symbol", "max_lines"}:
            complaints.append("A_REVIEW_SCOPE each scope must contain only module, symbol, and max_lines")
            continue
        module_name, symbol, max_lines = scope["module"], scope["symbol"], scope["max_lines"]
        if not isinstance(module_name, str) or module_name not in modules:
            complaints.append(f"A_REVIEW_SCOPE unknown declared module {module_name!r}")
            continue
        if (not isinstance(symbol, str) or not symbol.isidentifier() or isinstance(max_lines, bool)
                or not isinstance(max_lines, int) or not 1 <= max_lines <= 1000):
            complaints.append(f"A_REVIEW_SCOPE {module_name}: symbol or max_lines is invalid (1..1000 required)")
            continue
        identity = (module_name, symbol)
        if identity in seen:
            complaints.append(f"A_REVIEW_SCOPE duplicate symbol {module_name}.{symbol}")
            continue
        seen.add(identity)
        module_path = Path(modules[module_name]["path"])
        if module_path.is_absolute():
            complaints.append(f"A_REVIEW_SCOPE {module_name}: absolute module path is not allowed")
            continue
        path = (root / module_path).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            complaints.append(f"A_REVIEW_SCOPE {module_name}: module path escapes source root")
            continue
        if path.suffix.lower() != ".py":
            complaints.append(f"A_REVIEW_SCOPE {module_name}.{symbol}: only Python AST scopes are supported")
            continue
        if inventory is not None and path not in inventory:
            complaints.append(f"A_REVIEW_SCOPE {module_name}.{symbol}: declared module is outside source_paths")
            continue
        if path not in parsed_by_path:
            parsed_by_path[path] = analyze_source(path, root)
        parsed = parsed_by_path[path]
        if parsed.get("status") != "ok" or parsed.get("language") != "python":
            complaints.append(f"A_REVIEW_SCOPE {module_name}.{symbol}: Python AST is unavailable")
            continue
        matches = [node for node in ast.walk(parsed["tree"])
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol]
        if len(matches) != 1:
            complaints.append(f"A_REVIEW_SCOPE {module_name}.{symbol}: expected one exact function/class definition")
            continue
        node = matches[0]
        end_line = getattr(node, "end_lineno", None)
        start_line = min([node.lineno, *(decorator.lineno for decorator in getattr(node, "decorator_list", []))])
        if end_line is None or end_line - start_line + 1 > max_lines:
            complaints.append(f"A_REVIEW_SCOPE {module_name}.{symbol}: definition exceeds max_lines={max_lines}")
            continue
        by_path.setdefault(path, []).append(node)
    return by_path, complaints


def _subprocess_in_scope(tree, scope):
    """Conservatively detect subprocess references/calls in one AST subtree."""
    subprocess_names, importlib_names, builtins_names = {"subprocess"}, {"importlib"}, {"builtins"}
    import_module_names, dynamic_import_names = {"import_module"}, {"__import__"}
    wildcard_subprocess_import = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    subprocess_names.add(alias.asname or "subprocess")
                elif alias.name.split(".", 1)[0] == "importlib":
                    importlib_names.add(alias.asname or alias.name.split(".", 1)[0])
                elif alias.name == "builtins":
                    builtins_names.add(alias.asname or "builtins")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "subprocess":
                if any(alias.name == "*" for alias in node.names):
                    wildcard_subprocess_import = True
                subprocess_names.update(alias.asname or alias.name for alias in node.names if alias.name != "*")
            elif node.module == "importlib":
                import_module_names.update(alias.asname or alias.name for alias in node.names if alias.name == "import_module")
            elif node.module == "builtins":
                dynamic_import_names.update(alias.asname or alias.name for alias in node.names if alias.name == "__import__")
    if wildcard_subprocess_import:
        return True

    def target_names(target):
        if isinstance(target, ast.Name):
            return {target.id}
        if isinstance(target, (ast.Tuple, ast.List)):
            return set().union(*(target_names(item) for item in target.elts))
        return set()

    alias_groups = (subprocess_names, importlib_names, builtins_names, import_module_names, dynamic_import_names)
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
                value = node.value.id
                targets = set().union(*(target_names(target) for target in node.targets))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Name):
                value, targets = node.value.id, target_names(node.target)
            elif isinstance(node, ast.NamedExpr) and isinstance(node.value, ast.Name):
                value, targets = node.value.id, target_names(node.target)
            elif (isinstance(node, ast.Assign) and isinstance(node.value, ast.Attribute)
                  and node.value.attr == "import_module" and isinstance(node.value.value, ast.Name)
                  and node.value.value.id in importlib_names):
                value = node.value.attr
                targets = set().union(*(target_names(target) for target in node.targets))
            elif (isinstance(node, ast.Assign) and isinstance(node.value, ast.Attribute)
                  and node.value.attr == "__import__" and isinstance(node.value.value, ast.Name)
                  and node.value.value.id in builtins_names):
                value = node.value.attr
                targets = set().union(*(target_names(target) for target in node.targets))
            else:
                continue
            for aliases in alias_groups:
                if value in aliases and not targets.issubset(aliases):
                    aliases.update(targets)
                    changed = True
    for node in ast.walk(scope):
        if isinstance(node, ast.Import) and any(alias.name == "subprocess" for alias in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            return True
        if isinstance(node, ast.Name) and node.id in subprocess_names:
            return True
        if isinstance(node, ast.Name) and node.id in importlib_names | builtins_names:
            return True
        if isinstance(node, ast.Name) and node.id in dynamic_import_names:
            return True
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in dynamic_import_names | import_module_names:
                return True
    return False
SECRET = re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*=\s*['\"][A-Za-z0-9/\+_-]{12,}['\"]")

def grumpy_review(
    src_dir: Path,
    require_tests: bool = True,
    *,
    source_paths: Iterable[Path] | None = None,
    test_paths: Iterable[Path] | None = None,
    review_scopes=_UNSET,
    declared_modules=None,
) -> tuple[bool, list[str]]:
    """Returns (satisfied, complaints). Grumpy is satisfied only when it can
    find NO obvious sin AND the generator supplied tests to prove correctness."""
    src_dir = Path(src_dir).resolve(); complaints = []
    if source_paths is not None:
        source_paths = tuple(source_paths)
    scoped_nodes = {}
    if review_scopes is not _UNSET:
        scoped_nodes, scope_errors = _scoped_subprocess_nodes(
            src_dir, review_scopes, declared_modules, source_paths
        )
        complaints.extend(scope_errors)
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
            if needle == "subprocess" and p in scoped_nodes:
                continue
            if needle in text:
                complaints.append(f"{msg} in {p.relative_to(src_dir).as_posix()}")
        nodes = scoped_nodes.get(p, [])
        if nodes and _subprocess_in_scope(parsed.get("tree"), ast.Module(body=nodes, type_ignores=[])):
            complaints.append(f"A_SUBPROC subprocess use in scoped symbol in {p.relative_to(src_dir).as_posix()}")
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
