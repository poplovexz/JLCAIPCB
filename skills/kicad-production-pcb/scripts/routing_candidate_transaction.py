#!/usr/bin/env python3
"""Hard-gate and transactionally lock one complete routing-batch candidate."""

from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _layout_stage import check_evidence as check_layout_evidence  # noqa: E402
from _package_binding_stage import artifacts_root, ensure_artifact, mapping, project_root, resolve, sequence, strings  # noqa: E402
from _part_lock import replace_files_transactionally, roundtrip_spec_bytes  # noqa: E402
from _pcb_skill_checks import CheckResult, get_path, load_spec, print_result, sha256_file, string_value  # noqa: E402
from _routing_stage import check_contract, check_route_constraints, invariant_snapshot, load_policy, route_snapshot  # noqa: E402


def select_batch(spec: dict[str, Any], identifier: str) -> dict[str, Any]:
    return next((item for item in sequence(get_path(spec, "routing.batches")) if isinstance(item, dict) and item.get("id") == identifier), {})


def tracks_by_net(snapshot: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for record in sequence(snapshot.get("tracks")):
        if isinstance(record, dict):
            result.setdefault(str(record.get("net", "")), []).append(record)
    return result


NET_IN_ITEM_PATTERN = re.compile(r"\[([^\]]+)\]")


def unexpected_unconnected_nets(unconnected: list[Any], allowed_unconnected_nets: set[str]) -> set[str]:
    unexpected: set[str] = set()
    for violation in unconnected:
        descriptions = [str(item.get("description", "")) for item in sequence(violation.get("items")) if isinstance(item, dict)] if isinstance(violation, dict) else []
        nets = {match.group(1) for description in descriptions for match in NET_IN_ITEM_PATTERN.finditer(description)}
        if not nets or not nets <= allowed_unconnected_nets:
            unexpected.update(nets or {"<unknown>"})
    return unexpected


def run_drc(candidate: Path, report: Path, result: CheckResult, allowed_unconnected_nets: set[str]) -> dict[str, Any]:
    report.parent.mkdir(parents=True, exist_ok=True)
    command = ["kicad-cli", "pcb", "drc", "--format", "json", "--severity-all", "--refill-zones", "--output", str(report), str(candidate)]
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode or not report.is_file():
        result.issue(f"candidate DRC execution failed with exit {completed.returncode}")
        return {"command": command, "exit_code": completed.returncode}
    payload = json.loads(report.read_text(encoding="utf-8"))
    violations = sequence(payload.get("violations")) + sequence(payload.get("schematic_parity"))
    unconnected = sequence(payload.get("unconnected_items"))
    if violations:
        result.issue(f"candidate DRC has {len(violations)} non-connectivity violation(s)")
    unexpected = unexpected_unconnected_nets(unconnected, allowed_unconnected_nets)
    if unexpected:
        result.issue("routing candidate has unconnected items outside remaining planned batches: " + ", ".join(sorted(unexpected)))
    return {"command": command, "exit_code": completed.returncode, "report": str(report), "violations": len(violations), "unconnected_items": len(unconnected), "allowed_unconnected_nets": sorted(allowed_unconnected_nets), "unexpected_unconnected_nets": sorted(unexpected)}


def candidate_score(snapshot: dict[str, Any], spec: dict[str, Any]) -> float:
    weights = {**mapping(load_policy().get("score_weights")), **mapping(get_path(spec, "validation.routing_stage.score_weights"))}
    layers = sum(max(len(strings(item.get("layers"))) - 1, 0) for item in mapping(snapshot.get("metrics_by_net")).values() if isinstance(item, dict))
    return round(float(weights.get("via", 0)) * int(snapshot.get("via_count", 0)) + float(weights.get("segment_mm", 0)) * float(snapshot.get("total_length_mm", 0)) + float(weights.get("layer_change", 0)) * layers, 6)


def relative_or_absolute(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path.resolve())


def prepare(spec: dict[str, Any], spec_path: Path, manifest_path: Path, result: CheckResult) -> tuple[dict[str, Any], Path | None, dict[str, Any]]:
    details = check_contract(spec, spec_path, result, True)
    root = project_root(spec, spec_path)
    artifacts = artifacts_root(spec, root)
    manifest_path = manifest_path.resolve()
    ensure_artifact(manifest_path, artifacts, result, "routing candidate manifest")
    if not manifest_path.is_file():
        result.issue(f"routing candidate manifest does not exist: {manifest_path}")
        return spec, None, details
    candidate = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    for field in ["schema_version", "project_name", "candidate_id", "batch_id", "base_spec_sha256", "routing_revision", "candidate_pcb", "generator", "tool_version", "command_evidence", "command_evidence_sha256"]:
        if candidate.get(field) in (None, ""):
            result.issue(f"routing candidate {field} is required")
    if candidate.get("schema_version") != 1 or candidate.get("project_name") != get_path(spec, "project.name"):
        result.issue("routing candidate schema/project does not match the Spec")
    if candidate.get("base_spec_sha256") != sha256_file(spec_path) or candidate.get("routing_revision") != get_path(spec, "routing.revision"):
        result.issue("routing candidate binds a stale Spec or routing revision")
    batch = select_batch(spec, str(candidate.get("batch_id", "")))
    if not batch:
        result.issue("routing candidate batch_id is not declared")
    candidate_pcb = resolve(root, candidate.get("candidate_pcb"))
    command_evidence = resolve(root, candidate.get("command_evidence"))
    ensure_artifact(candidate_pcb, artifacts, result, "routing candidate PCB")
    ensure_artifact(command_evidence, artifacts, result, "routing command evidence")
    if not candidate_pcb.is_file() or not command_evidence.is_file():
        result.issue("routing candidate PCB and command evidence must exist")
    elif candidate.get("command_evidence_sha256") != sha256_file(command_evidence):
        result.issue("routing candidate command evidence hash is stale")
    else:
        command_record = json.loads(command_evidence.read_text(encoding="utf-8"))
        if command_record.get("exit_code") != 0 or command_record.get("import_exit_code") != 0:
            result.issue("routing candidate command evidence does not prove successful route and import commands")
    layout_result = CheckResult()
    check_layout_evidence(spec, spec_path, layout_result, True)
    result.issues.extend(layout_result.issues)
    result.warnings.extend(layout_result.warnings)
    baseline = Path(details["board"])
    if not baseline.is_file():
        result.issue("accepted baseline PCB is missing")
    if not result.ok():
        return spec, None, details

    baseline_routes = route_snapshot(baseline, result)
    candidate_routes = route_snapshot(candidate_pcb, result)
    if invariant_snapshot(baseline, result) != invariant_snapshot(candidate_pcb, result):
        result.issue("routing candidate changed layout, board geometry, footprints, zones, or keepouts")
    allowed = set(strings(batch.get("nets")))
    before_by_net, after_by_net = tracks_by_net(baseline_routes), tracks_by_net(candidate_routes)
    for net in (set(before_by_net) | set(after_by_net)) - allowed:
        if before_by_net.get(net, []) != after_by_net.get(net, []):
            result.issue(f"routing candidate changed net outside selected batch: {net}")
    selected_spec = copy.deepcopy(spec)
    all_constraints = mapping(get_path(spec, "routing.net_constraints"))
    selected_spec["routing"]["net_constraints"] = {net: copy.deepcopy(all_constraints.get(net)) for net in allowed}
    check_route_constraints(selected_spec, candidate_routes, result)
    remaining = [item for item in sequence(get_path(spec, "routing.batches")) if isinstance(item, dict) and item.get("id") != batch.get("id") and str(item.get("state")) == "planned"]
    allowed_unconnected = {net for item in remaining for net in strings(item.get("nets"))}
    drc = run_drc(candidate_pcb, artifacts / "routing-candidates" / str(get_path(spec, "project.name")) / str(candidate.get("candidate_id")) / "drc.json", result, allowed_unconnected)
    score = candidate_score(candidate_routes, spec)
    details.update({"candidate_pcb": str(candidate_pcb), "batch_id": batch.get("id"), "candidate_id": candidate.get("candidate_id"), "final_batch": not remaining, "drc": drc, "score": score})
    if not result.ok():
        return spec, None, details

    lock_path = artifacts / "routing-lock" / str(get_path(spec, "project.name")) / f"routing-r{get_path(spec, 'routing.revision')}.json"
    lock_payload = {"schema_version": 1, "project_name": get_path(spec, "project.name"), "routing_revision": get_path(spec, "routing.revision"), "base_spec_sha256": sha256_file(spec_path), "candidate": {"manifest": relative_or_absolute(manifest_path, root), "sha256": sha256_file(manifest_path), "pcb_sha256": sha256_file(candidate_pcb)}, "layout_fingerprint": invariant_snapshot(candidate_pcb, result), "tracks": candidate_routes["tracks"]}
    lock_bytes = (json.dumps(lock_payload, indent=2, sort_keys=True) + "\n").encode()
    updated = copy.deepcopy(spec)
    for item in updated["routing"]["batches"]:
        if item.get("id") == batch.get("id"):
            item["state"] = "locked"
    updated["routing"]["route_lock"] = {"schema_version": 1, "routing_revision": get_path(spec, "routing.revision"), "artifact": {"path": relative_or_absolute(lock_path, root), "sha256": "pending-transaction"}, "selected_candidate": {"id": candidate.get("candidate_id"), "batch_id": batch.get("id"), "score": score}}
    return updated, lock_path, {**details, "lock_bytes": lock_bytes}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Validate and transactionally apply one complete routing batch candidate.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])
    result = CheckResult()
    applied = False
    details: dict[str, Any] = {}
    try:
        spec = load_spec(args.spec)
        updated, lock_path, details = prepare(spec, args.spec, args.candidate, result)
        if result.ok() and lock_path and args.apply:
            lock_bytes = details.pop("lock_bytes")
            import hashlib
            updated["routing"]["route_lock"]["artifact"]["sha256"] = hashlib.sha256(lock_bytes).hexdigest()
            journal = artifacts_root(spec, project_root(spec, args.spec)) / "routing-lock" / str(get_path(spec, "project.name")) / "apply-transaction.json"
            replace_files_transactionally([(lock_path, lock_bytes), (args.spec, roundtrip_spec_bytes(args.spec, updated))], journal)
            applied = True
        else:
            details.pop("lock_bytes", None)
    except Exception as error:
        result.issue(str(error))
    payload = {"check": "routing_candidate_transaction", "ok": result.ok(), "applied": applied, "issues": result.issues, "warnings": result.warnings, "details": details}
    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if result.ok() else 1
    code = print_result("routing_candidate_transaction", result, False)
    if result.ok():
        print("routing batch locked; rerun Spec Freeze" if applied else "routing candidate passed; active Spec unchanged")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
