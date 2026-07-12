import json
import hashlib
from pathlib import Path
import pytest
from forgeline.states import State, can_transition, IllegalTransition
from forgeline.run_store import RunStore
from forgeline.orchestrator import Orchestrator
from forgeline.ssat import load_ssat, scaffold_from_ssat, check_erosion
from conftest import fill_good, write_smoke_manifest

def test_state_machine_rejects_illegal_transitions():
    assert can_transition(State.INTENT, State.EXPANDED)
    assert not can_transition(State.INTENT, State.SHIPPED)
    assert not can_transition(State.SCAFFOLDED, State.SHIPPED)


def test_cli_reachable_architecture_and_fill_states(proj):
    o = Orchestrator(proj, "notifier")
    o.store.set_state(State.EXPANDED)
    assert o.approve_architecture()["approved"] is True
    o.architect(proj / "notifier.ssat.yaml")
    fill_good(proj)
    result = o.fill(proj / "notifier.ssat.yaml")
    assert result["filled"] is True
    assert o.store.state == State.FILLED


def test_fill_blocks_unimplemented_scaffold(proj):
    o = Orchestrator(proj, "notifier")
    o.store.set_state(State.ARCHITECTED)
    o.architect(proj / "notifier.ssat.yaml")
    result = o.fill(proj / "notifier.ssat.yaml")
    assert result["filled"] is False
    assert o.store.state == State.BLOCKED

def test_run_store_persists_state_and_receipts(proj):
    s = RunStore(proj, "notifier")
    assert s.state == State.INTENT
    s.set_state(State.EXPANDED, "test"); s.receipt(phase="x", ok=True)
    assert RunStore(proj, "notifier").state == State.EXPANDED
    assert (proj/".forge"/"notifier"/"receipts.jsonl").read_text().strip()

def test_scaffold_generates_signatures_from_ssat(proj):
    ssat = load_ssat(proj/"notifier.ssat.yaml")
    created = scaffold_from_ssat(ssat, proj)
    assert len(created) == 2
    text = (proj/"slices"/"notifier"/"formatter.py").read_text()
    assert "def format_message(event: dict) -> str:" in text and "NotImplementedError" in text

def test_arch_erosion_detects_signature_drift(proj):
    ssat = load_ssat(proj/"notifier.ssat.yaml")
    scaffold_from_ssat(ssat, proj)
    # drift: change the signature
    f = proj/"slices"/"notifier"/"formatter.py"
    f.write_text('def format_message(evt: dict, extra: int) -> str:\n    return ""\n')
    v = check_erosion(ssat, proj)
    assert any(x.code == "E_SIG_DRIFT" for x in v)

def test_arch_erosion_detects_illegal_dependency(proj):
    ssat = load_ssat(proj/"notifier.ssat.yaml")
    scaffold_from_ssat(ssat, proj)
    # formatter must NOT import sender (no such edge in SSAT)
    f = proj/"slices"/"notifier"/"formatter.py"
    f.write_text('from slices.notifier.sender import send\ndef format_message(event: dict) -> str:\n    return ""\n')
    v = check_erosion(ssat, proj)
    assert any(x.code == "E_ILLEGAL_DEP" for x in v)

def test_arch_erosion_detects_invariant_violation(proj):
    ssat = load_ssat(proj/"notifier.ssat.yaml")
    scaffold_from_ssat(ssat, proj)
    f = proj/"slices"/"notifier"/"formatter.py"
    f.write_text('def format_message(event: dict) -> str:\n    return str(eval("1+1"))\n')
    v = check_erosion(ssat, proj)
    assert any(x.code == "E_INVARIANT" for x in v)

def test_grumpy_adversary_demands_tests_and_catches_danger(proj):
    from forgeline.gates import grumpy_review
    d = proj/"slices"/"notifier"; d.mkdir(parents=True, exist_ok=True)
    (d/"formatter.py").write_text('def f():\n    return eval("2")\n')
    ok, complaints = grumpy_review(proj)
    assert not ok
    joined = " ".join(complaints)
    assert "A_EVAL" in joined and "A_NO_PROOF" in joined

