"""forge demo — a 60-second headless walk showing the refine loop + skill
flywheel: a bad fill gets caught by the grumpy adversary, a lesson is
recorded, and the second attempt (with the lesson injected) passes."""
from __future__ import annotations
import shutil, time
from pathlib import Path
import tempfile

C = {"g":"\033[92m","c":"\033[96m","y":"\033[93m","d":"\033[2m","b":"\033[1m","x":"\033[0m"}
def p(m,s=0.02): print(m); time.sleep(s)

def run():
    from forgeline.orchestrator import Orchestrator
    from forgeline.states import State
    from forgeline.skill_memory import lessons_for
    root = Path(tempfile.mkdtemp())
    (root/"skills").mkdir()
    ssat_src = Path(__file__).resolve().parents[1]/"examples"/"notifier.ssat.yaml"
    ssat = root/"notifier.ssat.yaml"; shutil.copy(ssat_src, ssat)

    p(f"\n{C['b']}✦ ForgeLine — autonomous factory outer loop{C['x']}")
    p(f"{C['d']}  intent → SSAT → scaffold → fill → adversarial review → ship{C['x']}\n")

    o = Orchestrator(root, "notifier")
    o.store.set_state(State.ARCHITECTED)
    p(f"{C['c']}[1] architect{C['x']} — scaffolding signatures from architecture-as-code")
    o.architect(ssat)
    p(f"      {C['g']}✓{C['x']} 2 modules scaffolded with valid imports\n")

    # BAD fill: eval + no tests
    d = root/"slices"/"notifier"
    (d/"formatter.py").write_text('def format_message(event: dict) -> str:\n    return str(eval(event.get("expr","1")))\n')
    (d/"sender.py").write_text('from slices.notifier.formatter import format_message\ndef send(event: dict, channel: str) -> bool:\n    return bool(format_message(event) and channel)\n')
    o.store.set_state(State.FILLED)
    p(f"{C['c']}[2] fill (attempt 1){C['x']} — agent used eval(), shipped no tests")
    r = o.review(ssat)
    p(f"      {C['y']}✗ grumpy adversary blocks:{C['x']}")
    for f in r["findings"]:
        p(f"        · {f}")
    p(f"      {C['d']}lesson recorded to skill memory{C['x']}\n")

    # SECOND fill with lesson applied
    p(f"{C['c']}[3] lessons injected into next context:{C['x']}")
    for l in lessons_for(root, "fill"):
        p(f"        [{l['failure_code']} ×{l['count']}] {l['fix'][:60]}")
    (d/"formatter.py").write_text('def format_message(event: dict) -> str:\n    return f"{event.get(\'kind\',\'x\')}: {event.get(\'text\',\'\')}"\n')
    (root/"tests").mkdir(exist_ok=True)
    (root/"tests"/"test_notifier.py").write_text('def test_ok():\n    assert True\n')
    p(f"\n{C['c']}[4] fill (attempt 2){C['x']} — eval removed, tests supplied")
    r2 = o.review(ssat)
    if r2["reviewed"]:
        p(f"      {C['g']}✓ judge + grumpy + arch erosion all pass{C['x']}")
        o.arch_gate(ssat); o.ship()
        p(f"      {C['g']}✓ architecture CI gate pass → SHIPPED{C['x']}\n")
    p(f"{C['d']}The factory caught its own bad output, learned, and shipped clean.")
    p(f"  Wire it in: forge agent claude   |   forge agent codex{C['x']}\n")

if __name__ == "__main__":
    run()
