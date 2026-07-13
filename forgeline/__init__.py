"""ForgeLine — build the machine that writes the code.

The tier above SpecLine and HSF: an autonomous state machine that carries a
feature from vague intent to shipped code through confidence gates, running
a generate -> adversarial-review -> refine loop, enforcing architecture as a
hard CI gate, and learning from every run via an evolving skill memory.

  INTENT -> EXPAND -> ARCHITECT(SSAT) -> SCAFFOLD -> FILL
         -> JUDGE + ADVERSARY(grumpy) -> ARCH-GATE -> VERIFY-TESTS -> SMOKE -> SHIP
                         ^                              |
                         └──────── refine loop ─────────┘
                                   skill memory learns each pass
"""
__version__ = "0.10.0"