def test_judge_catches_unfilled_stubs(proj):
    from forgeline.gates import judge_consistency
    ssat = load_ssat(proj/"notifier.ssat.yaml")
    scaffold_from_ssat(ssat, proj)  # leaves NotImplementedError stubs
    ok, findings = judge_consistency(ssat, proj)
    assert not ok and any("J_STUB" in f for f in findings)

def test_full_happy_path_intent_to_ship(proj):
    o = Orchestrator(proj, "notifier")
    o.store.set_state(State.EXPANDED); o.store.set_state(State.ARCHITECTED)
    o.architect(proj/"notifier.ssat.yaml")
    assert o.store.state == State.SCAFFOLDED
    fill_good(proj)
    o.store.set_state(State.FILLED)
    r = o.review(proj/"notifier.ssat.yaml")
    assert r["reviewed"] is True and o.store.state == State.REVIEWED
    g = o.arch_gate(proj/"notifier.ssat.yaml")
    assert g["passed"] and o.store.state == State.ARCH_GATED
    write_smoke_manifest(proj)
    assert o.verify_tests(proj/"notifier.ssat.yaml")["verified"] and o.store.state == State.TESTS_VERIFIED
    assert o.smoke_gate()["smoked"] and o.store.state == State.SMOKED
    assert o.ship()["shipped"] and o.store.state == State.SHIPPED

def test_refine_loop_records_skill_lessons(proj):
    o = Orchestrator(proj, "notifier")
    o.store.set_state(State.ARCHITECTED)
    o.architect(proj/"notifier.ssat.yaml")
    o.store.set_state(State.FILLED)  # but leave stubs unfilled -> review fails
    r = o.review(proj/"notifier.ssat.yaml")
    assert r["reviewed"] is False
    lessons = (proj/"skills"/"lessons.jsonl")
    assert lessons.exists() and lessons.read_text().strip()
    assert "lessons_for_next" in r  # injected into next attempt's context

def test_skill_lessons_promote_to_constraints(proj):
    from forgeline.skill_memory import record_lesson, promotable_constraints
    for _ in range(3):
        record_lesson(proj, phase="fill", failure_code="A_EVAL", fix="never use eval", feature="x")
    promo = promotable_constraints(proj, threshold=3)
    assert any(p["failure_code"] == "A_EVAL" and p["count"] >= 3 for p in promo)

def test_handoff_detects_decision_table(proj, monkeypatch):
    # simulate a spec with a decision table; specline may or may not be importable
    spec = proj/"spec.md"
    spec.write_text("## Decision logic\n| # | if | then |\n|---|----|------|\n| 1 | x == true | APPROVED: ok |\n| 2 | else | DENIED: no |\n")
    o = Orchestrator(proj, "notifier")
    r = o.handoff_decisions(spec)
    # returns None if specline not importable, else finds 2 rules — both acceptable
    assert r is None or r["decision_rules"] == 2

def test_agent_wire_creates_entry_point_skill(proj):
    from forgeline.adapters import wire_agent
    wire_agent(proj, "claude")
    assert (proj/"CLAUDE.md").exists() and "executable contract" in (proj/"CLAUDE.md").read_text()
    assert (proj/".claude"/"skills"/"forge.md").exists()
    wire_agent(proj, "codex")
    assert (proj/"AGENTS.md").exists()


# ============ recursive learning + deep QA (v0.2 upgrades) ============
def test_qa_audit_grades_quality(proj):
    from forgeline.gates.qa_audit import qa_audit
    from forgeline.ssat import load_ssat, scaffold_from_ssat
    scaffold_from_ssat(load_ssat(proj/"notifier.ssat.yaml"), proj)
    fill_good(proj)
    r = qa_audit(proj)
    assert r.grade in ("A","B") and r.passed
    assert r.coverage_intent > 0 and r.doc_ratio > 0
    assert r.attribution.n_checked > 0

def test_qa_audit_catches_security_and_complexity(proj):
    from forgeline.gates.qa_audit import qa_audit
    d = proj/"slices"/"bad"; d.mkdir(parents=True)
    (d/"danger.py").write_text(
        "def run(x):\n"
        "    return eval(x)\n")  # eval => critical security hit
    r = qa_audit(proj)
    assert r.security_score < 100
    assert any("QA_SEC" in f for f in r.findings)
    assert not r.passed

