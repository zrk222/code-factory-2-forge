"""The Agentic SDLC state machine. States are explicit, transitions are
gated, and every transition is a receipt. Humans own confidence gates;
agents own execution between them."""
from __future__ import annotations
from enum import Enum

class State(str, Enum):
    INTENT       = "intent"          # vague human intent captured
    EXPANDED     = "expanded"        # use cases drafted (human confidence gate)
    ARCHITECTED  = "architected"     # SSAT / architecture-as-code produced
    SCAFFOLDED   = "scaffolded"      # signatures + files, valid imports
    FILLED       = "filled"          # function bodies implemented
    REVIEWED     = "reviewed"        # judge + adversary passed
    ARCH_GATED   = "arch_gated"      # architecture CI gate passed
    TESTS_VERIFIED = "tests_verified"  # smoke checks proven non-hollow
    SMOKED       = "smoked"          # runtime behavior verified (smoke gate)
    SHIPPED      = "shipped"
    BLOCKED      = "blocked"         # a gate failed; refine loop owns it

# allowed forward transitions; anything else is E_ILLEGAL_TRANSITION
TRANSITIONS = {
    State.INTENT:      {State.EXPANDED},
    State.EXPANDED:    {State.ARCHITECTED, State.BLOCKED},
    State.ARCHITECTED: {State.SCAFFOLDED, State.BLOCKED},
    State.SCAFFOLDED:  {State.FILLED, State.BLOCKED},
    State.FILLED:      {State.REVIEWED, State.BLOCKED},
    State.REVIEWED:    {State.ARCH_GATED, State.BLOCKED},
    State.ARCH_GATED:  {State.TESTS_VERIFIED, State.BLOCKED},
    State.TESTS_VERIFIED: {State.SMOKED, State.BLOCKED},
    State.SMOKED:      {State.SHIPPED, State.BLOCKED},
    State.BLOCKED:     {State.SCAFFOLDED, State.FILLED, State.REVIEWED, State.ARCH_GATED, State.TESTS_VERIFIED, State.SMOKED},
    State.SHIPPED:     set(),
}

HUMAN_GATES = {State.EXPANDED, State.ARCHITECTED}   # confidence gates requiring signoff

class IllegalTransition(RuntimeError):
    pass

def can_transition(frm: State, to: State) -> bool:
    return to in TRANSITIONS.get(frm, set())


_RESUME_TARGETS = {
    "scaffold": {State.SCAFFOLDED},
    "fill": {State.FILLED},
    "review": {State.REVIEWED},
    "arch_gate": {State.ARCH_GATED},
    "verify_tests": {State.TESTS_VERIFIED},
    "smoke_gate": {State.TESTS_VERIFIED, State.SMOKED},
    "ship": {State.TESTS_VERIFIED, State.SMOKED},
}
_LEGACY_BLOCK_NOTES = {
    "fill verification failed": "fill",
    "arch gate failed": "arch_gate",
    "reverse-classical test verification failed": "verify_tests",
    "runtime smoke gate failed": "smoke_gate",
    "intent-traceability gap": "ship",
}


def blocked_gate(run: dict) -> str | None:
    if run["state"] != State.BLOCKED.value or not run["history"]:
        return None
    last = run["history"][-1]
    gate = last.get("failed_gate")
    if gate in _RESUME_TARGETS:
        return gate
    note = last.get("note", "")
    if note.startswith("review failed ("):
        return "review"
    return _LEGACY_BLOCK_NOTES.get(note)


def unproven_blocked(run: dict, has_receipts: bool) -> bool:
    """A blocked run with no receipts and no known failed gate proves nothing (for example a state file another
    tool wrote). It can restart at expand: there is no evidence to bypass."""
    return run["state"] == State.BLOCKED.value and not has_receipts and blocked_gate(run) is None


def can_resume(run: dict, target: State) -> bool:
    gate = blocked_gate(run)
    return gate is not None and target in _RESUME_TARGETS[gate]
