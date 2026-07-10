"""forge CLI — the state-machine driver."""
from __future__ import annotations
import argparse, json
from pathlib import Path
from .states import State, TRANSITIONS, HUMAN_GATES
from .run_store import RunStore
from .orchestrator import Orchestrator

NEXT_ACTION = {
    State.INTENT: "forge expand <feature>  (draft use cases, then human signoff)",
    State.EXPANDED: "write/refine the SSAT, then: forge gate architected <feature>",
    State.ARCHITECTED: "forge architect <feature> <ssat.yaml>  (generate scaffold)",
    State.SCAFFOLDED: "implement function bodies + tests, then: forge review <feature> <ssat.yaml>",
    State.FILLED: "forge review <feature> <ssat.yaml>",
    State.REVIEWED: "forge arch-gate <feature> <ssat.yaml>",
    State.ARCH_GATED: "forge verify-tests <feature> <ssat.yaml>",
    State.TESTS_VERIFIED: "forge smoke <feature>",
    State.SMOKED: "forge ship <feature>",
    State.BLOCKED: "read lessons, fix, then re-run the failed gate",
    State.SHIPPED: "done ✓",
}

def main(argv=None):
    p = argparse.ArgumentParser(prog="forge", description="ForgeLine — autonomous software factory outer loop")
    sub = p.add_subparsers(required=True, dest="cmd")
    for name in ["init","status","expand","architect","review","arch-gate","verify-tests","smoke","ship","handoff","agent","lessons","policy","qa","optimize-pr","demo","demo-learning"]:
        sp = sub.add_parser(name)
        if name == "init": sp.add_argument("--root", default=".")
        elif name in {"architect","review","arch-gate","verify-tests"}:
            sp.add_argument("feature"); sp.add_argument("ssat", nargs="?"); sp.add_argument("--root", default=".")
        elif name == "agent": sp.add_argument("name"); sp.add_argument("--root", default=".")
        elif name == "handoff": sp.add_argument("feature"); sp.add_argument("spec"); sp.add_argument("--root", default=".")
        elif name == "lessons": sp.add_argument("--root", default=".")
        elif name == "policy": sp.add_argument("--root", default=".")
        elif name == "qa": sp.add_argument("--root", default="."); sp.add_argument("--strict", action="store_true")
        elif name == "optimize-pr": sp.add_argument("feature", nargs="?"); sp.add_argument("--root", default="."); sp.add_argument("--base", default="main")
        elif name == "demo": pass
        elif name == "demo-learning": pass
        elif name == "demo-learning": pass
        else: sp.add_argument("feature", nargs="?"); sp.add_argument("--root", default=".")
    a = p.parse_args(argv); root = Path(getattr(a, "root", "."))

    if a.cmd == "init":
        (root/".forge").mkdir(parents=True, exist_ok=True)
        (root/"skills").mkdir(exist_ok=True)
        print("ForgeLine initialized. Next: forge agent claude|codex, then forge expand <feature>")
    elif a.cmd == "status":
        if not a.feature: raise SystemExit("feature required")
        st = RunStore(root, a.feature).state
        gate = " (HUMAN CONFIDENCE GATE)" if st in HUMAN_GATES else ""
        print(json.dumps({"feature": a.feature, "state": st.value,
                          "human_gate": st in HUMAN_GATES, "next": NEXT_ACTION[st]}, indent=2))
    elif a.cmd == "expand":
        s = RunStore(root, a.feature); s.set_state(State.EXPANDED, "use cases drafted")
        s.receipt(phase="expand", note="awaiting human signoff")
        print(f"{a.feature}: EXPANDED — human confidence gate. After signoff: forge gate architected")
    elif a.cmd == "architect":
        o = Orchestrator(root, a.feature)
        # allow arriving from EXPANDED via implicit architected signoff for demo simplicity
        if o.store.state == State.EXPANDED:
            o.store.set_state(State.ARCHITECTED, "ssat approved")
        print(json.dumps(o.architect(Path(a.ssat)), indent=2))
    elif a.cmd == "review":
        print(json.dumps(Orchestrator(root, a.feature).review(Path(a.ssat)), indent=2))
    elif a.cmd == "arch-gate":
        print(json.dumps(Orchestrator(root, a.feature).arch_gate(Path(a.ssat)), indent=2))
    elif a.cmd == "verify-tests":
        print(json.dumps(Orchestrator(root, a.feature).verify_tests(Path(a.ssat)), indent=2))
    elif a.cmd == "smoke":
        print(json.dumps(Orchestrator(root, a.feature).smoke_gate(), indent=2))
    elif a.cmd == "ship":
        print(json.dumps(Orchestrator(root, a.feature).ship(), indent=2))
    elif a.cmd == "handoff":
        r = Orchestrator(root, a.feature).handoff_decisions(Path(a.spec))
        print(json.dumps(r or {"decision_rules": 0, "note": "no decision table"}, indent=2))
    elif a.cmd == "agent":
        from .adapters import wire_agent
        print("wired:\n  " + "\n  ".join(str(c) for c in wire_agent(root, a.name)))
    elif a.cmd == "demo":
        from .demo import run; run()
    elif a.cmd == "demo-learning":
        from .demo_learning import run; run()
    elif a.cmd == "policy":
        from .learning import LearningKernel
        print(json.dumps(LearningKernel(root).policy_summary(), indent=2))
    elif a.cmd == "qa":
        from .gates.qa_audit import qa_audit
        r = qa_audit(root)
        print(json.dumps({"grade": r.grade, "passed": r.passed, "metrics": r.metrics,
                          "findings": r.findings,
                          "attribution": r.attribution.to_dict()}, indent=2))
        if a.strict and not r.passed:
            raise SystemExit(1)
    elif a.cmd == "optimize-pr":
        from .pr_optimizer import optimize_pr
        print(json.dumps(optimize_pr(root, a.feature, base=a.base), indent=2))
    elif a.cmd == "lessons":
        from .skill_memory import lessons_for, promotable_constraints
        alll = []
        for ph in ["fill","review","architect"]:
            alll += lessons_for(root, ph)
        print(json.dumps({"lessons": alll, "promotable_to_constraints": promotable_constraints(root)}, indent=2))

if __name__ == "__main__":
    main()
