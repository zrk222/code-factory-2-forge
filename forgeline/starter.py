"""`forge new <feature>`: a minimal SSAT and a smoke manifest that cannot pass until a person edits them."""
from __future__ import annotations

import json
import re
from pathlib import Path

PLACEHOLDER_KEY = "starter_placeholder"

SSAT = """# SSAT for {feature}: architecture as code. Edit the modules, then `forge expand {feature}` and
# `forge gate architected {feature} {feature}.ssat.yaml` to approve these exact bytes.
name: {feature}
modules:
  - name: core
    path: slices/{module}/core.py
    imports: []
    functions:
      - name: run
        args: ["value: str"]
        returns: "str"
        doc: "Replace with what this function does for {feature}."
dependencies: []
invariants:
  - name: no_eval
    forbid_pattern: "\\\\beval\\\\("
    scopes:
      - module: core
        symbol: run
        max_lines: 40
"""


def _manifest(feature: str, module: str) -> dict:
    return {
        PLACEHOLDER_KEY: f"Replace the check below with a real behavioral assertion, then delete this key. "
                         f"verify-tests and smoke refuse this manifest while it is present.",
        "checks": [{
            "name": "core_runtime",
            "kind": "python",
            "run": (f"import sys; sys.path.insert(0, '.')\n"
                    f"from slices.{module}.core import run\n"
                    f"raise SystemExit('edit smoke/{feature}.json: assert what run() must return')\n"),
            "expect_exit": 0,
        }],
    }


def new_feature(root: Path, feature: str) -> dict:
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", feature) is None:
        raise ValueError("feature must start with a letter and use letters, digits and underscores "
                         "(it becomes a Python package name)")
    root = Path(root)
    ssat = root / f"{feature}.ssat.yaml"
    manifest = root / "smoke" / f"{feature}.json"
    existing = [path for path in (ssat, manifest) if path.exists()]
    if existing:
        raise FileExistsError("refusing to overwrite: " + ", ".join(p.relative_to(root).as_posix() for p in existing))
    (root / ".forge").mkdir(exist_ok=True)
    manifest.parent.mkdir(exist_ok=True)
    module = feature.lower()
    ssat.write_text(SSAT.format(feature=feature, module=module), encoding="utf-8")
    manifest.write_text(json.dumps(_manifest(feature, module), indent=2) + "\n", encoding="utf-8")
    return {"created": [ssat.relative_to(root).as_posix(), manifest.relative_to(root).as_posix()],
            "next": f"edit both files, then forge expand {feature} --root ."}
