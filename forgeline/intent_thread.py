"""Read sealed intent without confusing text matches with verified acceptance.

A checksum establishes local content integrity only. Executable acceptance
 evidence must be admitted separately; words in source cannot satisfy it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from contextlib import ExitStack
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
    envelope_valid: bool = False
    evidence_verified: bool = False

    @property
    def traceable(self) -> bool:
        return (self.envelope_valid and self.evidence_verified
                and self.obligations_total > 0
                and self.obligations_met == self.obligations_total
                and not self.unverified_assumptions)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate envelope key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("non-finite envelope number")


def _windows_handle_path(stream) -> Path:
    import ctypes
    import msvcrt
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
    api.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    api.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = api(msvcrt.get_osfhandle(stream.fileno()), buffer, len(buffer), 0)
    if not length or length >= len(buffer):
        raise OSError("cannot establish opened envelope path")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def _bounded_read(stream) -> bytes:
    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
        raise ValueError("envelope must be a regular file")
    return stream.read(1_048_577)


def _read_envelope(root: Path, path: Path) -> bytes:
    if os.name == "nt":
        with path.open("rb") as stream:
            # Validate the opened handle before reading, not a second path lookup.
            if not _windows_handle_path(stream).is_relative_to(root):
                raise ValueError("opened envelope escapes repository")
            return _bounded_read(stream)
    # Directory descriptors anchor the walk; no component may follow a symlink.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as stack:
        root_fd = os.open(root, flags)
        stack.callback(os.close, root_fd)
        directory_fd = os.open("envelopes", flags, dir_fd=root_fd)
        stack.callback(os.close, directory_fd)
        file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                          dir_fd=directory_fd)
        with os.fdopen(file_fd, "rb") as stream:
            return _bounded_read(stream)


def load_envelope(root: Path, feature: str) -> dict | None:
    """Read one contained, bounded envelope; never borrow another repository's."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", feature):
        raise ValueError("invalid feature identity")
    root = Path(root).resolve()
    path = root / "envelopes" / f"{feature}.json"
    if not path.resolve().is_relative_to(root):
        raise ValueError("envelope path escapes repository")
    try:
        data = _read_envelope(root, path)
    except FileNotFoundError:
        if path.is_symlink():
            raise ValueError("dangling envelope link")
        return None
    if len(data) > 1_048_576:
        raise ValueError("envelope exceeds size bound")
    value = json.loads(data, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("envelope must be an object")
    return value


def verify_against_intent(root: Path, feature: str, src_dir: Path) -> IntentTraceResult:
    """Validate intent identity; retain unmet obligations until evidence admission."""
    result = IntentTraceResult()
    try:
        envelope = load_envelope(root, feature)
    except (ValueError, OSError, RuntimeError):
        result.envelope_found = True
        result.unverified_assumptions = ["Intent envelope could not be validated."]
        result.findings.append("IT_INVALID_ENVELOPE: regenerate the contained intent envelope.")
        return result
    if envelope is None:
        result.findings.append("IT_NO_ENVELOPE: no local sealed intent; intent coverage is unverified.")
        return result
    result.envelope_found = True
    assumptions = envelope.get("assumptions")
    if (envelope.get("schema") != "specline.intent-envelope.v1"
            or not isinstance(assumptions, list) or not assumptions
            or any(not isinstance(item, str) or not item.strip() for item in assumptions)):
        result.unverified_assumptions = ["Missing or unsupported declared intent obligations."]
        result.findings.append("IT_INVALID_ENVELOPE: unsupported schema or empty/invalid obligations.")
        return result
    result.assumptions = list(assumptions)
    result.unverified_assumptions = list(assumptions)
    result.obligations_total = len(assumptions)
    sealed_hash = envelope.get("sealed_hash")
    content = {key: value for key, value in envelope.items() if key != "sealed_hash"}
    try:
        digest = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":"),
                                          ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    except (ValueError, TypeError):
        result.findings.append("IT_INVALID_ENVELOPE: intent content is not canonical JSON.")
        return result
    if sealed_hash != digest:
        result.findings.append("IT_ENVELOPE_HASH: sealed intent content has changed or is unsealed.")
        return result
    result.intent_hash = digest
    result.envelope_valid = True
    coherence = envelope.get("coherence_score", 0)
    if type(coherence) is int and 0 <= coherence <= 100:
        result.coherence_at_seal = coherence
    result.findings.append(
        "IT_ACCEPTANCE_EVIDENCE_REQUIRED: valid intent checksum; executable acceptance links are not admitted."
    )
    return result
