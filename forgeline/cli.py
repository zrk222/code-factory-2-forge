"""forge CLI — the state-machine driver."""
from __future__ import annotations
import argparse, hashlib, json, os, shlex, subprocess, sys
from pathlib import Path
from .states import State, IllegalTransition, blocked_gate, _NEXT_ACTION
from .run_store import RunStore

_PHASE_COMMAND = {
    "fill": "fill", "review": "review", "arch_gate": "arch-gate",
    "verify_tests": "verify-tests", "smoke_gate": "smoke", "ship": "ship",
}


_HELP = {
    "init": "scaffold the factory (.forge/ and skills/)",
    "new": "write a starter SSAT and a smoke manifest that blocks until you edit it",
    "advance": "run the next named step repeatedly until a human gate or blocker (max 8 steps)",
    "adopt": "inspect an existing repo and write a reviewable baseline",
    "status": "current state + the ONE next action (no feature: list features)",
    "expand": "draft use cases (-> human gate)",
    "gate": "approve specific SSAT bytes (gate architected <feature> <ssat>)",
    "architect": "scaffold files from the approved SSAT (--dry-run, --adopt-existing, --force)",
    "fill": "prove bodies are implemented; enter FILLED",
    "review": "judge + grumpy adversary + architecture erosion (refine loop)",
    "arch-gate": "architecture CI gate",
    "verify-tests": "prove smoke checks fail on generated stubs",
    "verify-tests-ts": "prove existing TypeScript tests fail on reviewed source mutants",
    "challenge": "write a Factory Passport challenge receipt",
    "smoke": "runtime behavior gate (--check: run the manifest without touching state)",
    "ship": "seal the feature",
    "handoff": "export an exact HSF workflow and compile command",
    "agent": "wire the entry-point skill (claude|codex|<name>)",
    "lessons": "show the skill memory + promotable constraints",
    "policy": "show the learning policy summary",
    "qa": "run scoped language-aware QA (--ssat <ssat> --strict)",
    "optimize-pr": "score the current branch as a PR",
    "demo": "the 60-second story",
    "demo-learning": "the learning-loop demo",
    "version": "print install and build provenance (--json)",
}


def _yaml_errors() -> tuple[type[BaseException], ...]:
    """Evaluated only while an exception is being matched, so commands that never fail never import yaml."""
    import yaml
    return (yaml.YAMLError,)


def _features(root):
    rows = []
    for path in sorted((root / ".forge").glob("*/state.json")):
        try:
            state = json.loads(path.read_bytes())["state"]
        except (OSError, ValueError, KeyError, TypeError):
            state = "unreadable"
        rows.append({"feature": path.parent.name, "state": state})
    return {"features": rows, **({} if rows else {"next": "forge expand <feature> --root ."})}


def _emit_outcome(result, *, passed):
    print(json.dumps(result, indent=2))
    if not passed:
        raise SystemExit(1)


def _next(argv):
    return {"next_argv": argv, "next": subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)}


def _status(root, feature):
    if not root.is_dir():
        _emit_outcome({"feature": feature, "state": "unknown_workspace",
                       "error": {"code": "E_WORKSPACE_NOT_FOUND", "message": str(root)}}, passed=False)
    store = RunStore(root, feature, create=False)
    ssat = f"{feature}.ssat.yaml"
    try:
        run = store.load()
        state = State(run["state"])
    except FileNotFoundError:
        return {"feature": feature, "state": "not_started", "human_gate": False,
                **_next(["forge", "expand", feature, "--root", str(root)])}
    latest = store.latest_receipts(("architecture_approval", *_PHASE_COMMAND), strict=True)
    approval = latest["architecture_approval"]
    if approval:
        ssat = approval["ssat_path"]
    result = {"feature": feature, "state": state.value, "human_gate": state == State.EXPANDED}
    command = _NEXT_ACTION[state].split(" (", 1)[0].split()
    if state == State.ARCHITECTED:
        try:
            current_sha = hashlib.sha256((root / ssat).read_bytes()).hexdigest()
        except OSError:
            current_sha = None
        if not approval or current_sha != approval["ssat_sha256"]:
            result["human_gate"] = True
            result["blocker"] = {"code": "E_SSAT_APPROVAL", "message": "approve the current SSAT before scaffolding"}
            command = _NEXT_ACTION[State.EXPANDED].split()
    if state == State.BLOCKED:
        failed = blocked_gate(run)
        if failed is None and not store.has_receipts():
            result["blocker"] = {"code": "E_UNPROVEN_TARGET",
                                 "message": "blocked run has no receipts (another tool wrote it); restart at expand",
                                 "next": f"forge expand {feature} --root {root}"}
            command = ["forge", "expand", feature, "--root", "."]
        elif failed is None:
            result["blocker"] = {"code": "E_UNKNOWN_BLOCKER", "message": "inspect the run's last failure"}
        elif failed in _PHASE_COMMAND:
            phase = _PHASE_COMMAND[failed]
            blocker = latest.get(failed) or {"phase": failed}
            smoke = latest.get("smoke_gate") or {}
            if phase in {"smoke", "ship"} and smoke.get("input_fingerprint") == "INVALID":
                phase = "verify-tests"
            command = ["forge", phase, feature, *([] if phase in {"smoke", "ship"} else [ssat]), "--root", "."]
            result["blocker"] = {key: blocker[key] for key in ("phase", "findings", "reason", "h")
                                 if key in blocker}
    if command[:1] == ["forge"]:
        result.update(_next([feature if arg == "<feature>" else ssat if arg == "<ssat.yaml>"
                             else str(root) if arg == "." else arg for arg in command]))
    return result