def test_learning_kernel_promotes_recurring_lessons(proj):
    from forgeline.learning import LearningKernel
    from forgeline.skill_memory import record_lesson, lessons_for
    for _ in range(3):
        record_lesson(proj, phase="fill", failure_code="A_EVAL", fix="never eval", feature="x")
    k = LearningKernel(proj, promote_threshold=3)
    promoted = k.promote(lessons_for(proj, "fill"))
    assert "A_EVAL" in promoted
    assert "A_EVAL" in k.active_codes()

def test_learning_kernel_measures_effectiveness(proj):
    from forgeline.learning import LearningKernel
    from forgeline.skill_memory import record_lesson, lessons_for
    for _ in range(3):
        record_lesson(proj, phase="fill", failure_code="A_EVAL", fix="no eval", feature="x")
    k = LearningKernel(proj, promote_threshold=3)
    k.promote(lessons_for(proj, "fill"))
    # the constraint fires again (caught the same failure) => validated
    prevented = k.enforce(["A_EVAL"])
    assert prevented.get("A_EVAL") is True
    summary = k.policy_summary()
    assert summary["constraints"]["A_EVAL"]["prevented"] >= 1

def test_learning_kernel_puts_dead_rules_on_probation(proj):
    from forgeline.learning import LearningKernel
    from forgeline.skill_memory import record_lesson, lessons_for
    for _ in range(3):
        record_lesson(proj, phase="fill", failure_code="A_STALE", fix="x", feature="x")
    k = LearningKernel(proj, promote_threshold=3)
    k.promote(lessons_for(proj, "fill"))
    # never enforced -> audit puts it on probation (self-pruning policy)
    eff = k.audit_effectiveness()
    assert "A_STALE" in eff["probation"]

def test_review_escalates_and_promotes(proj):
    from forgeline.orchestrator import Orchestrator
    from forgeline.states import State
    o = Orchestrator(proj, "notifier")
    o.store.set_state(State.ARCHITECTED)
    o.architect(proj/"notifier.ssat.yaml")
    o.store.set_state(State.FILLED)  # stubs unfilled -> repeated failures
    r1 = o.review(proj/"notifier.ssat.yaml")
    assert r1["reviewed"] is False and "escalation" in r1
    assert r1["escalation"].startswith("normal")
    r2 = o.review(proj/"notifier.ssat.yaml")
    assert "elevated" in r2["escalation"]  # loop gets stricter

def test_review_receipt_carries_qa_grade(proj):
    from forgeline.orchestrator import Orchestrator
    from forgeline.states import State
    import json
    o = Orchestrator(proj, "notifier")
    o.store.set_state(State.ARCHITECTED); o.architect(proj/"notifier.ssat.yaml")
    fill_good(proj); o.store.set_state(State.FILLED)
    o.review(proj/"notifier.ssat.yaml")
    receipts = [json.loads(l) for l in (proj/".forge"/"notifier"/"receipts.jsonl").read_text().splitlines()]
    review_r = [r for r in receipts if r.get("phase")=="review"]
    assert review_r and "qa_grade" in review_r[0] and "qa_metrics" in review_r[0]
    assert review_r[0]["attribution"]["n_checked"] == 4


def test_arch_gate_receipt_has_attribution(proj):
    o = _to_arch_gated(proj)
    receipts = [json.loads(line) for line in
                (proj / ".forge" / "notifier" / "receipts.jsonl").read_text().splitlines()]
    gate = [item for item in receipts if item.get("phase") == "arch_gate"][-1]
    assert gate["attribution"]["n_checked"] == 2
    assert gate["attribution"]["rate"] == 1.0


# ============ Intent Thread — PRD->production traceability (v0.3) ============
def test_intent_thread_no_envelope_is_untraceable(proj):
    from forgeline.intent_thread import verify_against_intent
    r = verify_against_intent(proj, "notifier", proj)
    assert not r.envelope_found
    assert any("IT_NO_ENVELOPE" in f for f in r.findings)

