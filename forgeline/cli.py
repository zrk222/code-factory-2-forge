"""forge CLI — the state-machine driver."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from .states import State, HUMAN_GATES
from .run_store import RunStore
from .orchestrator import Orchestrator

NEXT_ACTION = {
    State.INTENT: "forge expand <feature> (draft use cases; signoff)",
    State.EXPANDED: "write SSAT; then: forge gate architected <feature>",
    State.ARCHITECTED: "forge architect <feature> <ssat.yaml> (--adopt-existing to adopt)",
    State.SCAFFOLDED: "implement bodies/tests; forge fill <feature> <ssat.yaml>",
    State.FILLED: "forge review <feature> <ssat.yaml>",
    State.REVIEWED: "forge arch-gate <feature> <ssat.yaml>",
    State.ARCH_GATED: "forge verify-tests <feature> <ssat.yaml>",
    State.TESTS_VERIFIED: "forge smoke <feature>",
    State.SMOKED: "forge ship <feature>",
    State.BLOCKED: "read lessons, fix, then re-run the failed gate",
    State.SHIPPED: "done ✓",
}

def _emit_version(as_json: bool) -> None:
    from .provenance import provenance
    payload = provenance()
    print(json.dumps(payload, indent=2, sort_keys=True) if as_json else f"forge {payload['version']}")

def _review_command(root, args):
    root = root.resolve()
    material = response = None
    if args.jev_material:
        source = root / args.jev_material; path = source.resolve()
        if source.is_symlink() or any(parent.is_symlink() for parent in source.parents if parent != root.parent) or not path.is_file(): raise SystemExit("FORGE_CONTEXT_INVALID: material must be local")
        path.relative_to(root); data = path.read_bytes()
        try: material = json.loads(data.decode("utf-8")) if len(data) <= 256000 else {"profile": "invalid"}
        except (UnicodeDecodeError, json.JSONDecodeError): material = {"profile": "invalid"}
        if args.jev_response:
            response_source = root / args.jev_response; response_path = response_source.resolve()
            if response_source.is_symlink() or any(parent.is_symlink() for parent in response_source.parents if parent != root.parent) or not response_path.is_file(): raise SystemExit("JEV_REVIEW_INVALID: response must be a local file")
            response_path.relative_to(root); raw = response_path.read_bytes()
            try: response = json.loads(raw.decode("utf-8")) if len(raw) <= 256000 else {"oversized": True}
            except (UnicodeDecodeError, json.JSONDecodeError): response = {"malformed": True}
    elif args.jev_response: raise SystemExit("--jev-response requires --jev-material")
    reviewer = None
    if material is not None:
        def reviewer(packet):
            from factoryline.jev_classifier import review_forgeline_packet
            return review_forgeline_packet(packet, transport=(lambda request: response) if response is not None else None)
    print(json.dumps(Orchestrator(root, args.feature).review(Path(args.ssat), material=material, reviewer=reviewer), indent=2))


def _command_parser(sp, name):
    root = lambda: sp.add_argument("--root", default=".")
    feature = lambda optional=False: sp.add_argument("feature", nargs="?" if optional else None)
    if name in {"architect", "fill", "review", "arch-gate", "verify-tests"}:
        feature(); sp.add_argument("ssat", nargs="?"); root()
        if name == "review":
            sp.add_argument("--jev-material"); sp.add_argument("--jev-response")
        if name == "architect":
            sp.add_argument("--dry-run", action="store_true", help="plan scaffold actions without writing files")
            group = sp.add_mutually_exclusive_group()
            group.add_argument("--force", action="store_true", help="overwrite targets with backups")
            group.add_argument("--adopt-existing", action="store_true", help="validate existing targets; scaffold missing ones")
    elif name == "init": root()
    elif name == "adopt":
        feature(); root(); sp.add_argument("--out"); sp.add_argument("--force", action="store_true")
    elif name == "verify-tests-ts":
        feature(); root(); sp.add_argument("--manifest")
    elif name == "gate":
        sp.add_argument("phase", choices=["architected"]); feature(); root()
    elif name == "challenge":
        feature(); sp.add_argument("ssat"); root(); sp.add_argument("--out")
    elif name in {"agent", "handoff"}:
        sp.add_argument("name" if name == "agent" else "feature")
        if name == "handoff": sp.add_argument("spec")
        root()
    elif name in {"lessons", "policy"}: root()
    elif name == "qa":
        feature(True); sp.add_argument("--ssat"); sp.add_argument("--repo-wide", action="store_true"); root(); sp.add_argument("--strict", action="store_true")
    elif name == "optimize-pr":
        feature(True); root(); sp.add_argument("--base", default="main")
    elif name == "version": sp.add_argument("--json", action="store_true")
    elif name not in {"demo", "demo-learning"}: feature(True); root()


def _parser(argv):
    parser = argparse.ArgumentParser(prog="forge", description="ForgeLine CLI")
    sub = parser.add_subparsers(required=True, dest="cmd")
    names = "init adopt status expand gate architect fill review arch-gate verify-tests verify-tests-ts challenge smoke ship handoff agent lessons policy qa optimize-pr demo demo-learning version".split()
    for name in names: _command_parser(sub.add_parser(name), name)
    return parser.parse_args(argv)


def _orchestrator_command(args, root):
    if args.cmd == "review": return _review_command(root, args)
    obj = Orchestrator(root, args.feature)
    calls = {"gate": lambda: obj.approve_architecture(), "architect": lambda: obj.architect(Path(args.ssat), force=args.force, adopt_existing=args.adopt_existing, dry_run=args.dry_run),
             "fill": lambda: obj.fill(Path(args.ssat)),
             "arch-gate": lambda: obj.arch_gate(Path(args.ssat)), "verify-tests": lambda: obj.verify_tests(Path(args.ssat)),
             "smoke": lambda: obj.smoke_gate(), "ship": lambda: obj.ship()}
    result = calls[args.cmd](); print(json.dumps(result, indent=2))
    keys = {"gate": ("approved", False), "architect": ("scaffolded", False), "fill": ("filled", False)}
    if args.cmd in keys and not result[keys[args.cmd][0]] and not (args.cmd == "architect" and args.dry_run): raise SystemExit(1)


def _run_challenge(args, root):
    from .gates.reverse_classical import verify_tests
    gate = verify_tests(root, args.feature, Path(args.ssat)); attr = gate.attribution.to_dict()
    payload = {"schema": "factory.challenge.v1", "brick": "forgeline", "feature": args.feature,
               "stage": "verify_tests", "passed": gate.passed and attr["n_checked"] > 0,
               "mutants_total": attr["n_checked"], "mutants_killed": attr["n_passed"], "attribution": attr}
    out = Path(args.out) if args.out else root / ".forge" / args.feature / "challenge.json"
    out.parent.mkdir(parents=True, exist_ok=True); out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload | {"receipt_path": str(out)}, indent=2))
    if not payload["passed"]: raise SystemExit(1)


def _run_qa(args, root):
    from .gates.qa_audit import qa_audit
    from .source_scope import declared_paths
    from .ssat import load_ssat
    if not args.repo_wide and not args.ssat:
        print(json.dumps({"passed": False, "error": {"code": "E_SCOPE_REQUIRED", "message": "feature QA requires --ssat; --repo-wide is non-gating",
                          "next": f"forge qa {args.feature} --ssat <{args.feature}.ssat.yaml> --root ."}}, indent=2))
        raise SystemExit(2)
    paths = None if args.repo_wide else declared_paths(root, load_ssat(Path(args.ssat)))
    result = qa_audit(root, source_paths=paths)
    print(json.dumps({"grade": result.grade, "passed": result.passed, "metrics": result.metrics, "findings": result.findings,
                      "attribution": result.attribution.to_dict()}, indent=2))
    if args.strict and not result.passed: raise SystemExit(1)


def _run_basic(args, root):
    cmd = args.cmd
    if cmd == "version": _emit_version(args.json)
    elif cmd == "init":
        (root / ".forge").mkdir(parents=True, exist_ok=True); (root / "skills").mkdir(exist_ok=True)
        print("ForgeLine initialized. Next: forge agent claude|codex, then forge expand <feature>")
    elif cmd == "status":
        if not args.feature: raise SystemExit("feature required")
        state = RunStore(root, args.feature).state
        print(json.dumps({"feature": args.feature, "state": state.value, "human_gate": state in HUMAN_GATES,
                          "next": NEXT_ACTION[state].replace("<feature>", args.feature)}, indent=2))
    elif cmd == "expand":
        store = RunStore(root, args.feature); store.set_state(State.EXPANDED, "use cases drafted"); store.receipt(phase="expand", note="awaiting human signoff")
        print(f"{args.feature}: EXPANDED — human confidence gate. After signoff: forge gate architected")
    elif cmd == "adopt":
        from .adoption import adopt
        try: print(json.dumps(adopt(root, args.feature, out=Path(args.out) if args.out else None, force=args.force), indent=2))
        except FileExistsError as error: raise SystemExit(str(error))
    elif cmd == "verify-tests-ts":
        from .gates.typescript_mutants import verify_typescript_tests
        result = verify_typescript_tests(root, args.feature, Path(args.manifest) if args.manifest else None)
        print(json.dumps({"passed": result.passed, "attribution": result.attribution.to_dict()}, indent=2))
        if not result.passed: raise SystemExit(1)
    elif cmd == "challenge": _run_challenge(args, root)
    elif cmd == "qa": _run_qa(args, root)
    elif cmd == "agent":
        from .adapters import wire_agent
        print("wired:\n  " + "\n  ".join(str(command) for command in wire_agent(root, args.name)))
    elif cmd == "handoff":
        result = Orchestrator(root, args.feature).handoff_decisions(Path(args.spec))
        print(json.dumps(result or {"decision_rules": 0, "note": "no decision table"}, indent=2))
    elif cmd in {"demo", "demo-learning"}:
        from importlib import import_module
        import_module(f".{cmd.replace('-', '_')}", __package__).run()
    elif cmd == "policy":
        from .learning import LearningKernel
        print(json.dumps(LearningKernel(root).policy_summary(), indent=2))
    elif cmd == "optimize-pr":
        from .pr_optimizer import optimize_pr
        print(json.dumps(optimize_pr(root, args.feature, base=args.base), indent=2))
    elif cmd == "lessons":
        from .skill_memory import lessons_for, promotable_constraints
        lesson_rows = [row for phase in ("fill", "review", "architect") for row in lessons_for(root, phase)]
        print(json.dumps({"lessons": lesson_rows, "promotable_to_constraints": promotable_constraints(root)}, indent=2))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--version": return _emit_version("--json" in argv)
    args = _parser(argv); root = Path(getattr(args, "root", "."))
    if args.cmd in {"gate", "architect", "fill", "review", "arch-gate", "verify-tests", "smoke", "ship"}:
        return _orchestrator_command(args, root)
    return _run_basic(args, root)

if __name__ == "__main__":
    main()
