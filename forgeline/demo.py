"""forge demo - a headless walk through refine loop + skill flywheel."""
from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

C = {"g": "\033[92m", "c": "\033[96m", "y": "\033[93m", "d": "\033[2m", "b": "\033[1m", "x": "\033[0m"}


def p(message: str, sleep: float = 0.02) -> None:
    print(message.encode("ascii", "replace").decode("ascii"))
    time.sleep(sleep)


def run() -> None:
    from forgeline.orchestrator import Orchestrator
    from forgeline.skill_memory import lessons_for
    from forgeline.states import State

    root = Path(tempfile.mkdtemp())
    (root / "skills").mkdir()
    ssat_src = Path(__file__).resolve().parents[1] / "examples" / "notifier.ssat.yaml"
    ssat = root / "notifier.ssat.yaml"
    shutil.copy(ssat_src, ssat)

    p(f"\n{C['b']}* ForgeLine - autonomous factory outer loop{C['x']}")
    p(f"{C['d']}  intent -> SSAT -> scaffold -> fill -> adversarial review -> ship{C['x']}\n")

    orchestrator = Orchestrator(root, "notifier")
    orchestrator.store.set_state(State.ARCHITECTED)
    p(f"{C['c']}[1] architect{C['x']} - scaffolding signatures from architecture-as-code")
    orchestrator.architect(ssat)
    p(f"      {C['g']}OK{C['x']} 2 modules scaffolded with valid imports\n")

    slices = root / "slices" / "notifier"
    (slices / "formatter.py").write_text(
        'def format_message(event: dict) -> str:\n    return str(eval(event.get("expr","1")))\n',
        encoding="utf-8",
    )
    (slices / "sender.py").write_text(
        "from slices.notifier.formatter import format_message\n"
        "def send(event: dict, channel: str) -> bool:\n"
        "    return bool(format_message(event) and channel)\n",
        encoding="utf-8",
    )
    orchestrator.store.set_state(State.FILLED)
    p(f"{C['c']}[2] fill (attempt 1){C['x']} - agent used eval(), shipped no tests")
    review = orchestrator.review(ssat)
    p(f"      {C['y']}BLOCKED: grumpy adversary findings:{C['x']}")
    for finding in review["findings"]:
        p(f"        - {finding}")
    p(f"      {C['d']}lesson recorded to skill memory{C['x']}\n")

    p(f"{C['c']}[3] lessons injected into next context:{C['x']}")
    for lesson in lessons_for(root, "fill"):
        p(f"        [{lesson['failure_code']} x{lesson['count']}] {lesson['fix'][:60]}")

    (slices / "formatter.py").write_text(
        "def format_message(event: dict) -> str:\n"
        "    return f\"{event.get('kind','x')}: {event.get('text','')}\"\n",
        encoding="utf-8",
    )
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_notifier.py").write_text(
        "def test_ok():\n    assert True\n",
        encoding="utf-8",
    )
    p(f"\n{C['c']}[4] fill (attempt 2){C['x']} - eval removed, tests supplied")
    review2 = orchestrator.review(ssat)
    if review2["reviewed"]:
        p(f"      {C['g']}OK judge + grumpy + arch erosion all pass{C['x']}")
        orchestrator.arch_gate(ssat)
        orchestrator.ship()
        p(f"      {C['g']}OK architecture CI gate pass -> SHIPPED{C['x']}\n")

    p(f"{C['d']}The factory caught its own bad output, learned, and shipped clean.")
    p(f"  Wire it in: forge agent claude   |   forge agent codex{C['x']}\n")


if __name__ == "__main__":
    run()
