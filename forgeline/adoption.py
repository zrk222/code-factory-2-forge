"""Existing-repository discovery for ForgeLine adoption."""
from __future__ import annotations

import json
from pathlib import Path
import re

import yaml


IGNORED = {".git", "node_modules", "dist", "build", ".next", "coverage", ".forge", ".venv", "venv"}


def _files(root: Path, suffixes: set[str]) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file() and path.suffix in suffixes
            and not any(part in IGNORED for part in path.parts)]


def _functions(path: Path, language: str) -> list[dict]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    if language == "python":
        return [{"name": name, "args": [arg.strip() for arg in args.split(",") if arg.strip()], "returns": ret or "unknown"}
                for name, args, ret in re.findall(r"^def\s+(\w+)\(([^)]*)\)(?:\s*->\s*([^:]+))?:", text, re.M)]
    return [{"name": name, "args": [arg.strip() for arg in args.split(",") if arg.strip()], "returns": ret or "unknown"}
            for name, args, ret in re.findall(r"export\s+(?:async\s+)?function\s+(\w+)\(([^)]*)\)\s*(?::\s*([^\{=]+))?", text)]


def inspect_repository(root: Path, feature: str) -> dict:
    root = Path(root).resolve()
    py_files = _files(root, {".py"})
    ts_files = _files(root, {".ts", ".tsx"})
    languages = []
    if py_files:
        languages.append("python")
    if ts_files:
        languages.append("typescript")
    package = root / "package.json"
    package_scripts = {}
    if package.exists():
        try:
            package_scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
        except json.JSONDecodeError:
            package_scripts = {}
    modules = []
    for language, files in (("python", py_files), ("typescript", ts_files)):
        for path in files[:80]:
            functions = _functions(path, language)
            if functions:
                modules.append({"name": re.sub(r"\W+", "_", str(path.relative_to(root).with_suffix(""))),
                                "path": str(path.relative_to(root)).replace("\\", "/"),
                                "language": language, "functions": functions})
    tests = [str(path.relative_to(root)).replace("\\", "/") for path in _files(root, {".py", ".ts", ".tsx"})
             if any(part in {"test", "tests", "__tests__"} for part in path.parts) or ".test." in path.name or ".spec." in path.name]
    commands = []
    if package_scripts.get("test"):
        commands.append("npm test")
    if (root / "pyproject.toml").exists() or py_files:
        commands.append("python -m pytest -q")
    return {
        "schema": "forgeline.adoption.v1", "feature": feature, "root": str(root),
        "languages": languages, "test_commands": commands, "test_files": tests[:100],
        "modules": modules,
        "scope_limits": [
            "Architecture is inferred from static file and export signatures; it is not human-approved architecture.",
            "Review the generated baseline before using it for any gate.",
            "TypeScript reverse verification uses explicit reviewed mutants; it never rewrites the working tree.",
        ],
    }


def adopt(root: Path, feature: str, *, out: Path | None = None, force: bool = False) -> dict:
    root = Path(root).resolve()
    payload = inspect_repository(root, feature)
    directory = root / ".forge" / feature
    directory.mkdir(parents=True, exist_ok=True)
    receipt = directory / "adoption.json"
    receipt.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    baseline = out or root / f"{feature}.adoption.ssat.yaml"
    if baseline.exists() and not force:
        raise FileExistsError(f"refusing to overwrite adoption baseline: {baseline}; pass --force after review")
    baseline.write_text(yaml.safe_dump({"name": feature, "language": ",".join(payload["languages"]) or "unknown",
                                        "modules": payload["modules"], "dependencies": [], "invariants": []},
                                       sort_keys=False, allow_unicode=False), encoding="utf-8")
    mutants = directory / "typescript-mutants.json"
    if "typescript" in payload["languages"] and not mutants.exists():
        mutants.write_text(json.dumps({"schema": "forgeline.typescript_mutants.v1", "feature": feature,
                                       "mutants": [], "note": "Add reviewed source mutations and targeted test commands before forge verify-tests-ts."}, indent=2), encoding="utf-8")
    return payload | {"adoption_receipt": str(receipt), "baseline": str(baseline),
                      "typescript_manifest": str(mutants) if mutants.exists() else None}
