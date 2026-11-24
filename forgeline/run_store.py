"""Per-feature run state + receipts. The state machine's durable memory —
survives context resets (Ralph Wiggum) because the disk is the truth."""
from __future__ import annotations
import json, datetime, hashlib, os, re, tempfile
from pathlib import Path
from collections.abc import Iterable
from contextlib import contextmanager
from .states import State, IllegalTransition, can_transition

def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def _strict_receipt(item, previous_ts):
    if not isinstance(item, dict):
        raise ValueError("invalid receipt fields")
    digest = item.pop("h", "")
    expected = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()[:12]
    stamp = datetime.datetime.fromisoformat(item["ts"])
    if digest != expected or (previous_ts and stamp < previous_ts):
        raise ValueError("invalid receipt chain")
    item["h"] = digest
    return item, stamp

def _atomic_write(path: Path, data: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class RunStore:
    def __init__(self, root: Path, feature: str, *, create=True):
        if not feature or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", feature) is None or feature.endswith("."):
            raise ValueError("feature must be a single path-safe name")
        self.root = Path(root)
        self.feature = feature
        self.dir = self.root / ".forge" / feature
        self.state_path = self.dir / "state.json"
        self.receipts = self.dir / "receipts.jsonl"
        self.pending = self.dir / "transition.pending.json"
        if create and not self.state_path.exists():
            self.dir.mkdir(parents=True, exist_ok=True)
            with self._locked():
                if not self.state_path.exists():
                    self._write({"feature": feature, "state": State.INTENT.value,
                                 "created": _now(), "attempts": {}, "history": []})

    def _write(self, data):
        _atomic_write(self.state_path, json.dumps(data, indent=2).encode("utf-8"))

    @contextmanager
    def _locked(self):
        with (self.dir / "store.lock").open("a+b") as handle:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                unlock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX)
                unlock = lambda: fcntl.flock(handle, fcntl.LOCK_UN)
            try:
                yield
            finally:
                unlock()

    @staticmethod
    def _sealed(fields):
        fields = {"ts": _now(), **fields}
        digest = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()[:12]
        return {"h": digest, **fields}

    @staticmethod
    def _line(receipt):
        return (json.dumps(receipt, sort_keys=True) + "\n").encode("utf-8")

    def _append(self, receipt):
        with self.receipts.open("ab") as stream:
            stream.write(self._line(receipt))
            stream.flush()
            os.fsync(stream.fileno())

    def _pending_view(self):
        try:
            record = json.loads(self.pending.read_bytes())
        except FileNotFoundError:
            return None, False
        line = self._line(record["receipt"])
        _strict_receipt(dict(record["receipt"]), None)
        offset = record["offset"]
        try:
            with self.receipts.open("rb") as stream:
                stream.seek(offset)
                tail = stream.read(len(line) + 1)
        except FileNotFoundError:
            if offset:
                raise ValueError("transition receipt history is missing") from None
            tail = b""
        if len(tail) > len(line) or not line.startswith(tail):
            raise ValueError("transition receipt conflicts with its journal")
        return record, tail == line

    def _settle_pending(self):
        record, committed = self._pending_view()
        if record is None:
            return
        if committed:
            self._write(record["state"])
        elif self.receipts.exists():
            with self.receipts.open("r+b") as stream:
                stream.truncate(record["offset"])
                stream.flush()
                os.fsync(stream.fileno())
        self.pending.unlink()

    def load(self) -> dict:
        current = json.loads(self.state_path.read_bytes())
        record, committed = self._pending_view()
        return record["state"] if committed else current

    @property
    def state(self) -> State:
        return State(self.load()["state"])

    @staticmethod
    def _project(data, state, note, timestamp):
        data["state"] = state.value
        data["history"].append({"ts": timestamp, "state": state.value, "note": note})
        return data

    @contextmanager
    def operation(self):
        with self._locked():
            self._settle_pending()
            yield

    def transition(self, expected: State, target: State, note: str, **fields):
        with self.operation():
            self._transition(expected, target, note, **fields)

    def _transition(self, expected: State, target: State, note: str, **fields):
        data = self.load()
        current = State(data["state"])
        if current != expected or not (current == target or can_transition(current, target)):
            raise IllegalTransition(f"{current.value} -> {target.value}; expected {expected.value}")
        receipt = self._sealed({"transition": f"{current.value}->{target.value}", **fields})
        offset = self.receipts.stat().st_size if self.receipts.exists() else 0
        after = self._project(data, target, note, receipt["ts"])
        _atomic_write(self.pending, json.dumps({"offset": offset, "receipt": receipt,
                                                "state": after}).encode("utf-8"))
        self._append(receipt)
        self._write(after)
        self.pending.unlink()

    def set_state(self, state: State, note: str = ""):
        with self._locked():
            self._settle_pending()
            self._write(self._project(self.load(), state, note, _now()))

    def bump_attempt(self, phase: str) -> int:
        with self._locked():
            self._settle_pending()
            data = self.load()
            count = data["attempts"].get(phase, 0) + 1
            data["attempts"][phase] = count
            self._write(data)
            return count

    def receipt(self, **fields):
        with self._locked():
            self._settle_pending()
            self._append(self._sealed(fields))

    def latest_receipts(
        self, phases: Iterable[str], *, strict: bool = False
    ) -> dict[str, dict | None]:
        """Return latest receipts from one fresh read of the full history."""
        requested = tuple(dict.fromkeys(phases))
        latest: dict[str, dict | None] = dict.fromkeys(requested)
        if not self.receipts.exists():
            return latest
        pending, committed = self._pending_view()
        if pending and not committed:
            with self.receipts.open("rb") as stream:
                text = stream.read(pending["offset"]).decode("utf-8")
        else:
            text = self.receipts.read_text(encoding="utf-8")
        previous_ts = None
        for line in text.splitlines():
            try:
                item = json.loads(line)
                if strict:
                    item, stamp = _strict_receipt(item, previous_ts)
                    previous_ts = stamp
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                if strict:
                    raise ValueError("corrupt receipt history")
                continue
            if not isinstance(item, dict):
                continue
            phase = item.get("phase")
            if isinstance(phase, str) and phase in latest:
                latest[phase] = dict(item)
        return latest

    def latest_receipt(self, phase: str, *, strict: bool = False) -> dict | None:
        """Return the most recent receipt for one phase without trusting state."""
        return self.latest_receipts((phase,), strict=strict)[phase]
