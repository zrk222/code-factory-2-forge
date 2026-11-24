"""The outer loop. Advances the state machine, calling SpecLine for spec/plan
governance and HSF for decision compilation when a spec carries a decision
table. Everything is a receipt; every gate failure records a skill lesson and
routes to the refine loop."""
from __future__ import annotations
import hashlib, json, shutil, sys, os
from io import BytesIO, TextIOWrapper
from pathlib import Path
from tempfile import TemporaryDirectory
import yaml
from .states import State, can_transition, IllegalTransition
from .run_store import RunStore, _atomic_write
from .ssat import ScaffoldError, parse_ssat, scaffold_from_ssat, check_erosion
from .gates import judge_consistency, grumpy_review, skill_check
from .gates.judge import build_jev_review_packet, jev_snapshot, valid_jev_advisory
from .gates.qa_audit import qa_audit
from .source_scope import declared_paths
from .skill_memory import record_lesson, inject_lessons_block, lessons_for
from .learning import LearningKernel
from .attribution import Attribution, FailureClass, UnitResult
from . import __version__

MAX_REFINE = 3


class ContractApprovalError(ValueError):
    pass

_NEXT_ACTION = {
    State.INTENT: "forge expand <feature> --root .",
    State.EXPANDED: "forge gate architected <feature> <ssat.yaml> --root .",
    State.ARCHITECTED: "forge architect <feature> <ssat.yaml> --root . (add --adopt-existing for implemented targets)",
    State.SCAFFOLDED: "forge fill <feature> <ssat.yaml> --root .",
    State.FILLED: "forge review <feature> <ssat.yaml> --root .",
    State.REVIEWED: "forge arch-gate <feature> <ssat.yaml> --root .",
    State.ARCH_GATED: "forge verify-tests <feature> <ssat.yaml> --root .",
    State.TESTS_VERIFIED: "forge smoke <feature> --root .",
    State.SMOKED: "forge ship <feature> --root .",
    State.BLOCKED: "inspect the failed receipt, repair it, then rerun its gate",
    State.SHIPPED: "no further action; feature is shipped",
}


def _gate_attribution(stage: str, checks: list[tuple[str, bool, str, FailureClass]]) -> dict:
    units = [
        UnitResult(
            unit=f"{stage}:{name}",
            stage=stage,
            passed=passed,
            evidence=evidence,
            failure_class=None if passed else failure_class,
        )
        for name, passed, evidence, failure_class in checks
    ]
    return Attribution(stage, len(units), sum(unit.passed for unit in units), units).to_dict()


def _judge_failure_class(findings: list[str]) -> FailureClass:
    """Report the earliest causal class instead of calling every failure a stub."""
    prefixes = (
        ("J_STUB", FailureClass.STUB_UNFILLED),
        ("J_PARSER_UNSUPPORTED", FailureClass.PARSER_UNSUPPORTED),
        ("J_SYNTAX", FailureClass.SYNTAX_ERROR),
        ("J_ARCH E_INVARIANT", FailureClass.INVARIANT_VIOLATION),
        ("J_ARCH", FailureClass.SIGNATURE_DRIFT),
    )
    for prefix, failure_class in prefixes:
        if any(finding.startswith(prefix) for finding in findings):
            return failure_class
    return FailureClass.INCONSISTENT_LOGIC