def test_intent_thread_verifies_met_obligations(proj):
    import json
    from forgeline.intent_thread import verify_against_intent
    # write a sealed envelope with an auth assumption
    (proj/"envelopes").mkdir(exist_ok=True)
    (proj/"envelopes"/"notifier.json").write_text(json.dumps({
        "sealed_hash": "abc123", "coherence_score": 90,
        "assumptions": ["Assumes an identity/auth mechanism exists."]}))
    # code that DOES handle auth
    d = proj/"slices"/"notifier"; d.mkdir(parents=True, exist_ok=True)
    (d/"a.py").write_text("def login(token):\n    return authenticate(token)\ndef authenticate(t): return bool(t)\n")
    r = verify_against_intent(proj, "notifier", proj)
    assert r.envelope_found and r.obligations_met == 1 and r.traceable

def test_intent_thread_catches_unmet_obligation(proj):
    import json
    from forgeline.intent_thread import verify_against_intent
    (proj/"envelopes").mkdir(exist_ok=True)
    (proj/"envelopes"/"notifier.json").write_text(json.dumps({
        "sealed_hash": "abc123", "coherence_score": 90,
        "assumptions": ["Assumes an identity/auth mechanism exists."]}))
    d = proj/"slices"/"notifier"; d.mkdir(parents=True, exist_ok=True)
    (d/"a.py").write_text("def format_message(x):\n    return str(x)\n")  # no auth handling
    r = verify_against_intent(proj, "notifier", proj)
    assert not r.traceable and r.unverified_assumptions

def test_ship_blocks_on_intent_gap(proj):
    import json
    from forgeline.orchestrator import Orchestrator
    from forgeline.states import State
    o = Orchestrator(proj, "notifier")
    o.store.set_state(State.ARCHITECTED); o.architect(proj/"notifier.ssat.yaml")
    fill_good(proj); o.store.set_state(State.FILLED)
    o.review(proj/"notifier.ssat.yaml"); o.arch_gate(proj/"notifier.ssat.yaml")
    # now plant an envelope with an unmet obligation
    (proj/"envelopes").mkdir(exist_ok=True)
    (proj/"envelopes"/"notifier.json").write_text(json.dumps({
        "sealed_hash": "x", "coherence_score": 90,
        "assumptions": ["Assumes external dependency availability (integration referenced)."]}))
    write_smoke_manifest(proj); o.verify_tests(proj/"notifier.ssat.yaml"); o.smoke_gate()
    r = o.ship()
    assert r["shipped"] is False and r["unverified_assumptions"]

def test_ship_succeeds_when_intent_honored(proj):
    from forgeline.orchestrator import Orchestrator
    from forgeline.states import State
    o = Orchestrator(proj, "notifier")
    o.store.set_state(State.ARCHITECTED); o.architect(proj/"notifier.ssat.yaml")
    fill_good(proj); o.store.set_state(State.FILLED)
    o.review(proj/"notifier.ssat.yaml"); o.arch_gate(proj/"notifier.ssat.yaml")
    write_smoke_manifest(proj); o.verify_tests(proj/"notifier.ssat.yaml"); o.smoke_gate()
    r = o.ship()  # no envelope -> ships (traceability opt-in), or with met obligations
    assert r["shipped"] is True


# ---- runtime smoke gate ----

def _to_arch_gated(proj):
    from forgeline.orchestrator import Orchestrator
    from forgeline.states import State
    o = Orchestrator(proj, "notifier")
    o.store.set_state(State.ARCHITECTED); o.architect(proj/"notifier.ssat.yaml")
    fill_good(proj); o.store.set_state(State.FILLED)
    o.review(proj/"notifier.ssat.yaml"); o.arch_gate(proj/"notifier.ssat.yaml")
    return o

def _to_tests_verified(proj):
    o = _to_arch_gated(proj)
    write_smoke_manifest(proj)
    assert o.verify_tests(proj/"notifier.ssat.yaml")["verified"] is True
    return o

def _file_hashes(root: Path) -> dict[str, str]:
    hashes = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes

def test_smoke_gate_passes_on_green_check(proj):
    from forgeline.states import State
    o = _to_tests_verified(proj)
    r = o.smoke_gate()
    assert r["smoked"] is True and o.store.state == State.SMOKED

