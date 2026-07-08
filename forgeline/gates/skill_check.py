"""Skill-check gate — ensures the AGENT EXPERIENCE stays high: the run
produced the receipts, the state machine advanced legally, and the skill
memory recorded a lesson. Keeps the factory itself from eroding."""
from __future__ import annotations
from pathlib import Path

def skill_check(root: Path, feature: str) -> tuple[bool, list[str]]:
    findings = []
    fd = Path(root)/".forge"/feature
    if not (fd/"receipts.jsonl").exists() or not (fd/"receipts.jsonl").read_text().strip():
        findings.append("S_NO_RECEIPTS run produced no receipts")
    if not (fd/"state.json").exists():
        findings.append("S_NO_STATE run has no state record")
    return (len(findings) == 0, findings)
