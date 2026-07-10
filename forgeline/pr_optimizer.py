"""Bounded PR hardening loop design for ForgeLine."""
from __future__ import annotations

from pathlib import Path
import subprocess


def _changed(root: Path, base: str) -> list[str]:
    proc = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=20,
    )
    if proc.returncode != 0:
        proc = subprocess.run(["git", "diff", "--name-only"], cwd=str(root), capture_output=True, text=True, timeout=20)
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def optimize_pr(root: Path, feature: str | None = None, *, base: str = "main") -> dict:
    root = Path(root)
    changed = _changed(root, base)
    risky = [
        path for path in changed
        if any(part in path.replace("\\", "/").lower() for part in ("auth", "security", "billing", "deploy", "workflow"))
    ]
    commands = [
        "forge qa --strict",
        "forge lessons",
        "factory optimize-pr --json",
    ]
    if feature:
        commands.extend([f"forge status {feature}", f"factory pr-pack {feature}"])
    return {
        "schema": "forgeline.optimize_pr.v1",
        "feature": feature,
        "base": base,
        "changed_paths": changed,
        "approval_required": bool(risky),
        "risky_paths": risky,
        "loop": {
            "observe": "read current diff, receipts, lessons, and review comments",
            "act": "make one localized reversible edit",
            "verify": "rerun the failing gate and factory evidence command",
            "stop": "ready, blocked, approval_required, exhausted after 5 iterations, or stagnated after no improvement",
            "max_iterations": 5,
        },
        "commands": commands,
    }