def test_smoke_gate_blocks_on_missing_manifest(proj):
    from forgeline.states import State
    o = _to_arch_gated(proj)
    r = o.smoke_gate()
    assert r["smoked"] is False
    assert "verify-tests" in r["reason"]
    assert o.store.state == State.ARCH_GATED

def test_smoke_gate_blocks_on_runtime_failure(proj):
    from forgeline.states import State
    o = _to_arch_gated(proj)
    write_smoke_manifest(proj, passing=False)   # asserts wrong result -> nonzero exit
    assert o.verify_tests(proj/"notifier.ssat.yaml")["verified"] is True
    r = o.smoke_gate()
    assert r["smoked"] is False and o.store.state == State.BLOCKED
    assert r["failures"]

def test_ship_requires_smoke_gate(proj):
    from forgeline.states import State
    o = _to_arch_gated(proj)
    # skip smoke, go straight to ship -> refused
    r = o.ship()
    assert r["shipped"] is False and "smoke" in r["reason"].lower()


def test_verify_tests_passes_real_behavioral_check(proj):
    from forgeline.states import State
    o = _to_arch_gated(proj)
    write_smoke_manifest(proj, passing=True)
    r = o.verify_tests(proj/"notifier.ssat.yaml")
    assert r["verified"] is True
    assert o.store.state == State.TESTS_VERIFIED
    assert r["attribution"]["rate"] == 1.0


def test_verify_tests_catches_assert_true_hollow_check(proj):
    from forgeline.states import State
    o = _to_arch_gated(proj)
    smoke = proj / "smoke"
    smoke.mkdir(exist_ok=True)
    (smoke / "notifier.json").write_text(json.dumps({"checks": [{
        "name": "assert_true",
        "kind": "python",
        "run": "assert True\nprint('OK')",
        "expect_stdout": "OK",
    }]}))
    r = o.verify_tests(proj/"notifier.ssat.yaml")
    assert r["verified"] is False
    assert o.store.state == State.BLOCKED
    assert r["attribution"]["dominant_failure_class"] == "hollow_test"


def test_verify_tests_catches_trivially_true_assertion(proj):
    o = _to_arch_gated(proj)
    smoke = proj / "smoke"
    smoke.mkdir(exist_ok=True)
    (smoke / "notifier.json").write_text(json.dumps({"checks": [{
        "name": "assert_math",
        "kind": "python",
        "run": "assert 1 == 1",
    }]}))
    r = o.verify_tests(proj/"notifier.ssat.yaml")
    assert r["verified"] is False
    assert r["attribution"]["units"][0]["failure_class"] == "hollow_test"


def test_verify_tests_honors_structural_exemption(proj):
    o = _to_arch_gated(proj)
    smoke = proj / "smoke"
    smoke.mkdir(exist_ok=True)
    (smoke / "notifier.json").write_text(json.dumps({"checks": [
        {
            "name": "formatter_behavior",
            "kind": "python",
            "run": (
                "import sys; sys.path.insert(0, '.')\n"
                "from slices.notifier.formatter import format_message\n"
                "assert format_message({'kind':'ping','text':'hi'}) == 'ping: hi'\n"
            ),
        },
        {
            "name": "module_imports",
            "kind": "python",
            "run": "import sys; sys.path.insert(0, '.')\nimport slices.notifier.formatter\n",
            "must_fail_on_stub": False,
        },
    ]}))
    r = o.verify_tests(proj/"notifier.ssat.yaml")
    assert r["verified"] is True
    evidence = [unit["evidence"] for unit in r["attribution"]["units"]]
    assert any("exempt" in item for item in evidence)


def test_verify_tests_absent_field_defaults_strict(proj):
    o = _to_arch_gated(proj)
    smoke = proj / "smoke"
    smoke.mkdir(exist_ok=True)
    (smoke / "notifier.json").write_text(json.dumps({"checks": [{
        "name": "quietly_unmarked",
        "kind": "python",
        "run": "print('OK')",
        "expect_stdout": "OK",
    }]}))
    r = o.verify_tests(proj/"notifier.ssat.yaml")
    assert r["verified"] is False
    assert r["attribution"]["dominant_failure_class"] == "hollow_test"


