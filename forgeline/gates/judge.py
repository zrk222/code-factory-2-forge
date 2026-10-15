"""Judge Agent — directory & interface consistency (executable, LLM-free).
Checks the filled code matches the SSAT scaffold: no stubs left, every
declared function implemented, imports resolve within the tree."""
from __future__ import annotations
import hashlib, json
from pathlib import Path
import yaml
from ..ssat import check_erosion
from ..source_scope import analyze_source

def _jev_file(root: Path, path: Path, total: int) -> tuple[dict, bytes, int]:
    if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != root.parent) or not path.is_file():
        raise ValueError("FORGE_CONTEXT_INVALID: unsafe evidence path")
    path = path.resolve()
    try: relative = path.relative_to(root)
    except ValueError as error: raise ValueError("FORGE_CONTEXT_INVALID: evidence path escapes root") from error
    data = path.read_bytes(); total += len(data)
    if total > 256000: raise ValueError("FORGE_CONTEXT_INVALID: evidence exceeds byte limit")
    return {"path": relative.as_posix(), "sha256": hashlib.sha256(data).hexdigest()}, data, total

def _jev_claims(criteria: list, ids: set) -> list:
    claims = []
    for item in criteria:
        refs = item.get("evidence_ids") if isinstance(item, dict) else None
        if (not isinstance(item, dict) or set(item) != {"id", "statement", "evidence_ids"} or
                not all(isinstance(item.get(k), str) and item[k].strip() for k in ("id", "statement")) or
                not isinstance(refs, list) or not refs or not all(isinstance(x, str) for x in refs) or
                len(refs) != len(set(refs)) or not set(refs).issubset(ids) or len(item["statement"]) > 500):
            raise ValueError("FORGE_CONTEXT_INVALID: criterion evidence refs invalid")
        claims.append({"id": item["id"], "statement": item["statement"], "supporting_evidence_ids": refs + ["forgeline-context"], "contradicting_evidence_ids": [], "status": "unresolved"})
    return claims

def _jev_material_fields(material: dict) -> tuple:
    allowed = {"profile", "criteria", "evidence", "omissions", "rubric_version"}
    if not isinstance(material, dict) or set(material) - allowed:
        raise ValueError("FORGE_CONTEXT_INVALID: material fields invalid")
    raw = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    if len(raw) > 256000: raise ValueError("FORGE_CONTEXT_INVALID: material exceeds byte limit")
    profile = material.get("profile")
    if not isinstance(profile, str) or profile not in {"requirements", "implementation", "test_evidence", "repair"}:
        raise ValueError("FORGE_CONTEXT_INVALID: unsupported profile")
    criteria, evidence, omissions = material.get("criteria"), material.get("evidence", []), material.get("omissions", [])
    rubric = material.get("rubric_version", "forge-jev-review-v1")
    if (not isinstance(rubric, str) or not rubric.strip() or len(rubric) > 100 or
            not isinstance(criteria, list) or not criteria or len(criteria) > 128 or not isinstance(evidence, list) or len(evidence) > 512 or
            not isinstance(omissions, list) or len(omissions) > 128 or not all(isinstance(x, str) and x.strip() and len(x) <= 500 for x in omissions)):
        raise ValueError("FORGE_CONTEXT_INVALID: material bounds invalid")
    return raw, profile, criteria, evidence, omissions, rubric

def _jev_manifest(root: Path, ssat_path: Path, raw: bytes) -> tuple[list, int]:
    ssat = Path(ssat_path); ssat = ssat if ssat.is_absolute() else root / ssat
    modules = yaml.safe_load(ssat.read_text(encoding="utf-8")).get("modules", [])
    paths = sorted({ssat, *(root / module["path"] for module in modules)})
    manifest, total = [], len(raw)
    for path in paths:
        row, _, total = _jev_file(root, path, total); manifest.append(row)

    return manifest, total

def _jev_evidence_rows(root: Path, evidence: list, manifest: list, total: int) -> tuple[list, set]:
    rows, ids = [], set()
    for item in evidence:
        if (not isinstance(item, dict) or set(item) != {"id", "path", "kind"} or
                not all(isinstance(item.get(k), str) and item[k] for k in ("id", "path", "kind")) or item["id"] in ids):
            raise ValueError("FORGE_CONTEXT_INVALID: evidence row invalid")
        ids.add(item["id"]); row, data, total = _jev_file(root, root / item["path"], total); manifest.append(row)
        try: content = data.decode("utf-8")
        except UnicodeDecodeError as error: raise ValueError("FORGE_CONTEXT_INVALID: evidence must be UTF-8") from error
        rows.append({"id": item["id"], "kind": item["kind"], "epistemic_status": "observed", "provenance": row | {"candidate_sha256": "", "content": content}})
    manifest.sort(key=lambda r: (r["path"], r["sha256"]))
    return rows, ids

