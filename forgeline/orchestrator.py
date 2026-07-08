"""The outer loop. Advances the state machine, calling SpecLine for spec/plan
governance and HSF for decision compilation when a spec carries a decision
table. Everything is a receipt; every gate failure records a skill lesson and
routes to the refine loop."""
from __future__ import annotations
import shutil, subprocess, sys
from pathlib import Path
from .states import State, can_transition, IllegalTransition, HUMAN_GATES
from .run_store import RunStore
from .ssat import load_ssat, scaffold_from_ssat, check_erosion
from .gates import judge_consistency, grumpy_review, skill_check
from .gates.qa_audit import qa_audit
from .skill_memory import record_lesson, inject_lessons_block, lessons_for
from .learning import LearningKernel

MAX_REFINE = 3

class Orchestrator:
    def __init__(self, root: Path, feature: str):
        self.root = Path(root); self.feature = feature
        self.store = RunStore(self.root, feature)

    def _advance(self, to: State, note: str = "", **receipt):
        frm = self.store.state
        if not can_transition(frm, to):
            raise IllegalTransition(f"{frm.value} -> {to.value}")
        self.store.set_state(to, note)
        self.store.receipt(transition=f"{frm.value}->{to.value}", **receipt)

    def architect(self, ssat_path: Path) -> dict:
        ssat = load_ssat(ssat_path)
        created = scaffold_from_ssat(ssat, self.root)
        self._advance(State.SCAFFOLDED, "scaffold from SSAT",
                      modules=len(ssat.get("modules", [])), files=[str(c) for c in created])
        return {"scaffolded": [str(c) for c in created]}

    def review(self, ssat_path: Path) -> dict:
        """Judge + grumpy adversary + arch erosion + deep QA audit, with a
        recursive learning kernel and escalating refine loop."""
        ssat = load_ssat(ssat_path)
        attempt = self.store.bump_attempt("review")
        kernel = LearningKernel(self.root)
        j_ok, j = judge_consistency(ssat, self.root)
        g_ok, g = grumpy_review(self.root)
        erosion = check_erosion(ssat, self.root)
        qa = qa_audit(self.root)                       # STRICTER: quantitative QA grade
        all_findings = j + g + [f"{v.code} {v.message}" for v in erosion] + qa.findings
        all_ok = j_ok and g_ok and not erosion and qa.passed

        # recursive learning: measure whether active constraints caught failures
        codes = [f.split()[0].split("[")[0] for f in all_findings]
        prevented = kernel.enforce(codes)

        self.store.receipt(phase="review", attempt=attempt, judge_ok=j_ok,
                           grumpy_ok=g_ok, erosion=len(erosion),
                           qa_grade=qa.grade, qa_metrics=qa.metrics,
                           active_constraints_fired=list(prevented.keys()),
                           findings=all_findings)
        if all_ok:
            self._advance(State.REVIEWED, f"review pass (attempt {attempt}, QA={qa.grade})")
            return {"reviewed": True, "attempt": attempt, "qa_grade": qa.grade, "qa_metrics": qa.metrics}

        # record lessons, then PROMOTE recurring ones into active policy (recursion)
        for f in all_findings:
            code = f.split()[0].split("[")[0]
            record_lesson(self.root, phase="fill", failure_code=code, fix=f, feature=self.feature)
        promoted = kernel.promote(lessons_for(self.root, "fill"))
        kernel.audit_effectiveness()

        if self.store.state != State.BLOCKED:
            self._advance(State.BLOCKED, f"review failed (attempt {attempt}, QA={qa.grade})")

        # ESCALATION: refine loop gets stricter each attempt
        escalation = ("normal" if attempt == 1 else
                      "elevated: fix ALL findings, not just blockers" if attempt == 2 else
                      "final: exhausted — human review required" )
        result = {"reviewed": False, "attempt": attempt, "qa_grade": qa.grade,
                  "qa_metrics": qa.metrics, "findings": all_findings,
                  "newly_promoted_constraints": promoted, "escalation": escalation,
                  "lessons_for_next": inject_lessons_block(self.root, "fill")}
        if attempt >= MAX_REFINE:
            result["exhausted"] = True
        return result

    def arch_gate(self, ssat_path: Path) -> dict:
        ssat = load_ssat(ssat_path)
        erosion = check_erosion(ssat, self.root)
        s_ok, s = skill_check(self.root, self.feature)
        if erosion or not s_ok:
            self.store.receipt(phase="arch_gate", passed=False,
                               erosion=[f"{v.code} {v.message}" for v in erosion], skill=s)
            if self.store.state != State.BLOCKED:
                self._advance(State.BLOCKED, "arch gate failed")
            return {"passed": False, "erosion": [f"{v.code} {v.message}" for v in erosion] + s}
        # must currently be REVIEWED to legally gate
        if self.store.state == State.BLOCKED:
            self.store.set_state(State.REVIEWED, "recovered for arch gate")
        kernel = LearningKernel(self.root)
        eff = kernel.audit_effectiveness()
        self._advance(State.ARCH_GATED, "architecture CI gate passed")
        self.store.receipt(phase="arch_gate", passed=True, learning=eff,
                           active_policy=kernel.policy_summary())
        return {"passed": True, "learning": eff}

    def handoff_decisions(self, spec_path: Path) -> dict | None:
        """If SpecLine + HSF are importable and the spec has a decision table,
        compile decisions through the factory. Best-effort seam."""
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parents[2]/"specline"))
            from specline.spec_lint import decision_rows  # type: ignore
        except Exception:
            return None
        rows = decision_rows(spec_path)
        if not rows:
            return None
        self.store.receipt(phase="handoff", decision_rules=len(rows),
                           note="decision table present — route to HSF compile")
        return {"decision_rules": len(rows), "next": "hsf compile"}

    def smoke_gate(self) -> dict:
        """Runtime behavior verification — the behavior-by-inspection gate.
        Runs the built artifact against declared behavioral checks (smoke/<feature>.json)
        in isolated subprocesses. Blocks ship on any runtime failure. This is the
        right-sized version of a per-PR preview deployment: verify the thing RUNS
        and behaves, not just that it type-checks and honors the spec."""
        from .gates.runtime_smoke import runtime_smoke, smoke_report_lines
        # must be ARCH_GATED to legally smoke
        if self.store.state == State.SHIPPED:
            return {"smoked": True, "note": "already shipped"}
        if self.store.state != State.ARCH_GATED:
            if self.store.state == State.SMOKED:
                return {"smoked": True, "note": "already smoked"}
            self.store.set_state(State.ARCH_GATED, "recovered for smoke gate")
        rep = runtime_smoke(self.root, self.feature)
        lines = smoke_report_lines(rep)
        if not rep.ok:
            self.store.receipt(phase="smoke_gate", smoked=False,
                               manifest_found=rep.manifest_found,
                               failures=[r.name for r in rep.failures],
                               report=lines)
            # a runtime failure is a real defect -> block; refine loop owns recovery
            self._advance(State.BLOCKED, "runtime smoke gate failed")
            return {"smoked": False,
                    "reason": ("no runtime behavior verified" if not rep.manifest_found
                               else "runtime behavioral check(s) failed"),
                    "failures": [f"{r.name}: {r.reason}" for r in rep.failures]}
        self._advance(State.SMOKED, "runtime behavior verified")
        self.store.receipt(phase="smoke_gate", smoked=True,
                           checks=len(rep.results), report=lines)
        return {"smoked": True, "checks": len(rep.results)}

    def ship(self, verify_intent: bool = True) -> dict:
        # ship now requires runtime behavior to have been verified
        if self.store.state == State.ARCH_GATED:
            return {"shipped": False, "reason": "runtime smoke gate not run — "
                    "call smoke_gate() before ship (behavior must be verified)."}
        trace = None
        if verify_intent:
            from .intent_thread import verify_against_intent
            trace = verify_against_intent(self.root, self.feature, self.root)
            if trace.envelope_found and not trace.traceable:
                # PRD->production gap: shipped code doesn't honor sealed intent
                self.store.receipt(phase="ship", shipped=False,
                                   intent_hash=trace.intent_hash,
                                   unverified_assumptions=trace.unverified_assumptions,
                                   findings=trace.findings)
                if self.store.state != State.BLOCKED:
                    self._advance(State.BLOCKED, "intent-traceability gap")
                return {"shipped": False, "reason": "intent not honored by code",
                        "unverified_assumptions": trace.unverified_assumptions,
                        "findings": trace.findings}
        self._advance(State.SHIPPED, "all gates green")
        self.store.receipt(phase="ship", shipped=True,
                           intent_hash=(trace.intent_hash if trace else None),
                           obligations=f"{trace.obligations_met}/{trace.obligations_total}" if trace else None)
        return {"shipped": True,
                "intent_traceable": (trace.traceable if trace else None),
                "obligations_met": (f"{trace.obligations_met}/{trace.obligations_total}" if trace else None)}