def _verify_tests_fingerprint(root: Path, feature: str, ssat_path: Path) -> dict:
    """Hash every verified input so stale reverse-classical receipts cannot pass."""
    from .source_scope import iter_source_files
    from .gates.runtime_smoke import _load_manifest, _manifest_paths
    root = Path(root).resolve()
    ssat_path = Path(ssat_path).resolve()
    ssat_relative = ssat_path.relative_to(root).as_posix()
    if not ssat_path.is_file():
        raise ValueError("SSAT path is not a regular file")
    ssat_bytes = ssat_path.read_bytes()
    ssat = parse_ssat(ssat_bytes)
    hashes = {ssat_path: hashlib.sha256(ssat_bytes).hexdigest()}
    components: dict[str, str] = {"ssat": hashes[ssat_path], "ssat_path": ssat_relative}

    def fingerprint(path: Path, *, optional: bool = False) -> str:
        if path not in hashes:
            hashes[path] = ("MISSING" if optional and not path.exists()
                            else hashlib.sha256(path.read_bytes()).hexdigest())
        if not optional and hashes[path] == "MISSING":
            raise ValueError("test source changed during fingerprinting")
        return hashes[path]

    manifests = {path: path.read_bytes() if path.exists() else None
                 for path in dict.fromkeys(_manifest_paths(root, feature))}
    for path, raw in manifests.items():
        hashes[path] = hashlib.sha256(raw).hexdigest() if raw is not None else "MISSING"
    for path in declared_paths(root, ssat):
        key = f"source:{path.relative_to(root).as_posix()}"
        components[key] = fingerprint(path, optional=True)
    for path in manifests:
        components[f"smoke:{path.relative_to(root).as_posix()}"] = hashes[path]
    for path in iter_source_files(root):
        if path.name.startswith("test_") or path.parent.name in {"tests", "test", "__tests__"} or ".test." in path.name or ".spec." in path.name:
            components[f"test:{path.relative_to(root).as_posix()}"] = fingerprint(path)
    components["command"] = "forge verify-tests"
    components["tool_version"] = __version__

    def captured_manifest_text(path: Path) -> str | None:
        raw = manifests[path]
        if raw is None:
            return None
        with TextIOWrapper(BytesIO(raw)) as reader:
            return reader.read()

    try:
        checks = _load_manifest(root, feature, read_text=captured_manifest_text)
    except (OSError, ValueError, TypeError, KeyError):
        checks = None
    shell = os.environ.get("COMSPEC", "cmd.exe") if os.name == "nt" else "/bin/sh"
    executable = Path(sys.executable).resolve()
    shell_path = Path(shutil.which(shell) or shell).resolve()
    from .gates import reverse_classical, runtime_smoke
    gate_paths = (reverse_classical.__file__, runtime_smoke.__file__)
    if any(path is None for path in gate_paths):
        raise ValueError("verification gate provenance is unavailable")
    context_parts = [
        *(hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in gate_paths if path is not None),
        [hashlib.sha256(executable.read_bytes()).hexdigest(), str(executable), sys.version],
        [hashlib.sha256(shell_path.read_bytes()).hexdigest(), str(shell_path)],
        [sys.platform, str(root)],
        hashlib.sha256(json.dumps(sorted(os.environ.items())).encode()).hexdigest(),
    ]
    context = hashlib.sha256(json.dumps(context_parts).encode()).hexdigest()
    components["execution_context"] = context
    canonical = json.dumps(components, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"sha256": hashlib.sha256(canonical).hexdigest(), "components": components,
            "python_only": bool(checks) and all(check.kind == "python" for check in (checks or []))}


def _verified_inputs(store: RunStore, root: Path, feature: str) -> tuple[dict, dict]:
    receipt = store.latest_receipt("verify_tests", strict=True)
    return _verified_inputs_from_receipt(receipt, root, feature)


def _verified_inputs_from_receipt(
    receipt: dict | None, root: Path, feature: str
) -> tuple[dict, dict]:
    if not receipt or receipt.get("verified") is not True:
        raise ValueError("missing verified test receipt")
    relative = receipt.get("input_components", {}).get("ssat_path")
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValueError("invalid SSAT receipt path")
    current = _verify_tests_fingerprint(root, feature, root / relative)
    if current["sha256"] != receipt.get("input_fingerprint"):
        raise ValueError("verify-tests evidence is stale")
    return receipt, current


def _smoke_receipt_matches_record(
    smoke: dict | None, verified: dict, inputs: dict
) -> bool:
    return bool(smoke and smoke.get("smoked") is True
                and smoke.get("input_fingerprint") == inputs["sha256"]
                and smoke.get("ts", "") >= verified.get("ts", ""))


def _same_snapshot(before: dict, after: dict) -> bool:
    return before.get("sha256") == after.get("sha256")


def _smoke_response(report, lines: list[str], attribution: dict) -> dict:
    gate = report.gate_result
    if gate.passed:
        return {"smoked": True, "checks": len(report.results), "attribution": attribution}
    reason = (report.failures[0].reason if report.failures and report.failures[0].name == "evidence"
              else "no runtime behavior verified" if not report.manifest_found
              else "runtime behavioral check(s) failed")
    return {"smoked": False, "reason": reason,
            "failures": [f"{failure.name}: {failure.reason}" for failure in report.failures],
            "attribution": attribution}


def _verify_cache(store: RunStore, state: State, fingerprint: dict) -> tuple[dict | None, dict | None]:
    if state not in {State.SHIPPED, State.TESTS_VERIFIED, State.SMOKED}:
        return None, None
    previous = store.latest_receipt("verify_tests", strict=True)
    current = (fingerprint["python_only"] and previous
               and previous.get("verified") is True
               and previous.get("input_fingerprint") == fingerprint["sha256"])
    if current:
        note = "already shipped" if state == State.SHIPPED else "already verified"
        return {"verified": True, "note": note, "input_fingerprint": fingerprint["sha256"]}, previous
    if state == State.SHIPPED:
        return {"verified": False,
                "reason": "shipped artifact has stale verify-tests evidence; open a new feature run",
                "input_fingerprint": fingerprint["sha256"],
                "previous_fingerprint": previous.get("input_fingerprint") if previous else None}, previous
    return None, previous


def _smoke_execution(store: RunStore, root: Path, feature: str, inputs: dict):
    from .gates.runtime_smoke import runtime_smoke, SmokeReport
    try:
        report = runtime_smoke(root, feature)
        _, after = _verified_inputs(store, root.resolve(), feature)
        if not _same_snapshot(inputs, after):
            report = SmokeReport()
            report.add("evidence", False, "smoke inputs changed during execution")
    except Exception as error:
        report = SmokeReport()
        report.add("evidence", False, f"smoke evidence invalid: {error}")
    return report


