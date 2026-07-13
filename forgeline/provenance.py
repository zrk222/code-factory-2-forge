"""Installed-package provenance for reproducible ForgeLine receipts."""
from __future__ import annotations

import importlib.metadata
import json
import sys
from pathlib import Path

from . import __version__


def provenance() -> dict:
    """Return only facts available from the active installed distribution."""
    install_origin = "unknown"
    direct_url: dict | None = None
    try:
        distribution = importlib.metadata.distribution("code-factory-2-forge")
        metadata_path = Path(distribution._path).resolve()
        if metadata_path.name.endswith(".egg-info"):
            install_origin = "source-tree"
        installed_module = Path(distribution.locate_file("forgeline")).resolve()
        if installed_module != Path(__file__).resolve().parent:
            return {
                "schema": "forgeline.provenance.v1",
                "package": "code-factory-2-forge",
                "version": __version__,
                "source_commit": None,
                "build_hash": None,
                "install_origin": "source-tree",
                "direct_url": None,
                "python": sys.version.split()[0],
                "receipt_schema": "forge.receipt.v1",
            }
        direct_url_text = distribution.read_text("direct_url.json")
        if direct_url_text:
            direct_url = json.loads(direct_url_text)
            install_origin = "editable" if direct_url.get("dir_info", {}).get("editable") else "direct-url"
        elif install_origin != "source-tree":
            install_origin = "site-packages"
    except importlib.metadata.PackageNotFoundError:
        pass
    return {
        "schema": "forgeline.provenance.v1",
        "package": "code-factory-2-forge",
        "version": __version__,
        "source_commit": None,
        "build_hash": None,
        "install_origin": install_origin,
        "direct_url": direct_url.get("url") if direct_url else None,
        "python": sys.version.split()[0],
        "receipt_schema": "forge.receipt.v1",
    }
