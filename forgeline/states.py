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
