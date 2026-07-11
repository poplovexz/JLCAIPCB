#!/usr/bin/env python3
"""Validate, score, and transactionally apply one whole placement batch."""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _layout_stage import board_items, board_snapshot, check_contract, run_after_generation  # noqa: E402
from _package_binding_stage import artifacts_root, ensure_artifact, load_data, mapping, project_root, resolve, sequence, strings  # noqa: E402
from _part_lock import replace_files_transactionally, roundtrip_spec_bytes  # noqa: E402
from _pcb_skill_checks import CheckResult, get_path, load_spec, print_result, sha256_file, string_value  # noqa: E402


def placement_score(spec: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, float]:
    positions = mapping(snapshot.get("footprints"))
    net_refs: dict[str, set[str]] = {}
    for component in sequence(spec.get("components")):
        if not isinstance(component, dict) or not string_value(component.get("ref")):
            continue
        for net in mapping(component.get("pads")).values():
            if string_value(net):
                net_refs.setdefault(str(net), set()).add(str(component["ref"]))
    total = 0.0
    for refs in net_refs.values():
        remaining = set(refs)
        if len(remaining) < 2:
            continue
        connected = {remaining.pop()}
        while remaining:
            distance, selected = min(
                (
                    ((float(positions[a]["x"]) - float(positions[b]["x"])) ** 2 + (float(positions[a]["y"]) - float(positions[b]["y"])) ** 2) ** 0.5,
                    b,
                )
                for a in connected
                for b in remaining
            )
            total += distance
            connected.add(selected)
            remaining.remove(selected)
    return {"estimated_ratsnest_mst_mm": round(total, 6), "score": round(total, 6)}


