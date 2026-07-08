"""Semantic Software Architecture Tree — architecture as code, not a doc.

An SSAT is a YAML contract: modules, their public signatures, allowed
dependency edges, and invariants. It is BOTH the scaffold source AND the
CI gate — the same artifact that generates the skeleton also detects
'structural erosion' when filled code violates the declared boundaries.
"""
from __future__ import annotations
import ast, re
from dataclasses import dataclass, field
from pathlib import Path
import yaml

@dataclass
class ArchViolation:
    code: str
    message: str
    where: str = ""

def load_ssat(path: Path) -> dict:
    return yaml.safe_load(Path(path).read_text())

def scaffold_from_ssat(ssat: dict, out_dir: Path) -> list[Path]:
    """Generate signature-only files with valid imports from the SSAT."""
    out_dir = Path(out_dir); created = []
    for mod in ssat.get("modules", []):
        p = out_dir/mod["path"]
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = ['"""AUTO-SCAFFOLD from SSAT. Fill bodies only; do not change signatures."""']
        for imp in mod.get("imports", []):
            lines.append(f"import {imp}" if "." not in imp else f"from {imp.rsplit('.',1)[0]} import {imp.rsplit('.',1)[1]}")
        lines.append("")
        for fn in mod.get("functions", []):
            args = ", ".join(fn.get("args", []))
            ret = fn.get("returns", "None")
            lines.append(f"def {fn['name']}({args}) -> {ret}:")
            lines.append(f'    """{fn.get("doc","TODO")}"""')
            lines.append("    raise NotImplementedError  # FILL")
            lines.append("")
        p.write_text("\n".join(lines)); created.append(p)
    return created

def _module_of(path: str, ssat: dict) -> str | None:
    for mod in ssat.get("modules", []):
        if path.endswith(mod["path"]):
            return mod["name"]
    return None

def check_erosion(ssat: dict, src_dir: Path) -> list[ArchViolation]:
    """Architecture-as-CI-gate: detect signature drift + illegal dependency edges."""
    src_dir = Path(src_dir); violations = []
    allowed = {(e["from"], e["to"]) for e in ssat.get("dependencies", [])}
    names = {m["name"]: m for m in ssat.get("modules", [])}
    path_to_name = {m["path"]: m["name"] for m in ssat.get("modules", [])}
    for mod in ssat.get("modules", []):
        p = src_dir/mod["path"]
        if not p.exists():
            violations.append(ArchViolation("E_MISSING_MODULE", f"{mod['path']} declared in SSAT but absent"))
            continue
        try:
            tree = ast.parse(p.read_text())
        except SyntaxError as e:
            violations.append(ArchViolation("E_SYNTAX", str(e), mod["path"])); continue
        # signature drift
        declared = {f["name"]: f for f in mod.get("functions", [])}
        found = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        for fname, fspec in declared.items():
            if fname not in found:
                violations.append(ArchViolation("E_SIG_MISSING", f"{fname} declared but not implemented", mod["path"]))
                continue
            got_args = [a.arg for a in found[fname].args.args]
            want_args = [a.split(":")[0].strip() for a in fspec.get("args", [])]
            if got_args != want_args:
                violations.append(ArchViolation("E_SIG_DRIFT",
                    f"{fname}({got_args}) != SSAT ({want_args})", mod["path"]))
        # illegal dependency edges
        this = mod["name"]
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                for m in mods:
                    target = names.get(m.split(".")[-1]) or names.get(m)
                    tname = target["name"] if target else None
                    if tname and tname != this and (this, tname) not in allowed:
                        violations.append(ArchViolation("E_ILLEGAL_DEP",
                            f"{this} -> {tname} not in SSAT dependency allowlist", mod["path"]))
    for inv in ssat.get("invariants", []):
        pat = inv["forbid_pattern"]
        for mod in ssat.get("modules", []):
            p = src_dir/mod["path"]
            if p.exists() and re.search(pat, p.read_text()):
                violations.append(ArchViolation("E_INVARIANT", f"{inv['name']}: forbidden pattern {pat!r}", mod["path"]))
    return violations
