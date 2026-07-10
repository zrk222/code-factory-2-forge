"""Reverse-classical test verification.

Before ForgeLine trusts a smoke check against the real implementation, it runs
the same behavioral check against the generated SSAT scaffold. A behavioral
test must fail on that empty stub. If it passes, the test is hollow: it asserts
nothing the implementation provides.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from forgeline.attribution import Attribution, FailureClass, GateResult, UnitResult
from forgeline.gates.runtime_smoke import _load_manifest, _run_check
from forgeline.ssat import load_ssat, scaffold_from_ssat


def materialize_stub_root(ssat_path: Path) -> Path:
    """Regenerate the SSAT scaffold into an isolated temp root.

    The production tree is never touched. Reusing `scaffold_from_ssat` is the
    guarantee: the mutant is byte-identical to what the normal SCAFFOLDED state
    would produce for the same SSAT.
    """
    tmp = Path(tempfile.mkdtemp(prefix="forge-stub-"))
    scaffold_from_ssat(load_ssat(ssat_path), tmp)
    return tmp


def verify_tests(root: Path, feature: str, ssat_path: Path) -> GateResult:
    root = Path(root)
    try:
        checks = _load_manifest(root, feature)
    except ValueError as exc:
        attr = Attribution("verify_tests", 1, 0, [
            UnitResult(
                unit="verify_tests:manifest",
                stage="verify_tests",
                passed=False,
                evidence=str(exc),
                failure_class=FailureClass.HOLLOW_MANIFEST,
            )
        ])
        return GateResult(False, attr)

    if checks is None:
        attr = Attribution("verify_tests", 1, 0, [
            UnitResult(
                unit="verify_tests:manifest",
                stage="verify_tests",
                passed=False,
                evidence=f"no smoke manifest at smoke/{feature}.json; nothing to verify",
                failure_class=FailureClass.HOLLOW_MANIFEST,
            )
        ])
        return GateResult(False, attr)

    if not checks:
        attr = Attribution("verify_tests", 1, 0, [
            UnitResult(
                unit="verify_tests:manifest",
                stage="verify_tests",
                passed=False,
                evidence="smoke manifest declares no checks",
                failure_class=FailureClass.HOLLOW_MANIFEST,
            )
        ])
        return GateResult(False, attr)

    if all(not check.must_fail_on_stub for check in checks):
        units = [
            UnitResult(
                unit=f"verify_tests:{check.name}",
                stage="verify_tests",
                passed=False,
                evidence="every check is exempt; manifest verifies no behavior",
                failure_class=FailureClass.HOLLOW_MANIFEST,
            )
            for check in checks
        ]
        return GateResult(False, Attribution("verify_tests", len(units), 0, units))

    stub_root = materialize_stub_root(ssat_path)
    try:
        units: list[UnitResult] = []
        for check in checks:
            unit = f"verify_tests:{check.name}"
            if not check.must_fail_on_stub:
                units.append(UnitResult(
                    unit=unit,
                    stage="verify_tests",
                    passed=True,
                    evidence="exempt: declared structural check",
                    failure_class=None,
                ))
                continue

            result = _run_check(check, stub_root)
            hollow = result.passed
            units.append(UnitResult(
                unit=unit,
                stage="verify_tests",
                passed=not hollow,
                evidence=(
                    "check PASSED against an empty stub; it asserts nothing the "
                    "implementation provides"
                    if hollow
                    else f"correctly failed on stub: {result.reason}"
                ),
                failure_class=FailureClass.HOLLOW_TEST if hollow else None,
            ))
    finally:
        shutil.rmtree(stub_root, ignore_errors=True)

    attr = Attribution("verify_tests", len(units), sum(unit.passed for unit in units), units)
    return GateResult(attr.rate == 1.0, attr)

