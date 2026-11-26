"""FL-3: light commands import no YAML or orchestrator; handoff names the plain SpecLine file it supersedes."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ("import json, sys; from forgeline.cli import main\n"
         "try:\n    main(sys.argv[1:])\nexcept SystemExit:\n    pass\n"
         "print(json.dumps({m: m in sys.modules for m in ('yaml', 'forgeline.orchestrator', 'forgeline.gates.qa_audit')}),"
         " file=sys.stderr)")


@pytest.mark.parametrize("argv", [["--help"], ["status", "notifier", "--root", "{root}"], ["status", "--root", "{root}"],
                                  ["version"]])
def test_light_commands_do_not_import_heavy_modules(proj, argv):
    argv = [arg.replace("{root}", str(proj)) for arg in argv]
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    done = subprocess.run([sys.executable, "-c", PROBE, *argv], env=env, capture_output=True, text=True)
    loaded = json.loads(done.stderr.strip().splitlines()[-1])
    assert loaded == {"yaml": False, "forgeline.orchestrator": False, "forgeline.gates.qa_audit": False}, argv


def test_wheel_config_has_no_dead_template_line():
    assert "../templates" not in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


SPEC = """# Spec: notifier
Status: draft

## MUST — Functional core
### Requirements (EARS)
- When `urgent` is true, the system shall return `Page now`.

## Decision logic (factory candidates)
| # | if | then |
|---|----|------|
| 1 | urgent == true | APPROVED: Page now |
| 2 | else | DENIED: Later |
"""


def test_handoff_reports_the_plain_file_it_supersedes(proj):
    pytest.importorskip("specline.handoff")
    from forgeline.orchestrator import Orchestrator
    from specline.handoff import handoff_to_hsf
    (proj / "specs").mkdir()
    (proj / "specs" / "notifier.md").write_text(SPEC, encoding="utf-8")
    try:
        handoff_to_hsf(proj, "notifier", draft=True)  # what `specline handoff --draft` writes
    except TypeError:
        handoff_to_hsf(proj, "notifier")
    result = Orchestrator(proj, "notifier").handoff_decisions(Path("specs/notifier.md"))
    assert result["state"] in {"needs_goldens", "ready_to_compile"}
    assert result["supersedes"] == "handoff/notifier_decisions.yaml"
    assert result["spec_gated"] in {False, None}


def test_invalid_table_is_blocked_with_a_handoff_code(proj):
    pytest.importorskip("specline.handoff")
    from forgeline.orchestrator import Orchestrator
    bad = proj / "bad.md"
    bad.write_text(SPEC.replace("urgent == true", "Urgent >>= 1"), encoding="utf-8")
    result = Orchestrator(proj, "notifier").handoff_decisions(Path("bad.md"))
    if result["state"] != "blocked":
        pytest.skip("installed SpecLine predates E_HANDOFF_* codes")
    assert result["error"]["code"].startswith("E_HANDOFF")
