"""Per-feature run state + receipts. The state machine's durable memory —
survives context resets (Ralph Wiggum) because the disk is the truth."""
from __future__ import annotations
import json, datetime, hashlib
from pathlib import Path
from .states import State

def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

class RunStore:
    def __init__(self, root: Path, feature: str):
        self.root = Path(root); self.feature = feature
        self.dir = self.root/".forge"/feature
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir/"state.json"
        self.receipts = self.dir/"receipts.jsonl"
        if not self.state_path.exists():
            self._write({"feature": feature, "state": State.INTENT.value,
                         "created": _now(), "attempts": {}, "history": []})

    def _write(self, data): self.state_path.write_text(json.dumps(data, indent=2))
    def load(self) -> dict: return json.loads(self.state_path.read_text())

    @property
    def state(self) -> State:
        return State(self.load()["state"])

    def set_state(self, s: State, note: str = ""):
        d = self.load(); d["state"] = s.value
        d["history"].append({"ts": _now(), "state": s.value, "note": note})
        self._write(d)

    def bump_attempt(self, phase: str) -> int:
        d = self.load(); d["attempts"][phase] = d["attempts"].get(phase, 0) + 1
        self._write(d); return d["attempts"][phase]

    def receipt(self, **fields):
        fields["ts"] = _now()
        line = json.dumps(fields, sort_keys=True)
        fields = {"h": hashlib.sha256(line.encode()).hexdigest()[:12], **fields}
        with self.receipts.open("a") as f:
            f.write(json.dumps(fields, sort_keys=True) + "\n")

    def latest_receipt(self, phase: str) -> dict | None:
        """Return the most recent receipt for one phase without trusting state."""
        if not self.receipts.exists():
            return None
        latest = None
        for line in self.receipts.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("phase") == phase:
                latest = item
        return latest