ADVANCE_MAX_STEPS = 8


def _run_captured(argv):
    """Run one forge command in-process and return (rc, parsed JSON or text)."""
    import contextlib, io
    buffer = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(buffer):
        try:
            main(argv)
        except SystemExit as exit_:
            code = exit_.code if isinstance(exit_.code, int) else (0 if exit_.code is None else 1)
    text = buffer.getvalue()
    try:
        return code, json.loads(text)
    except ValueError:
        return code, text.strip()


def _advance(root, feature, until=None):
    """Run what `forge status` names, step by step, until a human gate, a blocker, `until`, or no progress.
    Adds no authority: every command it runs is one status already printed."""
    steps = []
    for _ in range(ADVANCE_MAX_STEPS):
        status = _status(root, feature)
        if (status["state"] in {"shipped", until} or status.get("human_gate") or not status.get("next_argv")
                or status.get("blocker", {}).get("code") == "E_UNKNOWN_BLOCKER"):
            return status | {"steps": steps}, 0
        argv = status["next_argv"]
        code, output = _run_captured(argv[1:])
        steps.append({"argv": argv, "rc": code})
        after = _status(root, feature)
        if code != 0:
            return after | {"steps": steps, "failed": output}, 1
        if after["state"] == status["state"] and after.get("next_argv") == argv:
            return after | {"steps": steps, "stalled": True}, 1
    return _status(root, feature) | {"steps": steps, "max_steps": ADVANCE_MAX_STEPS}, 0


def _smoke_check(root, feature):
    """Run the smoke manifest as a plain check (for harness gates): reads no run state and writes nothing."""
    from .gates.runtime_smoke import runtime_smoke
    report = runtime_smoke(root, feature)
    _emit_outcome({"feature": feature, "checked": report.ok, "state_changed": False,
                   "results": [vars(result) for result in report.results]}, passed=report.ok)


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
    from .orchestrator import Orchestrator
    obj = Orchestrator(root, args.feature)
    approval = obj.store.latest_receipt("architecture_approval", strict=True)
    path = root / (args.ssat or (approval or {}).get("ssat_path", f"{args.feature}.ssat.yaml"))
    result = obj.review(path, material=material, reviewer=reviewer)
    _emit_outcome(result, passed=result.get("reviewed") is True)


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
        sp.add_argument("phase", choices=["architected"]); feature(); sp.add_argument("ssat", nargs="?"); root()
    elif name == "challenge":
        feature(); sp.add_argument("ssat"); root(); sp.add_argument("--out")
    elif name in {"agent", "handoff"}:
        sp.add_argument("name" if name == "agent" else "feature")
        if name == "handoff":
            sp.add_argument("spec")
            sp.add_argument("--compile", action="store_true", help="run HSF locally with reviewed goldens")
            sp.add_argument("--signing-key", help="operator Ed25519 key for --compile")
        root()
    elif name in {"lessons", "policy"}: root()
    elif name == "qa":
        feature(True); sp.add_argument("--ssat"); sp.add_argument("--repo-wide", action="store_true"); root(); sp.add_argument("--strict", action="store_true")
    elif name == "optimize-pr":
        feature(True); root(); sp.add_argument("--base", default="main")
    elif name == "version": sp.add_argument("--json", action="store_true")
    elif name == "smoke":
        feature(); root()
        sp.add_argument("--check", action="store_true", help="run the manifest only: no state change, no receipt")
    elif name == "advance":
        feature(); root(); sp.add_argument("--until", help="stop once this state is reached")
    elif name == "new":
        feature(); root()
    elif name not in {"demo", "demo-learning"}: feature(True); root()