def test_verify_tests_all_exempt_manifest_blocks(proj):
    o = _to_arch_gated(proj)
    smoke = proj / "smoke"
    smoke.mkdir(exist_ok=True)
    (smoke / "notifier.json").write_text(json.dumps({"checks": [{
        "name": "module_imports",
        "kind": "python",
        "run": "import slices.notifier.formatter",
        "must_fail_on_stub": False,
    }]}))
    r = o.verify_tests(proj/"notifier.ssat.yaml")
    assert r["verified"] is False
    assert r["attribution"]["dominant_failure_class"] == "hollow_manifest"


def test_verify_tests_missing_and_empty_manifest_block(proj):
    o = _to_arch_gated(proj)
    missing = o.verify_tests(proj/"notifier.ssat.yaml")
    assert missing["verified"] is False
    assert missing["attribution"]["dominant_failure_class"] == "hollow_manifest"

    o.store.set_state(State.ARCH_GATED)
    smoke = proj / "smoke"
    smoke.mkdir(exist_ok=True)
    (smoke / "notifier.json").write_text(json.dumps({"checks": []}))
    empty = o.verify_tests(proj/"notifier.ssat.yaml")
    assert empty["verified"] is False
    assert empty["attribution"]["dominant_failure_class"] == "hollow_manifest"


def test_smoke_requires_tests_verified_state(proj):
    o = _to_arch_gated(proj)
    write_smoke_manifest(proj)
    r = o.smoke_gate()
    assert r["smoked"] is False
    assert "verify-tests" in r["reason"]
    assert o.store.state == State.ARCH_GATED


def test_arch_gated_to_smoked_is_illegal_transition():
    assert not can_transition(State.ARCH_GATED, State.SMOKED)


def test_verify_tests_does_not_touch_working_tree(proj):
    o = _to_arch_gated(proj)
    write_smoke_manifest(proj)
    target = proj / "slices" / "notifier"
    before = _file_hashes(target)
    assert o.verify_tests(proj/"notifier.ssat.yaml")["verified"] is True
    assert _file_hashes(target) == before


def test_materialized_stub_is_deterministic_and_identical_to_scaffold(proj, tmp_path):
    from forgeline.gates.reverse_classical import materialize_stub_root
    from forgeline.ssat import load_ssat, scaffold_from_ssat

    ssat_path = proj / "notifier.ssat.yaml"
    first = materialize_stub_root(ssat_path)
    second = materialize_stub_root(ssat_path)
    expected = tmp_path / "expected"
    scaffold_from_ssat(load_ssat(ssat_path), expected)
    try:
        assert _file_hashes(first) == _file_hashes(second)
        assert _file_hashes(first) == _file_hashes(expected)
    finally:
        import shutil
        shutil.rmtree(first, ignore_errors=True)
        shutil.rmtree(second, ignore_errors=True)


def test_adopt_existing_typescript_repo_writes_reviewable_baseline(tmp_path):
    from forgeline.adoption import adopt

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "sum.ts").write_text("export function sum(a: number, b: number): number { return a + b; }")
    (tmp_path / "package.json").write_text('{"scripts":{"test":"vitest run"}}')
    result = adopt(tmp_path, "math")
    assert result["languages"] == ["typescript"]
    assert (tmp_path / "math.adoption.ssat.yaml").exists()
    assert Path(result["typescript_manifest"]).exists()


def test_typescript_mutant_verification_requires_existing_test_to_fail(tmp_path):
    from forgeline.gates.typescript_mutants import verify_typescript_tests

    (tmp_path / "src").mkdir()
    (tmp_path / ".forge" / "math").mkdir(parents=True)
    (tmp_path / "src" / "sum.ts").write_text("export function sum(a: number, b: number): number { return a + b; }")
    manifest = {
        "mutants": [{
            "name": "sum_returns_stub", "path": "src/sum.ts",
            "replace_regex": "return a \\+ b;", "replacement": "throw new Error('FORGE_STUB');",
            "command": "python -c \"import pathlib; assert 'FORGE_STUB' not in pathlib.Path('src/sum.ts').read_text()\"",
        }]
    }
    path = tmp_path / ".forge" / "math" / "typescript-mutants.json"
    path.write_text(json.dumps(manifest))
    result = verify_typescript_tests(tmp_path, "math")
    assert result.passed is True
    assert result.attribution.n_passed == 1


