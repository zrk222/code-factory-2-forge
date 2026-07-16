"""Installed-package provenance for reproducible ForgeLine receipts."""
from __future__ import annotations

import importlib.metadata
import json
import hashlib
import subprocess
import sys
from pathlib import Path

from . import __version__
from ._build_provenance import SOURCE_COMMIT


def _source_commit(module_dir: Path) -> str | None:
    """Return a checked-out source revision when one is actually available."""
    source_root = module_dir.parent
    manifest = source_root / "pyproject.toml"
    if not (source_root / ".git").exists() or not manifest.exists():
        return None
    if 'name = "code-factory-2-forge"' not in manifest.read_text(encoding="utf-8"):
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=source_root, capture_output=True,
            text=True, timeout=3, check=False,
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"], cwd=source_root,
            capture_output=True, text=True, timeout=3, check=False,
        )
    except OSError:
        return None
    if result.returncode != 0 or dirty.returncode != 0 or dirty.stdout.strip():
        return None
    return result.stdout.strip()


def _build_hash(module_dir: Path) -> str:
    """Stable hash of the installed Python package payload, not a claimed commit."""
    digest = hashlib.sha256()
    for path in sorted(module_dir.rglob("*.py")):
        digest.update(path.relative_to(module_dir).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def provenance() -> dict:
    """Return only facts available from the active installed distribution."""
    module_dir = Path(__file__).resolve().parent
    install_origin = "unknown"
    direct_url: dict | None = None
    try:
        distribution = importlib.metadata.distribution("code-factory-2-forge")
        metadata_path = Path(distribution._path).resolve()
        if metadata_path.name.endswith(".egg-info"):
            install_origin = "source-tree"
        direct_url_text = distribution.read_text("direct_url.json")
        if direct_url_text:
            direct_url = json.loads(direct_url_text)
            install_origin = "editable" if direct_url.get("dir_info", {}).get("editable") else "direct-url"
        elif install_origin != "source-tree":
            install_origin = "site-packages"
    except importlib.metadata.PackageNotFoundError:
        pass
    source_commit = _source_commit(module_dir) or SOURCE_COMMIT
    build_hash = _build_hash(module_dir)
    return {
        "schema": "forgeline.provenance.v1",
        "package": "code-factory-2-forge",
        "version": __version__,
        "source_commit": source_commit,
        "build_hash": build_hash,
        "install_origin": install_origin,
        "direct_url": direct_url.get("url") if direct_url else None,
        "python": sys.version.split()[0],
        "runtime": {"python": sys.version.split()[0], "implementation": sys.implementation.name},
        "receipt_schema": "forge.receipt.v1",
        "identity_complete": bool(source_commit and build_hash),
    }
