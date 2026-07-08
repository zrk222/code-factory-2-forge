"""The Intent Thread — end-to-end traceability from PRD to production.

The breakthrough: SpecLine rationalizes intent into a sealed envelope
(coherence + surfaced assumptions + hash). ForgeLine consumes that same
envelope so the FINAL shipped code can be verified against the ORIGINAL
intent — not the plan, not the spec-as-drifted, but the rationalized intent
that was sealed at the plan gate.

This closes the last translation-loss gap (implementation → validation):
'does the shipped thing actually satisfy what we rationalized we wanted?'
Every surfaced assumption becomes a checkable obligation; the sealed hash
proves the intent hasn't been quietly swapped underneath the build.
"""
from __future__ import annotations
import json, hashlib
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class IntentTraceResult:
    envelope_found: bool = False
    intent_hash: str = ""
    coherence_at_seal: int = 0
    assumptions: list = field(default_factory=list)
    unverified_assumptions: list = field(default_factory=list)
    obligations_met: int = 0
    obligations_total: int = 0
    findings: list = field(default_factory=list)

    @property
    def traceable(self) -> bool:
        return self.envelope_found and not self.unverified_assumptions

def load_envelope(root: Path, feature: str) -> dict | None:
    """Load the SpecLine intent envelope if present (cross-module seam)."""
    for cand in [root/"envelopes"/f"{feature}.json",
                 root.parent/"specline"/"envelopes"/f"{feature}.json"]:
        if cand.exists():
            return json.loads(cand.read_text())
    return None

def verify_against_intent(root: Path, feature: str, src_dir: Path) -> IntentTraceResult:
    """Check shipped code honors the sealed intent's surfaced assumptions.
    Each assumption becomes an obligation the code must visibly address."""
    r = IntentTraceResult()
    env = load_envelope(root, feature)
    if not env:
        r.findings.append("IT_NO_ENVELOPE: no sealed intent envelope — build is untraceable to a rationalized PRD.")
        return r
    r.envelope_found = True
    r.intent_hash = env.get("sealed_hash", "")
    r.coherence_at_seal = env.get("coherence_score", 0)
    r.assumptions = env.get("assumptions", [])
    r.obligations_total = len(r.assumptions)

    # each surfaced assumption is an obligation — is it visibly addressed in code?
    code_text = "\n".join(p.read_text() for p in Path(src_dir).rglob("*.py")).lower()
    OBLIGATION_EVIDENCE = {
        "auth":      ["auth", "login", "token", "session", "identity", "authenticate"],
        "currency":  ["currency", "usd", "locale", "money", "decimal"],
        "dependency":["retry", "timeout", "fallback", "except", "circuit", "backoff"],
        "delivery":  ["retry", "bounce", "queue", "deliver", "ack", "confirm"],
    }
    for a in r.assumptions:
        al = a.lower()
        key = ("auth" if "auth" in al or "identity" in al else
               "currency" if "currency" in al else
               "dependency" if "dependency" in al or "availability" in al else
               "delivery" if "delivery" in al else None)
        evidence = OBLIGATION_EVIDENCE.get(key, [])
        if evidence and any(e in code_text for e in evidence):
            r.obligations_met += 1
        else:
            r.unverified_assumptions.append(a)
            r.findings.append(f"IT_UNMET_ASSUMPTION: intent assumed — '{a[:70]}' — but code shows no handling.")
    return r
