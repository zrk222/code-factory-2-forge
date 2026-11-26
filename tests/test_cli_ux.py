"""FL-2: help text, feature listing, YAML errors as JSON, foreign blocked state, installer exit codes."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from forgeline.cli import main

ROOT = Path(__file__).resolve().parents[1]
FOREIGN_STATE = {  # exactly what Code Factory's app starter writes today
    "feature": "notifier", "state": "blocked", "created": "2026-11-01T00:00:00Z", "attempts": {},
    "history": [{"ts": "2026-11-01T00:00:00Z", "state": "blocked",
                 "note": "factory app starter generated; run forge verify-tests before smoke"}]}


def _forge(*argv, cwd):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    return subprocess.run([sys.executable, "-m", "forgeline.cli", *argv], cwd=cwd, env=env, capture_output=True,
                          text=True)


def test_help_describes_every_command(tmp_path):
    out = _forge("--help", cwd=tmp_path).stdout
    assert "architect" in out and "scaffold files from the approved SSAT" in out
    assert "verify-tests" in out and "prove smoke checks fail on generated stubs" in out


def test_status_without_feature_lists_features(proj, capsys):
    main(["status", "--root", str(proj)])
    assert json.loads(capsys.readouterr().out)["features"] == []
    main(["expand", "notifier", "--root", str(proj)])
    capsys.readouterr()
    main(["status", "--root", str(proj)])
    assert json.loads(capsys.readouterr().out)["features"] == [{"feature": "notifier", "state": "expanded"}]


def test_malformed_ssat_is_a_json_error_not_a_traceback(proj):
    assert _forge("expand", "notifier", "--root", ".", cwd=proj).returncode == 0
    (proj / "notifier.ssat.yaml").write_text("feature: [unclosed\n  - : :\n", encoding="utf-8")
    for command in (["gate", "architected"], ["verify-tests"]):
        done = _forge(*command, "notifier", "notifier.ssat.yaml", "--root", ".", cwd=proj)
        assert done.returncode == 1 and "Traceback" not in done.stderr, done.stderr[-500:]
        assert json.loads(done.stdout)["error"]["code"] == "E_SSAT"


def test_foreign_blocked_state_gives_a_runnable_next(proj, capsys):
    state = proj / ".forge" / "notifier" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps(FOREIGN_STATE), encoding="utf-8")
    main(["status", "notifier", "--root", str(proj)])
    status = json.loads(capsys.readouterr().out)
    assert status["blocker"]["code"] == "E_UNPROVEN_TARGET"
    assert status["next_argv"][:3] == ["forge", "expand", "notifier"]
    main(status["next_argv"][1:])
    assert json.loads(capsys.readouterr().out)["expanded"] is True
    main(["status", "notifier", "--root", str(proj)])
    assert json.loads(capsys.readouterr().out)["state"] == "expanded"


def test_blocked_run_with_receipts_cannot_restart_at_expand(proj, capsys):
    from forgeline.orchestrator import Orchestrator
    from forgeline.states import State
    o = Orchestrator(proj, "notifier")
    o.expand()
    o.store.set_state(State.BLOCKED, "something unrecognised")
    with pytest.raises(SystemExit):
        main(["expand", "notifier", "--root", str(proj)])
    assert "E_ILLEGAL_TRANSITION" in capsys.readouterr().out


@pytest.mark.skipif(os.name == "nt", reason="fake interpreter is a POSIX shell script")
def test_installer_fails_when_pip_fails(tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    python = fake / "python"
    python.write_text("#!/bin/sh\nexit 3\n")
    python.chmod(0o755)
    script = (ROOT / "install.py").read_text(encoding="utf-8")
    assert script.count('sys.executable, "-m", "pip"') == 2
    probe = tmp_path / "install_probe.py"
    probe.write_text(script.replace("sys.executable, \"-m\", \"pip\"", f"{str(python)!r}, \"-m\", \"pip\""),
                     encoding="utf-8")
    done = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True, cwd=tmp_path)
    assert done.returncode != 0 and "pip install failed" in (done.stderr + done.stdout)