def test_verify_tests_temp_root_is_cleaned_up(proj):
    import tempfile
    o = _to_arch_gated(proj)
    write_smoke_manifest(proj)
    tmp = Path(tempfile.gettempdir())
    before = {path for path in tmp.glob("forge-stub-*")}
    assert o.verify_tests(proj/"notifier.ssat.yaml")["verified"] is True
    after = {path for path in tmp.glob("forge-stub-*")}
    assert after == before


def test_hollow_test_maps_to_structural_edit():
    from forgeline.attribution import FailureClass
    from forgeline.refinement import select_edit
    assert select_edit(FailureClass.HOLLOW_TEST).edit_class == "structural"


def test_smoke_attribution_is_per_check_and_verdict_derived(proj):
    from forgeline.gates.runtime_smoke import runtime_smoke
    smoke = proj / "smoke"
    smoke.mkdir(exist_ok=True)
    (smoke / "notifier.json").write_text(json.dumps({"checks": [
        {"name": "green", "kind": "python", "run": "print('ok')", "expect_stdout": "ok"},
        {"name": "wrong", "kind": "python", "run": "print('no')", "expect_stdout": "yes"},
    ]}))
    report = runtime_smoke(proj, "notifier")
    gate = report.gate_result
    assert gate.passed is False
    assert gate.attribution.n_checked == 2 and gate.attribution.n_passed == 1
    assert gate.attribution.rate == 0.5
    assert gate.attribution.failures[0].failure_class.value == "wrong_output"
    assert "expected stdout" in gate.attribution.failures[0].evidence


def test_edit_order_pareto_and_plateau(tmp_path):
    from forgeline.attribution import FailureClass
    from forgeline.refinement import pareto_win, refine, select_edit
    assert select_edit(FailureClass.SIGNATURE_DRIFT).edit_class == "structural"
    assert pareto_win({"smoke": 1.0, "judge": 1.0},
                      {"smoke": 0.5, "judge": 1.0}, "smoke")
    assert not pareto_win({"smoke": 1.0, "judge": 0.5},
                          {"smoke": 0.5, "judge": 1.0}, "smoke")
    state = {"rates": {"smoke": 0.5}, "reverts": 0}
    result = refine(
        lambda: dict(state["rates"]),
        lambda rates: ("smoke", FailureClass.RUNTIME_TIMEOUT),
        lambda edit: b"tree-before",
        lambda snapshot: state.__setitem__("reverts", state["reverts"] + 1),
        tmp_path,
    )
    assert result["reason"] == "plateau" and result["iters"] == 2
    assert state["reverts"] == 2
    lines = (tmp_path / ".forge" / "rejection_ledger.jsonl").read_text().splitlines()
    assert len(lines) == 2
    entry = json.loads(lines[0])
    assert entry["before_rates"]["smoke"] == 0.5
    assert entry["after_rates"]["smoke"] == 0.5


def test_orchestrator_refine_reverts_tree_byte_identically(proj):
    from forgeline.attribution import FailureClass
    target = proj / "slices" / "target.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"before\n")
    before = target.read_bytes()
    orchestrator = Orchestrator(proj, "notifier")

    def apply(edit):
        snapshot = target.read_bytes()
        target.write_bytes(b"rejected\n")
        return snapshot

    result = orchestrator.refine(
        lambda: {"smoke": 0.5},
        lambda rates: ("smoke", FailureClass.RUNTIME_TIMEOUT),
        apply,
        lambda snapshot: target.write_bytes(snapshot),
    )
    assert result["reason"] == "plateau"
    assert target.read_bytes() == before


def test_optimize_pr_has_bounded_loop_and_approval_boundary(proj, monkeypatch):
    from forgeline import pr_optimizer

    monkeypatch.setattr(pr_optimizer, "_changed", lambda root, base: ["src/auth/login.py", "README.md"])
    plan = pr_optimizer.optimize_pr(proj, "notifier")
    assert plan["loop"]["max_iterations"] == 5
    assert plan["approval_required"] is True
    assert "src/auth/login.py" in plan["risky_paths"]
    assert "factory pr-pack notifier" in plan["commands"]
