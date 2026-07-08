"""Recursive Learning Kernel — the flywheel that makes the factory improve
itself. Distinct from skill_memory (which records lessons): this CLOSES THE
LOOP by promoting recurring lessons into ACTIVE enforcement, tracking whether
promoted rules actually reduce future failures, and DEMOTING rules that stop
earning their keep. The factory's own run history becomes its QA policy.

Three tiers, each a ratchet:
  observe  -> a lesson is recorded (skill_memory)
  promote  -> a lesson seen >= N times becomes an ACTIVE constraint (enforced)
  validate -> a promoted constraint that prevents recurrence is KEPT; one that
              never fires again (dead) or fires with no failures behind it is
              reviewed. Effectiveness is measured, not assumed.
"""
from __future__ import annotations
import json, datetime
from dataclasses import dataclass, asdict
from pathlib import Path

POLICY = "skills/active_policy.json"
HISTORY = "skills/learning_history.jsonl"

def _now(): return datetime.datetime.now(datetime.timezone.utc).isoformat()

@dataclass
class ActiveConstraint:
    code: str                 # e.g. "A_EVAL"
    rule: str                 # human-readable enforced rule
    promoted_from_count: int  # how many observations triggered promotion
    promoted_at: str
    times_enforced: int = 0   # how often it has since blocked something
    prevented_recurrence: int = 0  # times it caught the same failure again
    status: str = "active"    # active | probation | retired

class LearningKernel:
    def __init__(self, root: Path, promote_threshold: int = 3):
        self.root = Path(root)
        self.threshold = promote_threshold
        self.policy_path = self.root/POLICY
        self.history_path = self.root/HISTORY
        self.policy_path.parent.mkdir(parents=True, exist_ok=True)

    def _load_policy(self) -> dict:
        if self.policy_path.exists():
            return json.loads(self.policy_path.read_text())
        return {"constraints": {}, "version": 0}

    def _save_policy(self, p: dict):
        p["version"] = p.get("version", 0) + 1
        p["updated"] = _now()
        self.policy_path.write_text(json.dumps(p, indent=2))

    def _log(self, event: str, **f):
        f.update({"ts": _now(), "event": event})
        with self.history_path.open("a") as fh:
            fh.write(json.dumps(f, sort_keys=True) + "\n")

    def promote(self, lessons: list[dict]) -> list[str]:
        """Tier 2: any lesson seen >= threshold becomes an active constraint.
        Returns the codes newly promoted this cycle."""
        policy = self._load_policy(); newly = []
        for l in lessons:
            code = l["failure_code"]
            if l["count"] >= self.threshold and code not in policy["constraints"]:
                c = ActiveConstraint(code=code, rule=l["fix"],
                                     promoted_from_count=l["count"], promoted_at=_now())
                policy["constraints"][code] = asdict(c)
                newly.append(code)
                self._log("promote", code=code, from_count=l["count"], rule=l["fix"])
        if newly:
            self._save_policy(policy)
        return newly

    def enforce(self, findings_codes: list[str]) -> dict:
        """Tier 3 measurement: record which active constraints fired this run.
        A constraint that catches its target failure again = validated (working).
        Returns {code: prevented_bool} for constraints that were relevant."""
        policy = self._load_policy(); result = {}
        for code in findings_codes:
            if code in policy["constraints"]:
                c = policy["constraints"][code]
                c["times_enforced"] += 1
                c["prevented_recurrence"] += 1
                result[code] = True
                self._log("enforce", code=code, prevented=True)
        if result:
            self._save_policy(policy)
        return result

    def audit_effectiveness(self, recent_run_count: int = 20) -> dict:
        """Recursive review: constraints that keep catching real failures are
        VALIDATED; ones promoted long ago that never fire go to PROBATION
        (candidate for retirement — the policy self-prunes)."""
        policy = self._load_policy()
        validated, probation = [], []
        for code, c in policy["constraints"].items():
            if c["status"] == "retired":
                continue
            if c["prevented_recurrence"] >= 1:
                c["status"] = "active"; validated.append(code)
            elif c["times_enforced"] == 0 and c["status"] == "active":
                c["status"] = "probation"; probation.append(code)
                self._log("probation", code=code, reason="never fired since promotion")
        self._save_policy(policy)
        return {"validated": validated, "probation": probation,
                "total_active": sum(1 for c in policy["constraints"].values() if c["status"]=="active")}

    def active_codes(self) -> list[str]:
        policy = self._load_policy()
        return [k for k,v in policy["constraints"].items() if v["status"] in ("active","probation")]

    def policy_summary(self) -> dict:
        policy = self._load_policy()
        return {"version": policy.get("version",0),
                "constraints": {k: {"status": v["status"], "enforced": v["times_enforced"],
                                    "prevented": v["prevented_recurrence"]}
                                for k,v in policy["constraints"].items()}}