def _jev_packet(deterministic: dict, fields: tuple, manifest: list,
                rows: list, ids: set) -> dict:
    from factoryline.analysis_evidence import REVIEW_EVIDENCE_SCHEMA, validate_review_evidence
    _, profile, criteria, _, omissions, rubric = fields
    candidate = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    for row in rows: row["provenance"]["candidate_sha256"] = candidate
    claims = _jev_claims(criteria, ids)
    review_material = {"criteria": criteria, "evidence": rows, "omissions": omissions}
    provenance = {"schema": "forge.jev-context.v1", "profile": profile, "candidate_sha256": candidate,
        "rubric_version": rubric, "deterministic_result": deterministic,
        "omissions": omissions, "source_manifest": manifest, "review_material": review_material,
        "material_sha256": hashlib.sha256(json.dumps(review_material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
    receipt = hashlib.sha256(json.dumps(provenance, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    core = {"schema": REVIEW_EVIDENCE_SCHEMA, "candidate_sha256": candidate, "receipt_sha256": receipt,
        "evidence": rows + [{"id": "forgeline-context", "kind": "forgeline_context", "epistemic_status": "observed", "provenance": provenance}],
        "claims": claims, "unknowns": [{"id": f"forge-omission-{i}", "verification_action": x} for i, x in enumerate(omissions)], "authority": "none"}
    packet = core | {"packet_sha256": hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
    return validate_review_evidence(packet)

def build_jev_review_packet(root: Path, ssat_path: Path, material: dict, deterministic: dict) -> dict:
    """Build a bounded standard CF packet from explicit local material."""
    root = Path(root).resolve()
    fields = _jev_material_fields(material)
    manifest, total = _jev_manifest(root, ssat_path, fields[0])
    rows, ids = _jev_evidence_rows(root, fields[3], manifest, total)
    return _jev_packet(deterministic, fields, manifest, rows, ids)

def jev_snapshot(root: Path, packet: dict) -> str:
    root = Path(root).resolve(); current = []; total = 0
    context = next(x for x in packet["evidence"] if x["kind"] == "forgeline_context")
    for item in context["provenance"]["source_manifest"]:
        path = root / item["path"]; row, _, total = _jev_file(root, path, total)
        if row["sha256"] != item["sha256"]: raise ValueError("JEV_REVIEW_STALE: source changed")
        current.append(row)
    data = json.dumps([current, packet], sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()

def valid_jev_advisory(result: dict, packet: dict) -> bool:
    context = next(x["provenance"] for x in packet["evidence"] if x["kind"] == "forgeline_context")
    return (isinstance(result, dict) and result.get("status") in {"ADVISORY", "FALLBACK"} and
            result.get("authority") == "none" and result.get("release_decision") is False and
            result.get("candidate_sha256") == packet["candidate_sha256"] and
            result.get("receipt_sha256") == packet["receipt_sha256"] and
            result.get("packet_sha256") == packet["packet_sha256"] and
            result.get("profile") == context["profile"] and
            result.get("deterministic_result") == context["deterministic_result"])

def judge_consistency(
    ssat: dict, src_dir: Path, *, erosion: list | None = None
) -> tuple[bool, list[str]]:
    findings = []
    src_dir = Path(src_dir)
    for mod in ssat.get("modules", []):
        p = src_dir/mod["path"]
        if not p.exists():
            findings.append(f"J_MISSING {mod['path']}"); continue
        parsed = analyze_source(p, src_dir)
        text = parsed["text"]
        if "raise NotImplementedError" in text or "# FILL" in text:
            findings.append(f"J_STUB unfilled body in {mod['path']}")
        if parsed["status"] == "syntax_error":
            findings.append(f"J_SYNTAX {mod['path']}: {parsed['error']}")
        elif parsed["status"] == "parser_unsupported":
            findings.append(f"J_PARSER_UNSUPPORTED {mod['path']}: {parsed.get('reason', parsed['language'])}")
    # structural erosion is a judge concern too
    erosion = check_erosion(ssat, src_dir) if erosion is None else erosion
    for v in erosion:
        findings.append(f"J_ARCH {v.code} {v.message}")
    return (len(findings) == 0, findings)
