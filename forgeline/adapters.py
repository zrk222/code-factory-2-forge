"""Wire ForgeLine as the ENTRY-POINT skill for an agent harness. The agent's
whole loop becomes: read the skill contract, run `forge status`, do the one
phase it names, run the gate command, repeat. This is the 'skill as the entire
entry point (claw.md-style executable contract)' pattern."""
from __future__ import annotations
from pathlib import Path

SKILL_CONTRACT = """# FORGE SKILL — executable contract (read first, every session)

You are operating inside a ForgeLine factory. Do NOT free-code. Follow the
state machine:

1. Run `forge status <feature>` — it tells you the current state and the ONE
   next action.
2. Do exactly that action:
   - EXPANDED gate: draft use cases into the spec; STOP for human signoff.
   - ARCHITECTED gate: write/refine the SSAT (architecture as code); STOP for signoff.
   - SCAFFOLDED: run `forge architect <feature> <ssat.yaml>` (generates signatures).
   - FILLED: implement ONLY function bodies; never change signatures; write tests.
   - REVIEWED: run `forge review <feature> <ssat.yaml>`. If it fails, read the
     `lessons_for_next` block it prints and fix — do not argue with the adversary.
   - ARCH_GATED: run `forge verify-tests <feature> <ssat.yaml>`.
   - TESTS_VERIFIED: run `forge smoke <feature>`.
   - SMOKED: run `forge ship <feature>`.
3. Between phases your context resets. The disk + `forge status` are the truth.
4. Decision logic (ordered business rules) is NEVER coded inline — it routes to
   the Harness Software Factory via the spec's decision table.

The grumpy adversary assumes your code is broken. Prove it isn't with tests
and architectural completeness. That's the job.
"""

# where each agent harness looks for its project contract file
AGENT_TARGETS = {
    "claude":   ["CLAUDE.md", ".claude/skills/forge.md"],
    "codex":    ["AGENTS.md"],
    "opencode": ["AGENTS.md", ".opencode/forge.md"],
    "cursor":   [".cursorrules", ".cursor/rules/forge.md"],
    "aider":    ["CONVENTIONS.md"],
    "gemini":   ["GEMINI.md"],
    "windsurf": [".windsurfrules"],
    "generic":  ["AGENT.md"],
}

def wire_agent(root: Path, agent: str) -> list[Path]:
    """Write the entry-point skill contract wherever the named agent reads it.
    Unknown agents fall back to AGENTS.md (the emerging cross-tool default)."""
    root = Path(root); created = []
    key = {"claude-code": "claude", "claude code": "claude"}.get(agent, agent)
    targets = AGENT_TARGETS.get(key, ["AGENTS.md"])
    for rel in targets:
        dst = root/rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(SKILL_CONTRACT)
        created.append(dst)
    return created
