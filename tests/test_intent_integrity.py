import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from forgeline.intent_thread import load_envelope, verify_against_intent


def seal(**changes):
    value = {
        "schema": "specline.intent-envelope.v1", "coherence_score": 90,
        "assumptions": ["Authenticated users may access only their own records."],
        **changes,
    }
    value["sealed_hash"] = hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    return value


def write(root, value):
    directory = root / "envelopes"
    directory.mkdir(exist_ok=True)
    path = directory / "demo.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_valid_seal_is_integrity_not_executable_acceptance(tmp_path, monkeypatch):
    value = seal()
    write(tmp_path, value)
    (tmp_path / "code.py").write_text("# auth identity login token session\n")
    monkeypatch.setattr(Path, "rglob", lambda *args: pytest.fail("keyword source walk is forbidden"))
    result = verify_against_intent(tmp_path, "demo", tmp_path)
    assert result.envelope_valid and result.intent_hash == value["sealed_hash"]
    assert result.obligations_total == 1 and result.obligations_met == 0
    assert not result.evidence_verified and not result.traceable
    assert result.unverified_assumptions == value["assumptions"]
    assert any("IT_ACCEPTANCE_EVIDENCE_REQUIRED" in item for item in result.findings)


@pytest.mark.parametrize("change", [
    {"sealed_hash": "forged"}, {"coherence_score": 99},
    {"assumptions": ["different requirement"]}, {"extra": "unsealed metadata"},
])
def test_changed_envelope_content_rejects_previous_seal(tmp_path, change):
    value = seal()
    value.update(change)
    write(tmp_path, value)
    result = verify_against_intent(tmp_path, "demo", tmp_path)
    assert result.envelope_found and not result.envelope_valid and not result.traceable
    assert any("IT_ENVELOPE_HASH" in item for item in result.findings)


@pytest.mark.parametrize("change", [
    {"schema": "unknown"}, {"assumptions": []}, {"assumptions": [""]},
    {"assumptions": [5]}, {"assumptions": "auth"},
])
def test_unsupported_or_empty_obligations_cannot_pass(tmp_path, change):
    write(tmp_path, seal(**change))
    result = verify_against_intent(tmp_path, "demo", tmp_path)
    assert result.envelope_found and result.unverified_assumptions
    assert not result.envelope_valid and not result.traceable


@pytest.mark.parametrize("raw", [
    "{", "[]", "null", '{"schema": "a", "schema": "b"}',
    '{"x": NaN}', '"' + "x" * 1_048_576 + '"',
], ids=["broken-json", "array", "null", "duplicate-key", "nan", "oversized"])
def test_invalid_envelope_returns_non_traceable_finding(tmp_path, raw):
    path = write(tmp_path, {})
    path.write_text(raw)
    result = verify_against_intent(tmp_path, "demo", tmp_path)
    assert result.envelope_found and not result.traceable
    assert any("IT_INVALID_ENVELOPE" in item for item in result.findings)
    assert str(tmp_path) not in json.dumps(result.findings)


def test_sibling_envelope_is_not_borrowed(tmp_path):
    sibling = tmp_path / "specline"
    sibling.mkdir()
    write(sibling, seal())
    project = tmp_path / "project"
    project.mkdir()
    assert load_envelope(project, "demo") is None


@pytest.mark.parametrize("feature", ["../demo", "/demo", r"C:\demo", ""])
def test_unsafe_feature_returns_invalid_finding(tmp_path, feature):
    result = verify_against_intent(tmp_path, feature, tmp_path)
    assert result.envelope_found and not result.traceable
    assert any("IT_INVALID_ENVELOPE" in item for item in result.findings)


def test_escaping_envelope_link_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = write(outside, seal())
    project = tmp_path / "project"
    (project / "envelopes").mkdir(parents=True)
    try:
        (project / "envelopes/demo.json").symlink_to(target)
    except OSError as exc:
        pytest.skip(f"host does not permit symlinks: {exc}")
    result = verify_against_intent(project, "demo", project)
    assert result.envelope_found and not result.traceable
    assert any("IT_INVALID_ENVELOPE" in item for item in result.findings)


def test_ship_attribution_records_trace_failure_not_empty_list_success(proj, monkeypatch):
    from forgeline.orchestrator import Orchestrator
    from forgeline.states import State
    value = SimpleNamespace(
        envelope_found=True, traceable=False, unverified_assumptions=[],
        findings=["invalid intent"], intent_hash="", obligations_met=0, obligations_total=0,
    )
    monkeypatch.setattr("forgeline.intent_thread.verify_against_intent", lambda *args: value)
    orchestrator = Orchestrator(proj, "notifier")
    orchestrator.store.set_state(State.SMOKED)
    blocked, _, attribution = orchestrator._ship_intent(True)
    assert blocked["shipped"] is False
    assert orchestrator.store.state == State.BLOCKED
    assert attribution["n_passed"] == 0 and attribution["n_checked"] == 1


def test_link_swap_at_open_cannot_read_external_intent(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    path = write(project, seal())
    target = tmp_path / "outside.json"
    target.write_text(json.dumps(seal(assumptions=["PRIVATE_EXTERNAL_REQUIREMENT"])))
    probe = tmp_path / "probe"
    try:
        probe.symlink_to(target)
        probe.unlink()
    except OSError as exc:
        pytest.skip(f"host does not permit symlinks: {exc}")

    def swap():
        path.unlink()
        path.symlink_to(target)

    if os.name == "nt":
        original = Path.open

        def racing_open(candidate, *args, **kwargs):
            if candidate == path:
                swap()
            return original(candidate, *args, **kwargs)

        monkeypatch.setattr(Path, "open", racing_open)
    else:
        original = os.open

        def racing_open(candidate, *args, **kwargs):
            if candidate == path.name and kwargs.get("dir_fd") is not None:
                swap()
            return original(candidate, *args, **kwargs)

        monkeypatch.setattr(os, "open", racing_open)
    result = verify_against_intent(project, "demo", project)
    assert not result.envelope_valid and not result.traceable
    assert "PRIVATE_EXTERNAL_REQUIREMENT" not in str(result)
    assert any("IT_INVALID_ENVELOPE" in item for item in result.findings)
