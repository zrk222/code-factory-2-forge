"""FL-4: `forge advance` runs only what status names and stops at human gates and failures; `forge new` starters
block until edited."""
from __future__ import annotations

import json

import pytest

from conftest import fill_good, write_smoke_manifest
from forgeline.cli import main


def _cli(capsys, *argv):
    code = 0
    try:
        main(list(argv))
    except SystemExit as exit_:
        code = exit_.code if isinstance(exit_.code, int) else 1
    out = capsys.readouterr().out
    return code, json.loads(out)


def _to_filled(proj):
    from forgeline.orchestrator import Orchestrator
    from forgeline.states import State
    o = Orchestrator(proj, "notifier")
    o.store.set_state(State.EXPANDED)
    assert o.approve_architecture()["approved"]
    o.architect(proj / "notifier.ssat.yaml")
    fill_good(proj)
    assert o.fill(proj / "notifier.ssat.yaml")["filled"]
    return o


def test_advance_from_filled_runs_through_the_gates(proj, capsys):
    _to_filled(proj)
    write_smoke_manifest(proj)
    code, result = _cli(capsys, "advance", "notifier", "--root", str(proj))
    phases = [step["argv"][1] for step in result["steps"]]
    assert phases[:4] == ["review", "arch-gate", "verify-tests", "smoke"]
    assert phases == ["review", "arch-gate", "verify-tests", "smoke", "ship"]
    assert code == 0 and result["state"] == "shipped"


def test_advance_until_stops_early(proj, capsys):
    _to_filled(proj)
    write_smoke_manifest(proj)
    code, result = _cli(capsys, "advance", "notifier", "--root", str(proj), "--until", "arch_gated")
    assert code == 0 and result["state"] == "arch_gated"


def test_advance_stops_at_the_human_gate(proj, capsys):
    code, result = _cli(capsys, "advance", "notifier", "--root", str(proj))
    assert code == 0 and result["state"] == "expanded" and result["human_gate"] is True
    assert [step["argv"][1] for step in result["steps"]] == ["expand"]
    code, again = _cli(capsys, "advance", "notifier", "--root", str(proj))
    assert code == 0 and again["steps"] == []


def test_advance_stops_on_a_failing_gate(proj, capsys):
    _to_filled(proj)
    write_smoke_manifest(proj, passing=False)
    code, result = _cli(capsys, "advance", "notifier", "--root", str(proj))
    assert code == 1 and "failed" in result


def test_new_starter_runs_expand_and_dry_run(tmp_path, capsys):
    code, created = _cli(capsys, "new", "demo", "--root", str(tmp_path))
    assert code == 0 and created["created"] == ["demo.ssat.yaml", "smoke/demo.json"]
    assert _cli(capsys, "expand", "demo", "--root", str(tmp_path))[0] == 0
    code, preview = _cli(capsys, "architect", "demo", "demo.ssat.yaml", "--root", str(tmp_path), "--dry-run")
    assert code == 0 and "error" not in preview
    code, again = _cli(capsys, "new", "demo", "--root", str(tmp_path))
    assert code == 1 and again["error"]["code"] == "E_EXISTS"


def test_untouched_starter_blocks_verify_tests(tmp_path):
    from forgeline.gates.reverse_classical import verify_tests
    from forgeline.starter import new_feature
    new_feature(tmp_path, "demo")
    gate = verify_tests(tmp_path, "demo", tmp_path / "demo.ssat.yaml")
    assert gate.passed is False
    assert "untouched `forge new` starter" in gate.attribution.units[0].evidence


def test_feature_names_must_be_package_names(tmp_path):
    from forgeline.starter import new_feature
    with pytest.raises(ValueError):
        new_feature(tmp_path, "my-feature")


def test_smoke_check_runs_before_tests_verified_and_writes_nothing(proj, capsys):
    _to_filled(proj)
    write_smoke_manifest(proj)
    state = proj / ".forge" / "notifier" / "state.json"
    snapshot = lambda: {p.name: p.read_bytes() for p in (proj / ".forge" / "notifier").iterdir() if p.is_file()}  # noqa: E731
    receipts = snapshot()
    before = state.read_bytes()
    code, result = _cli(capsys, "smoke", "notifier", "--root", str(proj), "--check")
    assert code == 0 and result["checked"] is True and result["state_changed"] is False
    assert state.read_bytes() == before and snapshot() == receipts
    write_smoke_manifest(proj, passing=False)
    code, result = _cli(capsys, "smoke", "notifier", "--root", str(proj), "--check")
    assert code == 1 and result["checked"] is False and state.read_bytes() == before
