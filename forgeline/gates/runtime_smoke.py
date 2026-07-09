"""forgeline runtime smoke gate — behavior-by-inspection before ship.

The rest of ForgeLine verifies code against *specifications*: the judge checks
consistency, the QA audit grades static quality, the intent thread proves the
code honors the sealed envelope. All of that is correctness-*by-construction*.

None of it answers the question a per-PR preview deployment answers: **does the
built thing actually RUN and behave correctly when executed?** A change can pass
every static gate and still crash on import, throw at runtime, or produce the
wrong output. This gate closes that gap at the right scale for a solo builder:
it runs the artifact against declared behavioral checks and blocks ship on any
runtime failure — without the cost of ephemeral per-PR environments.

Design contract:
  - Deterministic pass/fail: a check either ran green or it didn't.
  - Isolated: checks run in a subprocess with a timeout, so a hang or crash in
    the built code cannot take down the orchestrator.
  - Declarative: behavioral checks live in a `smoke/` manifest beside the spec,
    so what "correct runtime behavior" means is itself a reviewed artifact.
  - Fail-closed: no manifest, or a manifest that references nothing runnable,
    is a BLOCK — you cannot ship unverified runtime behavior by omission.
"""
from __future__ import annotations
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from forgeline.attribution import Attribution, FailureClass, GateResult, UnitResult


@dataclass
class SmokeCheck:
    name: str
    kind: str           # "command" | "python"
    run: str            # shell command, or python snippet/file
    expect_exit: int = 0
    expect_stdout: str | None = None   # substring that must appear
    timeout_s: int = 30


@dataclass
class SmokeResult:
    name: str
    passed: bool
    reason: str
    duration_ms: int = 0


@dataclass
class SmokeReport:
    results: list[SmokeResult] = field(default_factory=list)
    manifest_found: bool = True

    @property
    def ok(self) -> bool:
        return self.manifest_found and bool(self.results) and all(r.passed for r in self.results)

    @property
    def failures(self):
        return [r for r in self.results if not r.passed]

    def add(self, name, passed, reason, duration_ms=0):
        self.results.append(SmokeResult(name, passed, reason, duration_ms))

    @property
    def attribution(self) -> Attribution:
        units = []
        for result in self.results:
            failure_class = None
            reason = result.reason.lower()
            if not result.passed:
                if "timed out" in reason:
                    failure_class = FailureClass.RUNTIME_TIMEOUT
                elif "expected stdout" in reason:
                    failure_class = FailureClass.WRONG_OUTPUT
                else:
                    failure_class = FailureClass.RUNTIME_CRASH
            units.append(UnitResult(
                unit=f"smoke:{result.name}",
                stage="smoke",
                passed=result.passed,
                evidence=result.reason,
                failure_class=failure_class,
            ))
        return Attribution("smoke", len(units), sum(unit.passed for unit in units), units)

    @property
    def gate_result(self) -> GateResult:
        attr = self.attribution
        return GateResult(attr.n_checked > 0 and attr.rate == 1.0, attr)


def _load_manifest(root: Path, feature: str) -> list[SmokeCheck] | None:
    """A smoke manifest is JSON at smoke/<feature>.json (or smoke/smoke.json).
    Each entry declares one behavioral check. Missing manifest -> None (fail-closed
    upstream)."""
    candidates = [
        Path(root) / "smoke" / f"{feature}.json",
        Path(root) / "smoke" / "smoke.json",
    ]
    for p in candidates:
        if p.exists():
            try:
                data = json.loads(p.read_text())
            except json.JSONDecodeError as e:
                raise ValueError(f"smoke manifest {p} is not valid JSON: {e}")
            checks = []
            for entry in data.get("checks", []):
                checks.append(SmokeCheck(
                    name=entry["name"],
                    kind=entry.get("kind", "command"),
                    run=entry["run"],
                    expect_exit=entry.get("expect_exit", 0),
                    expect_stdout=entry.get("expect_stdout"),
                    timeout_s=entry.get("timeout_s", 30),
                ))
            return checks
    return None


def _run_check(check: SmokeCheck, cwd: Path) -> SmokeResult:
    """Execute one check in an isolated subprocess with a hard timeout."""
    t0 = time.monotonic()
    if check.kind == "python":
        cmd = [sys.executable, "-c", check.run]
    else:
        cmd = check.run  # shell command string
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            shell=(check.kind != "python"),
            capture_output=True,
            text=True,
            timeout=check.timeout_s,
        )
    except subprocess.TimeoutExpired:
        ms = int((time.monotonic() - t0) * 1000)
        return SmokeResult(check.name, False,
                           f"timed out after {check.timeout_s}s — runtime hang", ms)
    except (OSError, ValueError) as e:
        ms = int((time.monotonic() - t0) * 1000)
        return SmokeResult(check.name, False, f"could not execute: {e}", ms)

    ms = int((time.monotonic() - t0) * 1000)
    # exit-code check
    if proc.returncode != check.expect_exit:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        return SmokeResult(check.name, False,
                           f"exit {proc.returncode} != expected {check.expect_exit}; "
                           f"last: {' | '.join(tail)[:200]}", ms)
    # stdout-substring check
    if check.expect_stdout is not None and check.expect_stdout not in (proc.stdout or ""):
        return SmokeResult(check.name, False,
                           f"expected stdout to contain {check.expect_stdout!r}, not found", ms)
    return SmokeResult(check.name, True, "ran green", ms)


def runtime_smoke(root: Path, feature: str) -> SmokeReport:
    """Run all declared behavioral checks for a feature. Fail-closed: no manifest,
    empty manifest, or any failing check blocks ship."""
    rep = SmokeReport()
    root = Path(root)
    try:
        checks = _load_manifest(root, feature)
    except ValueError as e:
        rep.manifest_found = True
        rep.add("manifest", False, str(e))
        return rep
    if checks is None:
        rep.manifest_found = False
        rep.add("manifest", False,
                f"no smoke manifest at smoke/{feature}.json — cannot verify runtime "
                f"behavior. Declare at least one behavioral check before ship.")
        return rep
    if not checks:
        rep.add("manifest", False, "smoke manifest present but declares no checks — "
                                   "runtime behavior would ship unverified.")
        return rep
    for c in checks:
        rep.results.append(_run_check(c, root))
    return rep


def smoke_report_lines(rep: SmokeReport) -> list[str]:
    out = []
    for r in rep.results:
        mark = "PASS" if r.passed else "FAIL"
        out.append(f"[{mark}] {r.name} ({r.duration_ms}ms): {r.reason}")
    return out
