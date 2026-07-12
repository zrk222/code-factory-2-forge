"""Reverse-classical verification for reviewed TypeScript source mutants."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

from forgeline.attribution import Attribution, FailureClass, GateResult, UnitResult


def verify_typescript_tests(root: Path, feature: str, manifest_path: Path | None = None) -> GateResult:
    root = Path(root).resolve()
    manifest_path = manifest_path or root / ".forge" / feature / "typescript-mutants.json"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        mutants = data.get("mutants", [])
    except (OSError, json.JSONDecodeError) as error:
        return GateResult(False, Attribution("verify_tests_ts", 1, 0, [UnitResult("typescript:manifest", "verify_tests_ts", False, str(error), FailureClass.HOLLOW_MANIFEST)]))
    if not mutants:
        return GateResult(False, Attribution("verify_tests_ts", 1, 0, [UnitResult("typescript:manifest", "verify_tests_ts", False, "TypeScript mutant manifest declares no reviewed mutants", FailureClass.HOLLOW_MANIFEST)]))
    tmp = Path(tempfile.mkdtemp(prefix="forge-ts-mutant-"))
    try:
        shutil.copytree(root, tmp, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", "node_modules", "dist", "build", ".next", "coverage", ".forge"))
        units = []
        for mutant in mutants:
            name = mutant["name"]
            target = tmp / mutant["path"]
            if not target.exists():
                units.append(UnitResult(f"typescript:{name}", "verify_tests_ts", False, f"mutant target missing: {mutant['path']}", FailureClass.HOLLOW_MANIFEST))
                continue
            source = target.read_text(encoding="utf-8")
            changed, count = re.subn(mutant["replace_regex"], mutant["replacement"], source, count=1, flags=re.S)
            if count != 1:
                units.append(UnitResult(f"typescript:{name}", "verify_tests_ts", False, "reviewed mutation did not match exactly once", FailureClass.HOLLOW_MANIFEST))
                continue
            target.write_text(changed, encoding="utf-8")
            t0 = time.monotonic()
            try:
                proc = subprocess.run(mutant["command"], cwd=tmp, shell=True, capture_output=True, text=True,
                                      timeout=int(mutant.get("timeout_s", 60)))
                duration = int((time.monotonic() - t0) * 1000)
                hollow = proc.returncode == 0
                evidence = ("test command passed against reviewed TypeScript mutant" if hollow else
                            f"test command failed on mutant (exit {proc.returncode}, {duration}ms)")
                units.append(UnitResult(f"typescript:{name}", "verify_tests_ts", not hollow, evidence,
                                        FailureClass.HOLLOW_TEST if hollow else None))
            except subprocess.TimeoutExpired:
                units.append(UnitResult(f"typescript:{name}", "verify_tests_ts", False, "test command timed out on mutant", FailureClass.RUNTIME_TIMEOUT))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    attr = Attribution("verify_tests_ts", len(units), sum(unit.passed for unit in units), units)
    return GateResult(attr.rate == 1.0, attr)