def _parser(argv):
    parser = argparse.ArgumentParser(prog="forge", description="ForgeLine CLI")
    sub = parser.add_subparsers(required=True, dest="cmd")
    names = "init new advance adopt status expand gate architect fill review arch-gate verify-tests verify-tests-ts challenge smoke ship handoff agent lessons policy qa optimize-pr demo demo-learning version".split()
    for name in names: _command_parser(sub.add_parser(name, help=_HELP[name], description=_HELP[name]), name)
    return parser.parse_args(argv)


def _orchestrator_command(args, root):
    if args.cmd == "review": return _review_command(root, args)
    from .orchestrator import Orchestrator
    preview = args.cmd == "architect" and args.dry_run
    obj = Orchestrator(root, args.feature, create=not preview)
    ssat_arg = getattr(args, "ssat", None)
    approval = obj.store.latest_receipt("architecture_approval", strict=True)
    ssat = root / (ssat_arg or (approval or {}).get("ssat_path", f"{args.feature}.ssat.yaml"))
    calls = {
        "gate": (lambda: obj.approve_architecture(ssat), "approved"),
        "architect": (lambda: obj.architect(ssat, force=args.force, adopt_existing=args.adopt_existing,
                                            dry_run=args.dry_run), "scaffolded"),
        "fill": (lambda: obj.fill(ssat), "filled"),
        "arch-gate": (lambda: obj.arch_gate(ssat), "passed"),
        "verify-tests": (lambda: obj.verify_tests(ssat), "verified"),
        "smoke": (obj.smoke_gate, "smoked"),
        "ship": (obj.ship, "shipped"),
    }
    command, key = calls[args.cmd]
    try:
        result = command()
    except (OSError, ValueError, IllegalTransition) as error:
        result = {key: False, "error": {"code": "E_PHASE", "message": str(error)}}
    except _yaml_errors() as error:
        result = {key: False, "error": {"code": "E_SSAT", "message": f"SSAT is not valid YAML: {error}"}}
    preview = args.cmd == "architect" and args.dry_run and "error" not in result
    _emit_outcome(result, passed=result.get(key) is True or preview)


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
    elif cmd == "new":
        from .starter import new_feature
        try:
            print(json.dumps(new_feature(root, args.feature), indent=2))
        except FileExistsError as error:
            _emit_outcome({"created": [], "error": {"code": "E_EXISTS", "message": str(error)}}, passed=False)
    elif cmd == "advance":
        result, code = _advance(root, args.feature, args.until)
        _emit_outcome(result, passed=code == 0)
    elif cmd == "status":
        print(json.dumps(_status(root, args.feature) if args.feature else _features(root), indent=2))
    elif cmd == "expand":
        from .orchestrator import Orchestrator
        result = Orchestrator(root, args.feature).expand()
        _emit_outcome(result, passed=result["expanded"])
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
        if args.signing_key and not args.compile:
            _emit_outcome({"state": "blocked", "error": {"code": "E_COMPILE_REQUIRED",
                           "message": "--signing-key requires --compile"}}, passed=False)
        from .orchestrator import Orchestrator
        result = Orchestrator(root, args.feature).handoff_decisions(Path(args.spec))
        if args.compile and result["state"] == "ready_to_compile":
            command = result["next_argv"]
            if args.signing_key:
                command = [*command, "--signing-key", str(root / args.signing_key)]
            completed = subprocess.run([sys.executable, "-m", "hsf", *command[1:]],
                                       cwd=root, encoding="utf-8", capture_output=True, check=False)
            try:
                result["compile"] = json.loads(completed.stdout)
            except json.JSONDecodeError:
                result["compile"] = {"shipped": False, "error": completed.stderr.strip()}
            result["state"] = "compiled" if completed.returncode == 0 and result["compile"].get("shipped") else "blocked"
        passed = result["state"] == "compiled" if args.compile else result["state"] != "blocked"
        _emit_outcome(result, passed=passed)
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
    args = _parser(argv); root = Path(getattr(args, "root", ".")).resolve()
    try:
        if args.cmd == "smoke" and args.check:
            return _smoke_check(root, args.feature)
        if args.cmd in {"gate", "architect", "fill", "review", "arch-gate", "verify-tests", "smoke", "ship"}:
            return _orchestrator_command(args, root)
        return _run_basic(args, root)
    except (OSError, ValueError, IllegalTransition) as error:
        _emit_outcome({"state": "blocked", "error": {"code": "E_FORGE_WORKFLOW", "message": str(error)}}, passed=False)
    except _yaml_errors() as error:
        _emit_outcome({"state": "blocked", "error": {"code": "E_SSAT", "message": f"SSAT is not valid YAML: {error}"}},
                      passed=False)

if __name__ == "__main__":
    main()