class Orchestrator:
    def __init__(self, root: Path, feature: str, *, create=True):
        self.root = Path(root); self.feature = feature
        self.store = RunStore(self.root, feature, create=create)

    def _advance(self, to: State, note: str = "", **receipt):
        current = self.store.state
        if not can_transition(current, to):
            raise IllegalTransition(f"{current.value} -> {to.value}")
        self.store.transition(current, to, note, **receipt)

    def expand(self) -> dict:
        current = self.store.state
        if current == State.EXPANDED:
            return {"expanded": True, "state": current.value, "reused": True}
        if current != State.INTENT:
            return {"expanded": False, "state": current.value,
                    "error": {"code": "E_ILLEGAL_TRANSITION", "message": "use status to resume this run"}}
        try:
            self.store.transition(State.INTENT, State.EXPANDED, "use cases drafted", phase="expand")
        except IllegalTransition:
            if self.store.state != State.EXPANDED:
                raise
            return {"expanded": True, "state": State.EXPANDED.value, "reused": True}
        return {"expanded": True, "state": State.EXPANDED.value}

    def _contract(self, ssat_path):
        path = (self.root / (ssat_path or f"{self.feature}.ssat.yaml")).resolve()
        relative = path.relative_to(self.root.resolve()).as_posix()
        raw = path.read_bytes()
        ssat = parse_ssat(raw)
        if not isinstance(ssat, dict):
            raise ValueError("SSAT must be a YAML mapping")
        return ssat, {"ssat_path": relative, "ssat_sha256": hashlib.sha256(raw).hexdigest()}

    def _require_approved_binding(self, binding):
        approved = self.store.latest_receipt("architecture_approval", strict=True)
        if approved and any(approved.get(key) != value for key, value in binding.items()):
            raise ContractApprovalError("SSAT differs from the reviewed architecture")

    def _approved_contract(self, ssat_path):
        ssat, binding = self._contract(ssat_path)
        self._require_approved_binding(binding)
        return ssat

    def approve_architecture(self, ssat_path: Path | None = None) -> dict:
        with self.store.operation():
            return self._approve_architecture(ssat_path)

    def _approve_architecture(self, ssat_path: Path | None) -> dict:
        current = self.store.state
        if current not in {State.EXPANDED, State.ARCHITECTED}:
            return {"approved": False, "reason": f"expected expanded or architected, got {current.value}"}
        try:
            _, binding = self._contract(ssat_path)
        except (OSError, ValueError, yaml.YAMLError) as error:
            return {"approved": False, "error": {"code": "E_SSAT", "message": str(error)}}
        existing = self.store.latest_receipt("architecture_approval", strict=True)
        if current == State.ARCHITECTED and existing and all(existing.get(k) == v for k, v in binding.items()):
            return {"approved": True, "state": current.value, "reused": True, **binding}
        self.store._transition(current, State.ARCHITECTED, "architecture approved by CLI operator",
                               phase="architecture_approval", approver="human", **binding)
        return {"approved": True, "state": State.ARCHITECTED.value, **binding}

    def fill(self, ssat_path: Path) -> dict:
        """Verify implemented bodies and advance the previously unreachable FILLED state."""
        if self.store.state not in {State.SCAFFOLDED, State.BLOCKED}:
            return {"filled": False, "reason": f"expected scaffolded or blocked, got {self.store.state.value}"}
        try:
            ssat = self._approved_contract(ssat_path)
        except ContractApprovalError as error:
            return {"filled": False, "error": {"code": "E_SSAT_APPROVAL", "message": str(error)}}
        ok, findings = judge_consistency(ssat, self.root)
        attr = _gate_attribution("fill", [
            ("implementation", ok, "all declared bodies implemented" if ok else " | ".join(findings),
             _judge_failure_class(findings)),
        ])
        self.store.receipt(phase="fill", passed=ok, findings=findings, attribution=attr)
        if not ok:
            if self.store.state != State.BLOCKED:
                self._advance(State.BLOCKED, "fill verification failed")
            return {"filled": False, "findings": findings, "attribution": attr}
        self._advance(State.FILLED, "implemented bodies verified", attribution=attr)
        return {"filled": True, "attribution": attr}

    def architect(
        self,
        ssat_path: Path,
        *,
        force: bool = False,
        adopt_existing: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """Preview freely, or hold the feature lock through scaffold and commit."""
        if dry_run:
            return self._architect(ssat_path, force, adopt_existing, dry_run=True)
        with self.store.operation():
            return self._architect(ssat_path, force, adopt_existing, dry_run=False)

    def _architect(self, ssat_path, force, adopt_existing, *, dry_run):
        try:
            current = self.store.state
        except FileNotFoundError:
            current = State.INTENT
        preview_before_approval = dry_run and current == State.EXPANDED
        if current not in {State.ARCHITECTED, State.BLOCKED} and not preview_before_approval:
            return {
                "scaffolded": False,
                "error": {
                    "code": "E_ILLEGAL_TRANSITION",
                    "current_state": current.value,
                    "requested_state": State.SCAFFOLDED.value,
                    "next": _NEXT_ACTION[current].replace("<feature>", self.feature),
                },
            }
        try:
            approval = self.store.latest_receipt("architecture_approval", strict=True)
            ssat, binding = self._contract(ssat_path)
        except (OSError, ValueError, yaml.YAMLError) as error:
            return {"scaffolded": False, "error": {"code": "E_SSAT", "message": f"cannot read SSAT: {error}"}}
        matches_approval = approval and all(approval.get(key) == value for key, value in binding.items())
        if not (matches_approval or preview_before_approval):
            return {"scaffolded": False, "error": {
                "code": "E_SSAT_APPROVAL", "message": "approve the current SSAT before scaffolding",
                "next_argv": ["forge", "gate", "architected", self.feature, binding["ssat_path"],
                              "--root", str(self.root.resolve())],
            }}
        ssat_sha256 = binding["ssat_sha256"]
        try:
            report = scaffold_from_ssat(
                ssat,
                self.root,
                force=force,
                adopt_existing=adopt_existing,
                dry_run=dry_run,
            )
        except ScaffoldError as error:
            return {"scaffolded": False, "error": {"code": "E_SCAFFOLD", "message": str(error)},
                    "report": error.report.to_dict()}
        result = {
            "scaffolded": not dry_run,
            "adopted_existing": adopt_existing,
            "ssat_sha256": ssat_sha256,
            "report": report.to_dict(),
        }
        if dry_run:
            return result | {"requires_approval": not bool(matches_approval)}
        self.store._transition(
            current, State.SCAFFOLDED,
            "adopt existing modules from SSAT" if adopt_existing else "scaffold from SSAT",
            phase="scaffold", ssat_path=binding["ssat_path"],
            mode="adopt_existing" if adopt_existing else "scaffold",
            ssat_sha256=ssat_sha256,
            modules=len(ssat.get("modules", [])),
            created=[item.to_dict() for item in report.created],
            adopted=[item.to_dict() for item in report.adopted],
            overwritten=[item.to_dict() for item in report.overwritten],
            skipped=[item.to_dict() for item in report.skipped],
        )
        return result

    def _run_jev_review(self, ssat_path: Path, material: dict, reviewer, deterministic: dict):
        packet = None
        profile = material.get("profile") if isinstance(material, dict) else None
        try:
            packet = build_jev_review_packet(self.root, ssat_path, material, deterministic)
            before = jev_snapshot(self.root, packet)
            if reviewer is None:
                from factoryline.jev_classifier import review_forgeline_packet
                reviewer = review_forgeline_packet
            result = reviewer(packet)
            if not valid_jev_advisory(result, packet):
                result = {"status": "JEV_REVIEW_INVALID", "authority": "none", "release_decision": False,
                          "deterministic_result": deterministic}
            elif result["status"] == "FALLBACK" and result.get("fallback_reason") != "JEV_UNAVAILABLE":
                result = {**result, "status": "JEV_REVIEW_INVALID"}
            if before != jev_snapshot(self.root, packet):
                result = {"status": "JEV_REVIEW_STALE", "authority": "none", "release_decision": False,
                          "deterministic_result": deterministic}
        except Exception as error:
            status = ("FORGE_CONTEXT_INVALID" if packet is None or str(error).startswith("FORGE_CONTEXT_INVALID") else
                      "JEV_REVIEW_STALE" if str(error).startswith("JEV_REVIEW_STALE") else "FALLBACK")
            result = {"status": status, "reason": type(error).__name__, "authority": "none",
                      "release_decision": False, "deterministic_result": deterministic}
        self.store.receipt(phase="jev_review", profile=profile,
            packet_sha256=packet.get("packet_sha256") if packet else None,
            request_sha256=result.get("request_sha256", packet.get("packet_sha256") if packet else None),
            result_sha256=hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            status=result.get("status"), advisory=result, usage=result.get("usage"),
            cost=result.get("cost_usd", result.get("cost")), latency=result.get("latency"))
        return packet, result

    def _review_success(self, attempt, qa, attribution):
        self._advance(State.REVIEWED, f"review pass (attempt {attempt}, QA={qa.grade})")
        return {"reviewed": True, "attempt": attempt, "qa_grade": qa.grade,
                "qa_metrics": qa.metrics, "attribution": attribution}

    def _review_failure(self, attempt, qa, findings, attribution):
        for finding in findings:
            code = finding.split()[0].split("[")[0]
            record_lesson(self.root, phase="fill", failure_code=code, fix=finding, feature=self.feature)
        kernel = LearningKernel(self.root)
        promoted = kernel.promote(lessons_for(self.root, "fill")); kernel.audit_effectiveness()
        if self.store.state != State.BLOCKED:
            self._advance(State.BLOCKED, f"review failed (attempt {attempt}, QA={qa.grade})")
        escalation = ("normal" if attempt == 1 else
                      "elevated: fix ALL findings, not just blockers" if attempt == 2 else
                      "final: exhausted - human review required")
        result = {"reviewed": False, "attempt": attempt, "qa_grade": qa.grade,
                  "qa_metrics": qa.metrics, "findings": findings,
                  "newly_promoted_constraints": promoted, "escalation": escalation,
                  "lessons_for_next": inject_lessons_block(self.root, "fill"),
                  "attribution": attribution}
        if attempt >= MAX_REFINE: result["exhausted"] = True
        return result

    def _finish_review(self, passed, attempt, qa, findings, attribution, material, packet, advisory):
        result = (self._review_success(attempt, qa, attribution) if passed else
                  self._review_failure(attempt, qa, findings, attribution))
        if material is not None: result.update({"jev_packet": packet, "jev_review": advisory})
        return result

    def _review_attribution(self, judge_ok, judge_findings, grumpy_ok, grumpy_findings, erosion, qa):
        return _gate_attribution("review", [
            ("judge", judge_ok, "consistent" if judge_ok else " | ".join(judge_findings),
             FailureClass.INCONSISTENT_LOGIC),
            ("adversary", grumpy_ok, "all probes resisted" if grumpy_ok else " | ".join(grumpy_findings),
             FailureClass.SECURITY_FINDING),
            ("architecture", not erosion, "no erosion" if not erosion else
             " | ".join(f"{v.code} {v.message}" for v in erosion),
             FailureClass.INVARIANT_VIOLATION if any(v.code.startswith("E_INVARIANT") for v in erosion) else FailureClass.SIGNATURE_DRIFT),
            ("qa_audit", qa.passed, f"grade={qa.grade}; metrics={qa.metrics}",
             qa.attribution.dominant_failure_class() or FailureClass.COMPLEXITY_EXCEEDED),
        ])

    def review(self, ssat_path: Path, *, material: dict | None = None, reviewer=None) -> dict:
        """Judge + grumpy adversary + arch erosion + deep QA audit, with a
        recursive learning kernel and escalating refine loop."""
        try:
            ssat = self._approved_contract(ssat_path)
        except ContractApprovalError as error:
            return {"reviewed": False, "error": {"code": "E_SSAT_APPROVAL", "message": str(error)}}
        attempt = self.store.bump_attempt("review")
        kernel = LearningKernel(self.root)
        erosion = check_erosion(ssat, self.root)
        j_ok, j = judge_consistency(ssat, self.root, erosion=erosion)
        feature_paths = declared_paths(self.root, ssat)
        g_ok, g = grumpy_review(self.root, source_paths=feature_paths)
        qa = qa_audit(self.root, source_paths=feature_paths)
        all_findings = j + g + [f"{v.code} {v.message}" for v in erosion] + qa.findings
        all_ok = j_ok and g_ok and not erosion and qa.passed
        review_attr = self._review_attribution(j_ok, j, g_ok, g, erosion, qa)

        jev_packet = jev_result = None
        if material is not None:
            deterministic = {"passed": bool(all_ok), "findings": all_findings, "judge_ok": bool(j_ok),
                             "grumpy_ok": bool(g_ok), "erosion": len(erosion), "qa_grade": qa.grade}
            jev_packet, jev_result = self._run_jev_review(ssat_path, material, reviewer, deterministic)

        # recursive learning: measure whether active constraints caught failures
        codes = [f.split()[0].split("[")[0] for f in all_findings]
        prevented = kernel.enforce(codes)

        self.store.receipt(phase="review", attempt=attempt, judge_ok=j_ok,
                           grumpy_ok=g_ok, erosion=len(erosion),
                           qa_grade=qa.grade, qa_metrics=qa.metrics,
                           active_constraints_fired=list(prevented.keys()),
                           findings=all_findings, attribution=review_attr)
        if all_ok:
            return self._finish_review(True, attempt, qa, all_findings, review_attr,
                                       material, jev_packet, jev_result)

        # record lessons, then PROMOTE recurring ones into active policy (recursion)
        return self._finish_review(False, attempt, qa, all_findings, review_attr,
                                   material, jev_packet, jev_result)

    def arch_gate(self, ssat_path: Path) -> dict:
        try:
            ssat = self._approved_contract(ssat_path)
        except ContractApprovalError as error:
            return {"passed": False, "error": {"code": "E_SSAT_APPROVAL", "message": str(error)}}
        erosion = check_erosion(ssat, self.root)
        s_ok, s = skill_check(self.root, self.feature)
        arch_attr = _gate_attribution("arch_gate", [
            ("erosion", not erosion, "no architecture erosion" if not erosion else
             " | ".join(f"{v.code} {v.message}" for v in erosion),
             FailureClass.SIGNATURE_DRIFT),
            ("skill_contract", s_ok, "skill contract valid" if s_ok else " | ".join(s),
             FailureClass.INCONSISTENT_LOGIC),
        ])
        if erosion or not s_ok:
            self.store.receipt(phase="arch_gate", passed=False,
                               erosion=[f"{v.code} {v.message}" for v in erosion], skill=s,
                               attribution=arch_attr)
            if self.store.state != State.BLOCKED:
                self._advance(State.BLOCKED, "arch gate failed")
            return {"passed": False, "erosion": [f"{v.code} {v.message}" for v in erosion] + s,
                    "attribution": arch_attr}
        # must currently be REVIEWED to legally gate
        if self.store.state == State.BLOCKED:
            self.store.set_state(State.REVIEWED, "recovered for arch gate")
        kernel = LearningKernel(self.root)
        eff = kernel.audit_effectiveness()
        self._advance(State.ARCH_GATED, "architecture CI gate passed")
        self.store.receipt(phase="arch_gate", passed=True, learning=eff,
                           active_policy=kernel.policy_summary(), attribution=arch_attr)
        return {"passed": True, "learning": eff, "attribution": arch_attr}

    def handoff_decisions(self, spec_path: Path) -> dict:
        """Export the selected SpecLine decision table as pinned HSF input."""
        try:
            from specline.handoff import handoff_to_hsf
            from specline.spec_lint import decision_rows
        except ImportError:
            return {"state": "blocked", "error": {"code": "E_SPECLINE_REQUIRED",
                                                 "message": "install code-factory-1-spec"}}
        root = self.root.resolve()
        source = (root / spec_path).resolve()
        try:
            raw = source.read_bytes()
        except OSError as error:
            return {"state": "blocked", "error": {"code": "E_SPEC", "message": str(error)}}
        # The existing SpecLine exporter reads one canonical name. Snapshot the
        # explicit input so the table count and export use identical bytes.
        with TemporaryDirectory(prefix="forge-handoff-") as temporary:
            staging = Path(temporary)
            staged_spec = staging / "specs" / f"{self.feature}.md"
            staged_spec.parent.mkdir()
            staged_spec.write_bytes(raw)
            rows = decision_rows(staged_spec)
            if not rows:
                return {"state": "no_decisions", "decision_rules": 0}
            yaml_bytes = handoff_to_hsf(staging, self.feature).read_bytes()
        output = root / "handoff"
        if output.resolve() != output:
            return {"state": "blocked", "error": {"code": "E_HANDOFF_PATH", "message": "handoff directory must be local"}}
        digest = hashlib.sha256(yaml_bytes).hexdigest()
        manifest = output / f"{self.feature}_decisions-{digest[:16]}.yaml"
        goldens = root / "goldens" / f"{self.feature}_decisions" / "cases.jsonl"
        binding = {"decision_rules": len(rows), "spec_sha256": hashlib.sha256(raw).hexdigest(),
                   "hsf_spec_sha256": digest, "hsf_spec": manifest.relative_to(root).as_posix()}
        with self.store.operation():
            output.mkdir(exist_ok=True)
            if manifest.is_symlink() or (manifest.exists() and manifest.read_bytes() != yaml_bytes):
                return {"state": "blocked", "error": {"code": "E_HANDOFF_DRIFT",
                                                       "message": "exported handoff bytes have changed"}}
            if not manifest.is_file():
                _atomic_write(manifest, yaml_bytes)
            latest = self.store.latest_receipt("handoff", strict=True)
            if not latest or any(latest.get(key) != value for key, value in binding.items()):
                self.store._append(self.store._sealed({"phase": "handoff", **binding}))
        return {
            "state": "ready_to_compile" if goldens.is_file() else "needs_goldens",
            "decision_rules": len(rows),
            "hsf_spec": str(manifest),
            "hsf_spec_sha256": digest,
            "goldens": str(goldens),
            "cwd": str(root),
            "next_argv": ["hsf", "compile", str(manifest), "--expected-spec-file-sha256", digest, "--json"],
        }

    def smoke_gate(self, *, force: bool = False) -> dict:
        """Runtime behavior verification — the behavior-by-inspection gate.
        Runs the built artifact against declared behavioral checks (smoke/<feature>.json)
        in isolated subprocesses. Blocks ship on any runtime failure. This is the
        right-sized version of a per-PR preview deployment: verify the thing RUNS
        and behaves, not just that it type-checks and honors the spec."""
        from .gates.runtime_smoke import smoke_report_lines
        run = self.store.load()
        state = State(run["state"])
        retry_smoke = (state == State.BLOCKED and run["history"]
                       and run["history"][-1]["note"] == "runtime smoke gate failed")
        if state not in {State.TESTS_VERIFIED, State.SMOKED, State.SHIPPED} and not retry_smoke:
            return {"smoked": False,
                    "reason": "tests not verified - run `forge verify-tests` first"}
        try:
            if state in {State.SMOKED, State.SHIPPED} and not force:
                receipts = self.store.latest_receipts(
                    ("verify_tests", "smoke_gate"), strict=True
                )
                verified, inputs = _verified_inputs_from_receipt(
                    receipts["verify_tests"], self.root.resolve(), self.feature
                )
                if inputs["python_only"] and _smoke_receipt_matches_record(
                    receipts["smoke_gate"], verified, inputs
                ):
                    return {"smoked": True, "note": "current smoke evidence reused"}
            else:
                verified, inputs = _verified_inputs(
                    self.store, self.root.resolve(), self.feature
                )
        except Exception as error:
            from .gates.runtime_smoke import SmokeReport
            rep = SmokeReport()
            rep.add("evidence", False, f"smoke evidence invalid: {error}")
            verified, inputs = {}, {"sha256": "INVALID"}
        else:
            rep = self._smoke_after_fresh_verify(inputs)
        lines = smoke_report_lines(rep)
        return self._record_smoke_result(rep, inputs, lines)

    def _record_smoke_result(self, rep, inputs: dict, lines: list[str]) -> dict:
        attr = rep.gate_result.attribution.to_dict()
        result = _smoke_response(rep, lines, attr)
        passed = result["smoked"]
        state = self.store.state
        receipt = dict(phase="smoke_gate", smoked=passed,
                       input_fingerprint=inputs["sha256"], manifest_found=rep.manifest_found,
                       failures=[failure.name for failure in rep.failures],
                       checks=len(rep.results), report=lines, attribution=attr, reason=result.get("reason"))
        if passed and state not in {State.SMOKED, State.SHIPPED}:
            self._advance(State.SMOKED, "runtime behavior verified", **receipt)
        elif not passed and state not in {State.BLOCKED, State.SHIPPED}:
            self._advance(State.BLOCKED, "runtime smoke gate failed", **receipt)
        else:
            self.store.receipt(**receipt)
        return result

    def _record_verify_result(self, gate, fingerprint: dict, drifted: bool,
                              preserve_state: bool = False) -> dict:
        attr = (Attribution("verify_tests", 1, 0, [UnitResult(
            "verify_tests:input_drift", "verify_tests", False,
            "verification inputs changed during reverse-classical execution",
            FailureClass.RUNTIME_CRASH,
        )]).to_dict() if drifted else gate.attribution.to_dict())
        passed = gate.passed and not drifted
        receipt = dict(phase="verify_tests", verified=passed, attribution=attr,
                       input_fingerprint=fingerprint["sha256"], input_components=fingerprint["components"])
        if not passed:
            receipt["reason"] = "verification inputs changed during execution" if drifted else None
            if self.store.state not in {State.BLOCKED, State.SHIPPED}:
                self._advance(State.BLOCKED, "reverse-classical test verification failed", **receipt)
            else:
                self.store.receipt(**receipt)
            return {"verified": False, "reason": "hollow smoke check(s) found", "attribution": attr}
        if preserve_state:
            self.store.receipt(**receipt)
        else:
            self._advance(State.TESTS_VERIFIED, "smoke checks proven non-hollow", **receipt)
        return {"verified": True, "checks": gate.attribution.n_checked, "attribution": attr,
                "input_fingerprint": fingerprint["sha256"]}

    def _run_verify_tests(self, ssat_path: Path, fingerprint: dict,
                          preserve_state: bool = False) -> dict:
        from .gates.reverse_classical import verify_tests
        gate = verify_tests(self.root, self.feature, ssat_path)
        try:
            after = _verify_tests_fingerprint(self.root, self.feature, ssat_path)
            drifted = not _same_snapshot(after, fingerprint)
        except (OSError, ValueError, TypeError):
            drifted = True
        return self._record_verify_result(gate, fingerprint, drifted, preserve_state)

    def _smoke_after_fresh_verify(self, inputs: dict):
        if inputs["python_only"]:
            return _smoke_execution(self.store, self.root, self.feature, inputs)
        ssat = self.root / inputs["components"]["ssat_path"]
        result = self._run_verify_tests(ssat, inputs, preserve_state=True)
        if result["verified"]:
            return _smoke_execution(self.store, self.root, self.feature, inputs)
        from .gates.runtime_smoke import SmokeReport
        report = SmokeReport()
        report.add("evidence", False, "fresh reverse-classical verification failed")
        return report

    def verify_tests(self, ssat_path: Path) -> dict:
        """Verify smoke checks can fail before trusting smoke results."""
        try:
            ssat_path = (self.root / ssat_path).resolve()
            fingerprint = _verify_tests_fingerprint(self.root, self.feature, ssat_path)
            components = fingerprint["components"]
            self._require_approved_binding({"ssat_path": components["ssat_path"], "ssat_sha256": components["ssat"]})
        except ContractApprovalError as error:
            return {"verified": False, "error": {"code": "E_SSAT_APPROVAL", "message": str(error)}}
        except (OSError, ValueError, TypeError) as error:
            return {"verified": False, "reason": f"invalid verification inputs: {error}"}
        cached, previous = _verify_cache(self.store, self.store.state, fingerprint)
        if cached:
            return cached
        if self.store.state in {State.TESTS_VERIFIED, State.SMOKED}:
            # Source, tests, SSAT, command, or tool version changed. Invalidate
            # downstream proof before rerunning rather than returning stale green.
            self.store.set_state(State.ARCH_GATED, "verify-tests inputs changed; stale receipt invalidated")
            self.store.receipt(phase="verify_tests_cache", verified=False,
                               reason="input fingerprint changed", input_fingerprint=fingerprint["sha256"],
                               previous_fingerprint=previous.get("input_fingerprint") if previous else None)
        if self.store.state not in {State.ARCH_GATED, State.BLOCKED}:
            return {"verified": False,
                    "reason": "architecture gate not passed - run `forge arch-gate` first"}
        return self._run_verify_tests(Path(ssat_path), fingerprint)

    def refine(self, evaluate, propose, apply, revert, max_iters: int = 6) -> dict:
        """Run deterministic localized refinement.

        Callers may propose an edit, but exact Pareto acceptance and rollback are
        controlled here. Learning state remains build-time only under `.forge/`.
        """
        from .refinement import refine
        return refine(evaluate, propose, apply, revert, self.root, max_iters=max_iters)

    def _ship_intent(self, verify_intent: bool):
        if not verify_intent:
            return None, None, None
        from .intent_thread import verify_against_intent
        trace = verify_against_intent(self.root, self.feature, self.root)
        findings = trace.findings or ["intent obligations satisfied"]
        checks = [(f"obligation_{index}", trace.traceable, finding,
                   FailureClass.INCONSISTENT_LOGIC)
                  for index, finding in enumerate(findings, 1)]
        attribution = _gate_attribution("intent_thread", checks)
        if trace.envelope_found and not trace.traceable:
            self.store.receipt(phase="ship", shipped=False, intent_hash=trace.intent_hash,
                               unverified_assumptions=trace.unverified_assumptions,
                               findings=trace.findings, attribution=attribution)
            if self.store.state not in {State.BLOCKED, State.SHIPPED}:
                self._advance(State.BLOCKED, "intent-traceability gap")
            return {"shipped": False, "reason": "intent evidence is incomplete or invalid",
                    "unverified_assumptions": trace.unverified_assumptions,
                    "findings": trace.findings}, trace, attribution
        return None, trace, attribution

    def _ship_evidence_current(self) -> bool:
        try:
            receipts = self.store.latest_receipts(
                ("verify_tests", "smoke_gate"), strict=True
            )
            verified, inputs = _verified_inputs_from_receipt(
                receipts["verify_tests"], self.root.resolve(), self.feature
            )
            current = _smoke_receipt_matches_record(
                receipts["smoke_gate"], verified, inputs
            )
        except Exception:
            current = False
        if not current:
            self.store.receipt(phase="ship", shipped=False,
                               reason="smoke evidence changed before promotion")
        return current

    def _record_ship_success(self, trace, attribution: dict) -> dict:
        evidence = None
        if trace and trace.traceable:
            from .intent_thread import load_envelope
            try:
                from specline.acceptance import validate_acceptance_envelope
                evidence = load_envelope(self.root, self.feature)
                verified = validate_acceptance_envelope(self.root, self.feature, evidence)
                if verified["state"] != "PASS" or verified["sealed_hash"] != trace.intent_hash:
                    raise ValueError("intent identity changed")
            except (ImportError, SystemExit, ValueError, OSError, KeyError, TypeError, RuntimeError):
                self.store.receipt(phase="ship", shipped=False, reason="intent evidence changed before promotion")
                return {"shipped": False, "reason": "intent evidence changed before promotion"}
        obligations = (f"{trace.obligations_met}/{trace.obligations_total}" if trace else None)
        receipt = dict(phase="ship", shipped=True, intent_hash=(trace.intent_hash if trace else None),
                       obligations=obligations, attribution=attribution)
        if self.store.state != State.SHIPPED:
            self._advance(State.SHIPPED, "all gates green", **receipt)
        else:
            self.store.receipt(**receipt)
        return {"shipped": True, "intent_evidence": evidence,
                "intent_traceable": (trace.traceable if trace else None),
                "obligations_met": obligations, "attribution": attribution}

    def ship(self, verify_intent: bool = True) -> dict:
        if self.store.state in {State.ARCH_GATED, State.TESTS_VERIFIED, State.SMOKED, State.SHIPPED}:
            smoke = self.smoke_gate(force=True)
            if not smoke.get("smoked"):
                return {"shipped": False, "reason": "fresh runtime smoke evidence required"}
        if self.store.state not in {State.SMOKED, State.SHIPPED}:
            return {"shipped": False, "reason": "runtime smoke gate not run - "
                    "call smoke_gate() before ship (behavior must be verified)."}
        rejected, trace, intent_attr = self._ship_intent(verify_intent)
        if rejected:
            return rejected
        if not self._ship_evidence_current():
            return {"shipped": False, "reason": "smoke evidence changed before promotion"}
        return self._record_ship_success(trace, intent_attr)