def prepare_candidate(
    spec: dict[str, Any],
    spec_path: Path,
    candidate_path: Path,
    generator: Path,
    result: CheckResult,
) -> tuple[dict[str, Any], Path | None, Path | None]:
    root = project_root(spec, spec_path)
    artifacts = artifacts_root(spec, root)
    candidate_path = candidate_path.resolve()
    ensure_artifact(candidate_path, artifacts, result, "layout candidate manifest")
    if not candidate_path.is_file():
        result.issue(f"layout candidate manifest does not exist: {candidate_path}")
        return spec, None, None
    candidate = load_data(candidate_path)
    for field in ["schema_version", "project_name", "candidate_id", "batch_id", "base_spec_sha256", "layout_revision", "placements"]:
        if candidate.get(field) in (None, "", []):
            result.issue(f"layout candidate {field} is required")
    if candidate.get("schema_version") != 1:
        result.issue("layout candidate schema_version is unsupported")
    if candidate.get("project_name") != get_path(spec, "project.name"):
        result.issue("layout candidate project_name does not match the Spec")
    if candidate.get("base_spec_sha256") != sha256_file(spec_path):
        result.issue("layout candidate base_spec_sha256 is stale")
    if candidate.get("layout_revision") != get_path(spec, "layout.revision"):
        result.issue("layout candidate layout_revision is stale")
    batches = {
        str(item.get("id")): item
        for item in sequence(get_path(spec, "layout.placement_batches"))
        if isinstance(item, dict) and string_value(item.get("id"))
    }
    batch = mapping(batches.get(str(candidate.get("batch_id"))))
    if not batch:
        result.issue("layout candidate batch_id is not declared")
    allowed_refs = set(strings(batch.get("refs")))
    placements = sequence(candidate.get("placements"))
    placement_refs: set[str] = set()
    for index, raw in enumerate(placements):
        item = mapping(raw)
        ref = str(item.get("ref", ""))
        if ref not in allowed_refs:
            result.issue(f"layout candidate placements[{index}] updates ref outside batch: {ref}")
        if ref in placement_refs:
            result.issue(f"layout candidate duplicates placement ref {ref}")
        placement_refs.add(ref)
        position = mapping(item.get("position_mm"))
        for field in ["x", "y", "rotation"]:
            if not isinstance(position.get(field), (int, float)) or isinstance(position.get(field), bool):
                result.issue(f"layout candidate {ref}.position_mm.{field} must be numeric")
        if str(position.get("side", "")) not in {"top", "bottom"}:
            result.issue(f"layout candidate {ref}.position_mm.side must be top or bottom")
    if placement_refs != allowed_refs:
        result.issue("layout candidate must update the complete declared placement batch")
    if not generator.is_file():
        result.issue(f"layout candidate generator is missing: {generator}")
    if not result.ok():
        return spec, None, None

    updated = copy.deepcopy(spec)
    components = {str(item.get("ref")): item for item in board_items(updated)}
    for item in placements:
        components[str(item["ref"])]["position_mm"] = copy.deepcopy(item["position_mm"])
    candidate_root = artifacts / "layout-candidates" / str(get_path(spec, "project.name")) / str(candidate["batch_id"]) / str(candidate["candidate_id"])
    candidate_project = candidate_root / "project"
    candidate_artifacts = candidate_root / "artifacts"
    candidate_spec = candidate_root / "candidate-spec.yaml"
    updated["project"]["root_dir"] = str(root)
    updated["project"]["output_dir"] = str(candidate_project)
    updated["project"]["artifacts_dir"] = str(candidate_artifacts)
    candidate_root.mkdir(parents=True, exist_ok=True)
    candidate_spec.write_text(yaml.safe_dump(updated, sort_keys=False), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(generator), "--layout-only", str(candidate_spec)],
        cwd=root,
        text=True,
        capture_output=True,
    )
    if completed.returncode:
        result.issue("layout candidate generation failed: " + " | ".join((completed.stdout + completed.stderr).splitlines()[-8:]))
        return updated, candidate_spec, None
    contract_result = CheckResult()
    check_contract(updated, candidate_spec, contract_result, force=True)
    run_after_generation(updated, candidate_spec, generator, contract_result, force=True)
    result.issues.extend(contract_result.issues)
    result.warnings.extend(contract_result.warnings)
    board_path = candidate_project / f"{get_path(spec, 'project.name')}.kicad_pcb"
    snapshot = board_snapshot(board_path, result)
    metrics = placement_score(updated, snapshot) if result.ok() else {}
    report_path = candidate_root / "candidate-report.json"
    report = {
        "check": "layout_batch_transaction",
        "ok": result.ok(),
        "candidate_manifest": {"path": str(candidate_path), "sha256": sha256_file(candidate_path)},
        "base_spec_sha256": sha256_file(spec_path),
        "batch_id": candidate.get("batch_id"),
        "candidate_id": candidate.get("candidate_id"),
        "metrics": metrics,
        "issues": result.issues,
        "warnings": result.warnings,
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if result.ok():
        selected = updated.setdefault("layout", {}).setdefault("selected_candidates", {})
        selected[str(candidate["batch_id"])] = {
            "candidate_id": candidate["candidate_id"],
            "manifest": {"path": str(candidate_path), "sha256": sha256_file(candidate_path)},
            "report": {"path": str(report_path), "sha256": sha256_file(report_path)},
            "score": metrics["score"],
        }
        updated["project"]["root_dir"] = get_path(spec, "project.root_dir")
        updated["project"]["output_dir"] = get_path(spec, "project.output_dir")
        updated["project"]["artifacts_dir"] = get_path(spec, "project.artifacts_dir")
    return updated, candidate_spec, report_path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Validate and transactionally apply one complete layout placement batch.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--generator", type=Path, default=Path("scripts/generate_project.py"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])
    result = CheckResult()
    report: Path | None = None
    applied = False
    try:
        spec = load_spec(args.spec)
        updated, _, report = prepare_candidate(spec, args.spec, args.candidate, args.generator.resolve(), result)
        if result.ok() and args.apply:
            journal = artifacts_root(spec, project_root(spec, args.spec)) / "layout-candidates" / str(get_path(spec, "project.name")) / "apply-transaction.json"
            replace_files_transactionally([(args.spec, roundtrip_spec_bytes(args.spec, updated))], journal)
            applied = True
    except Exception as error:
        result.issue(str(error))
    payload = {"check": "layout_batch_transaction", "ok": result.ok(), "applied": applied, "report": str(report) if report else None, "issues": result.issues, "warnings": result.warnings}
    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if result.ok() else 1
    code = print_result("layout_batch_transaction", result, False)
    if result.ok():
        print("layout batch applied; rerun Spec Freeze" if applied else "layout batch candidate passed; active Spec unchanged")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
