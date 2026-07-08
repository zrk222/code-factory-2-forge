"""forge demo-learning — shows the recursive loop: a failure recurs, gets
promoted into active policy, then that policy catches it on the next run and
is marked validated. The factory improving its own QA."""
from __future__ import annotations
import tempfile, time
from pathlib import Path
from .learning import LearningKernel
from .skill_memory import record_lesson, lessons_for

C = {"g":"\033[92m","c":"\033[96m","y":"\033[93m","d":"\033[2m","b":"\033[1m","x":"\033[0m"}
def p(m): print(m); time.sleep(0.02)

def run():
    root = Path(tempfile.mkdtemp()); (root/"skills").mkdir()
    k = LearningKernel(root, promote_threshold=3)
    p(f"\n{C['b']}✦ ForgeLine — recursive learning loop{C['x']}")
    p(f"{C['d']}  observe → promote → validate → self-prune{C['x']}\n")

    p(f"{C['c']}[1] observe{C['x']} — the same flaw (eval) appears in 3 runs")
    for i in range(3):
        record_lesson(root, phase="fill", failure_code="A_EVAL", fix="never use eval()", feature=f"run{i}")
        p(f"      {C['y']}▲{C['x']} run {i+1}: A_EVAL recorded")

    p(f"\n{C['c']}[2] promote{C['x']} — seen ≥3× → becomes an ENFORCED constraint")
    promoted = k.promote(lessons_for(root, "fill"))
    p(f"      {C['g']}✓{C['x']} promoted to active policy: {promoted}")

    p(f"\n{C['c']}[3] validate{C['x']} — next run, the policy CATCHES the same flaw")
    prevented = k.enforce(["A_EVAL"])
    p(f"      {C['g']}✓{C['x']} A_EVAL caught by learned policy → validated")

    p(f"\n{C['c']}[4] self-prune{C['x']} — a stale rule that never fires goes to probation")
    for i in range(3):
        record_lesson(root, phase="fill", failure_code="A_OLD", fix="obsolete rule", feature="x")
    k.promote(lessons_for(root, "fill"))
    eff = k.audit_effectiveness()
    p(f"      {C['d']}validated: {eff['validated']}  ·  probation: {eff['probation']}{C['x']}")

    s = k.policy_summary()
    p(f"\n{C['d']}Active policy v{s['version']} — the factory now enforces what it learned.")
    p(f"  Every future build is checked against its own accumulated experience.{C['x']}\n")

if __name__ == "__main__":
    run()
