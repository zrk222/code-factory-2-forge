"""Deterministic refinement policy: one edit, ordered, Pareto-gated, bounded."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from pathlib import Path
import json

from .attribution import FailureClass


@dataclass(frozen=True)
class Edit:
    edit_class: str
    target: str
    description: str


STRUCTURAL_FAILURES = {
    FailureClass.SIGNATURE_DRIFT,
    FailureClass.STUB_UNFILLED,
    FailureClass.INCONSISTENT_LOGIC,
    FailureClass.SCOPE_ESCAPE,
    FailureClass.HOLLOW_TEST,
    FailureClass.HOLLOW_MANIFEST,
}


def select_edit(failure_class: FailureClass) -> Edit:
    edit_class = "structural" if failure_class in STRUCTURAL_FAILURES else (
        "configuration" if failure_class in {
            FailureClass.RUNTIME_CRASH, FailureClass.RUNTIME_TIMEOUT,
            FailureClass.SECURITY_FINDING,
        } else "parametric"
    )
    return Edit(edit_class, failure_class.value, f"localized {edit_class} correction")


def pareto_win(current: dict[str, float], previous: dict[str, float], target: str) -> bool:
    return (
        current.get(target, 0.0) > previous.get(target, 0.0)
        and all(current.get(stage, 0.0) >= rate for stage, rate in previous.items())
    )


class RejectionLedger:
    def __init__(self, root: Path):
        self.path = Path(root) / ".forge" / "rejection_ledger.jsonl"

    def log(self, edit: Edit, before: dict, after: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({
                "edit": asdict(edit), "before_rates": before, "after_rates": after,
            }, sort_keys=True) + "\n")


def refine(evaluate, propose, apply, revert, root: Path, max_iters: int = 6) -> dict:
    previous = evaluate()
    no_win_streak = 0
    ledger = RejectionLedger(root)
    for iteration in range(max_iters):
        if all(rate == 1.0 for rate in previous.values()):
            return {"converged": True, "iters": iteration}
        target, failure_class = propose(previous)
        edit = select_edit(failure_class)
        snapshot = apply(edit)
        current = evaluate()
        if pareto_win(current, previous, target):
            previous = current
            no_win_streak = 0
            continue
        revert(snapshot)
        ledger.log(edit, previous, current)
        no_win_streak += 1
        if no_win_streak >= 2:
            return {"converged": False, "reason": "plateau", "iters": iteration + 1}
    return {"converged": False, "reason": "budget", "iters": max_iters}
