"""Skill memory — the self-accelerating flywheel. Records what failed in each
run and the fix, so future runs inject accumulated lessons into agent context.
This is the 'skill learns from past refinements' mechanism, made concrete and
LLM-free: lessons are structured, deduped, and promotable to constraints."""
from __future__ import annotations
import json, datetime
from pathlib import Path

LESSONS = "skills/lessons.jsonl"

def record_lesson(root: Path, *, phase: str, failure_code: str, fix: str, feature: str):
    root = Path(root); lp = root/LESSONS
    lp.parent.mkdir(parents=True, exist_ok=True)
    # dedupe by (phase, failure_code, fix)
    existing = []
    if lp.exists():
        existing = [json.loads(l) for l in lp.read_text().splitlines() if l.strip()]
    key = (phase, failure_code, fix)
    for e in existing:
        if (e["phase"], e["failure_code"], e["fix"]) == key:
            e["count"] += 1; e["last_seen"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            lp.write_text("\n".join(json.dumps(x) for x in existing) + "\n")
            return e
    entry = {"phase": phase, "failure_code": failure_code, "fix": fix, "feature": feature,
             "count": 1, "last_seen": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    existing.append(entry)
    lp.write_text("\n".join(json.dumps(x) for x in existing) + "\n")
    return entry

def lessons_for(root: Path, phase: str, min_count: int = 1) -> list[dict]:
    lp = Path(root)/LESSONS
    if not lp.exists(): return []
    rows = [json.loads(l) for l in lp.read_text().splitlines() if l.strip()]
    return sorted([r for r in rows if r["phase"] == phase and r["count"] >= min_count],
                  key=lambda r: -r["count"])

def promotable_constraints(root: Path, threshold: int = 3) -> list[dict]:
    """Lessons seen >= threshold times graduate into hard constraints
    (conventions-into-constraints). These get injected as SSAT invariants."""
    lp = Path(root)/LESSONS
    if not lp.exists(): return []
    rows = [json.loads(l) for l in lp.read_text().splitlines() if l.strip()]
    return [r for r in rows if r["count"] >= threshold]

def inject_lessons_block(root: Path, phase: str) -> str:
    """Text block injected into an agent's task context for this phase."""
    ls = lessons_for(root, phase)
    if not ls: return ""
    lines = [f"## Lessons from past runs (phase: {phase})"]
    for l in ls[:8]:
        lines.append(f"- [{l['failure_code']} ×{l['count']}] {l['fix']}")
    return "\n".join(lines)
