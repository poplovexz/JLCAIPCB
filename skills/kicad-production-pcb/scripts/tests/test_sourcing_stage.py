#!/usr/bin/env python3
"""Focused regressions for deterministic sourcing, ranking, and part locks."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from _part_lock import apply_selections_to_spec, candidate_map, check_part_lock  # noqa: E402
from _pcb_skill_checks import CheckResult, get_path, load_spec  # noqa: E402
from _sourcing_stage import (  # noqa: E402
    architecture_maps,
    atomic_write_yaml,
    check_sourcing_context,
    effective_now,
    evaluate_candidate,
    evaluate_candidate_criterion,
    evaluate_candidate_manifest,
    evaluate_criterion,
    load_data_file,
    load_policy,
    required_order_quantity,
    resolved_path,
)


AS_OF = "2026-07-10T12:00:00Z"


def expect_lock_issue(name: str, spec: dict, expected: str, spec_path: Path) -> list[str]:
    result = CheckResult()
    check_part_lock(spec, spec_path, result, force=True, as_of=AS_OF)
    if any(expected in issue for issue in result.issues):
        print(f"{name}: PASS")
        return []
    return [f"{name}: expected issue containing {expected!r}; got {result.issues}"]


def expect_context_issue(name: str, spec: dict, expected: str) -> list[str]:
    result = CheckResult()
    check_sourcing_context(spec, result, force=True)
    if any(expected in issue for issue in result.issues):
        print(f"{name}: PASS")
        return []
    return [f"{name}: expected issue containing {expected!r}; got {result.issues}"]


def candidate_fixture(spec: dict) -> tuple[dict, dict, dict, dict, Path]:
    policy = load_policy(spec)
    manifest = load_data_file(resolved_path(get_path(spec, "sourcing.artifacts.candidates")))
    requirements = {
        str(item["id"]): item
        for item in spec["sourcing"]["requirements"]
    }
    candidates = candidate_map(manifest)
    requirement_id = next(iter(requirements))
    candidate = copy.deepcopy(next(value for (owner, _), value in candidates.items() if owner == requirement_id))
    _, constraints = architecture_maps(spec, policy)
    providers = {str(item["id"]): item for item in spec["sourcing"]["context"]["providers"]}
    return candidate, requirements[requirement_id], constraints, providers, resolved_path(spec["project"]["artifacts_dir"])


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: test_sourcing_stage.py <passing-part-selection-spec>", file=sys.stderr)
        return 2
    base = load_spec(Path(argv[1]))
    spec_path = Path(argv[1])
    policy = load_policy(base)
    failures: list[str] = []

    passing = CheckResult()
    check_part_lock(base, Path(argv[1]), passing, force=True, as_of=AS_OF)
    if passing.ok():
        print("complete part lock: PASS")
    else:
        failures.append(f"complete part lock should pass: {passing.issues}")

    component_mismatch = copy.deepcopy(base)
    component_mismatch["components"][0]["manufacturer"] = "Changed Manufacturer"
    failures.extend(expect_lock_issue("component differs from lock", component_mismatch, "does not match part lock", spec_path))

    stale_architecture = copy.deepcopy(base)
    stale_architecture["architecture"]["blocks"][0]["selection_constraints"][0]["criteria"][0]["value"] = "changed"
    failures.extend(expect_lock_issue("architecture invalidates candidates", stale_architecture, "architecture_sha256 is stale", spec_path))

    missing_criteria = copy.deepcopy(base)
    missing_criteria["architecture"]["blocks"][0]["selection_constraints"][0].pop("criteria")
    failures.extend(expect_context_issue("machine criteria required", missing_criteria, "machine-readable criteria"))

    unbounded_search = copy.deepcopy(base)
    unbounded_search["sourcing"]["context"]["max_search_rounds"] = 1000
    failures.extend(expect_context_issue("search rounds bounded", unbounded_search, "max_search_rounds must be between"))

    template_spec = copy.deepcopy(base)
    template_path = Path(__file__).resolve().parents[2] / "assets" / "sourcing-stage-template.yaml"
    template_spec["sourcing"] = load_data_file(template_path)["sourcing"]
    template_result = CheckResult()
    check_sourcing_context(template_spec, template_result, force=True)
    if template_result.issues:
        print("unfilled sourcing template rejected: PASS")
    else:
        failures.append("unfilled sourcing template must fail closed")

    unlocked_component = copy.deepcopy(base)
    unlocked_component["components"].append({"ref": "X1", "value": "Untracked procured item"})
    failures.extend(expect_context_issue("all procured components locked", unlocked_component, "has no sourcing requirement"))

    invalidated = copy.deepcopy(base)
    invalidated["sourcing"]["downstream_binding"]["status"] = "invalidated"
    failures.extend(expect_lock_issue("relock invalidates downstream", invalidated, "downstream binding is invalidated", spec_path))

    stale_lock_digest = copy.deepcopy(base)
    stale_lock_digest["sourcing"]["part_lock"]["sha256"] = "0" * 64
    failures.extend(expect_lock_issue("lock digest binding", stale_lock_digest, "part_lock.sha256 is missing or stale", spec_path))

    if required_order_quantity(100, 20, 0.1, 1, 1) == 2200:
        print("board quantity times per-board usage: PASS")
    else:
        failures.append("stock quantity calculation did not multiply board quantity and per-board usage")
    if required_order_quantity(100, 2, 0.1, 5, 5) == 220:
        print("decimal attrition rounding: PASS")
    else:
        failures.append("decimal attrition calculation must not add a binary floating-point unit")

    unit_passed, _ = evaluate_criterion(
        {"attribute": "voltage", "operator": "gte", "value": 3.3, "unit": "V"},
        {"value": 3300, "unit": "mV"},
        policy,
    )
    if unit_passed:
        print("configured unit conversion: PASS")
    else:
        failures.append("configured unit conversion should compare 3300 mV with 3.3 V")

    derated_passed, _ = evaluate_candidate_criterion(
        {
            "attribute": "rated_current",
            "operator": "gte",
            "value": 1.5,
            "unit": "A",
            "actual_multiplier": 0.8,
            "required_multiplier": 1.1,
        },
        {"rated_current": {"value": 2.1, "unit": "A"}},
        policy,
    )
    if derated_passed:
        print("decimal derating and margin: PASS")
    else:
        failures.append("explicit derating and required margin should be evaluated with decimal arithmetic")

    compound_passed, _ = evaluate_candidate_criterion(
        {
            "all_of": [
                {"attribute": "voltage_range", "operator": "range_contains", "value": {"min": 3, "max": 5}, "unit": "V"},
                {"attribute": "modes", "operator": "contains", "value": "active"},
            ]
        },
        {
            "voltage_range": {"min": 2.7, "max": 5.5, "unit": "V"},
            "modes": {"values": ["active", "sleep"]},
        },
        policy,
    )
    if compound_passed:
        print("compound and range criteria: PASS")
    else:
        failures.append("all_of and range_contains criteria should pass for a covering candidate")

    candidate, requirement, constraints, providers, artifacts_root = candidate_fixture(base)
    context = base["sourcing"]["context"]
    now = effective_now(AS_OF)

    zero_stock = copy.deepcopy(candidate)
    zero_stock["inventory"]["in_stock"] = 0
    zero_result = evaluate_candidate(zero_stock, requirement, constraints, providers, context, policy, now, artifacts_root)
    if not zero_result["qualified"] and any("below orderable requirement" in reason for reason in zero_result["reasons"]):
        print("zero stock remains zero: PASS")
    else:
        failures.append(f"zero stock candidate should fail: {zero_result}")

    bad_capability = copy.deepcopy(candidate)
    bad_capability["capabilities"]["functions"]["values"] = ["wrong_function"]
    capability_result = evaluate_candidate(bad_capability, requirement, constraints, providers, context, policy, now, artifacts_root)
    if not capability_result["qualified"] and any("does not satisfy" in reason for reason in capability_result["reasons"]):
        print("hard constraint rejection: PASS")
    else:
        failures.append(f"hard constraint mismatch should fail: {capability_result}")

    bad_evidence = copy.deepcopy(candidate)
    bad_evidence["evidence"][0]["sha256"] = "0" * 64
    evidence_result = evaluate_candidate(bad_evidence, requirement, constraints, providers, context, policy, now, artifacts_root)
    if not evidence_result["qualified"] and any("does not match" in reason for reason in evidence_result["reasons"]):
        print("evidence hash tamper rejection: PASS")
    else:
        failures.append(f"tampered evidence should fail: {evidence_result}")

    wrong_evidence_role = copy.deepcopy(candidate)
    wrong_evidence_role["inventory"]["evidence_id"] = wrong_evidence_role["lifecycle"]["evidence_id"]
    role_result = evaluate_candidate(
        wrong_evidence_role, requirement, constraints, providers, context, policy, now, artifacts_root
    )
    if not role_result["qualified"] and any("must reference supplier-snapshot" in reason for reason in role_result["reasons"]):
        print("evidence role binding: PASS")
    else:
        failures.append(f"wrong evidence role should fail: {role_result}")

    no_assembly_stock = copy.deepcopy(candidate)
    no_assembly_stock["assembly"]["in_stock"] = 0
    assembly_stock_result = evaluate_candidate(
        no_assembly_stock, requirement, constraints, providers, context, policy, now, artifacts_root
    )
    if not assembly_stock_result["qualified"] and any("assembly stock" in reason for reason in assembly_stock_result["reasons"]):
        print("assembly stock separated from supplier stock: PASS")
    else:
        failures.append(f"zero assembly stock should fail independently: {assembly_stock_result}")

    missing_lifecycle = copy.deepcopy(candidate)
    missing_lifecycle.pop("lifecycle")
    lifecycle_result = evaluate_candidate(
        missing_lifecycle, requirement, constraints, providers, context, policy, now, artifacts_root
    )
    if not lifecycle_result["qualified"] and any("lifecycle.status" in reason for reason in lifecycle_result["reasons"]):
        print("lifecycle qualification: PASS")
    else:
        failures.append(f"missing lifecycle should fail: {lifecycle_result}")

    placeholder = copy.deepcopy(candidate)
    placeholder["manufacturer"] = "Example Semiconductor"
    placeholder_result = evaluate_candidate(placeholder, requirement, constraints, providers, context, policy, now, artifacts_root)
    if not placeholder_result["qualified"] and any("forbidden placeholder" in reason for reason in placeholder_result["reasons"]):
        print("placeholder identity rejection: PASS")
    else:
        failures.append(f"placeholder identity should fail: {placeholder_result}")

    duplicate_spec = copy.deepcopy(base)
    duplicate_manifest = load_data_file(resolved_path(get_path(base, "sourcing.artifacts.candidates")))
    first_part = duplicate_manifest["batches"][0]["candidates"][0]["supplier"]["part_number"]
    duplicate_candidate = duplicate_manifest["batches"][1]["candidates"][0]
    duplicate_candidate["supplier"]["part_number"] = first_part
    duplicate_candidate["supplier"]["source_url"] = f"https://www.lcsc.com/product-detail/{first_part}.html"
    with tempfile.TemporaryDirectory(dir=artifacts_root) as temporary_dir:
        temporary_root = Path(temporary_dir)
        for evidence in duplicate_candidate["evidence"]:
            if evidence["kind"] == "supplier_snapshot":
                evidence["url"] = duplicate_candidate["supplier"]["source_url"]
                evidence["final_url"] = evidence["url"]
                evidence["assertions"]["supplier_part"] = first_part
            if evidence["kind"] == "assembly_snapshot":
                evidence["url"] = f"https://jlcpcb.com/parts/componentSearch?searchTxt={first_part}"
                evidence["final_url"] = evidence["url"]
                evidence["assertions"]["supplier_part"] = first_part
            if evidence["kind"] not in {"supplier_snapshot", "assembly_snapshot"}:
                continue
            source_data = json.loads(resolved_path(evidence["file"]).read_text(encoding="utf-8"))
            source_data["supplier_part"] = first_part
            evidence_path = temporary_root / f"{evidence['id'].lower()}.json"
            evidence_bytes = (json.dumps(source_data, separators=(",", ":")) + "\n").encode("utf-8")
            evidence_path.write_bytes(evidence_bytes)
            evidence["file"] = str(evidence_path)
            evidence["sha256"] = hashlib.sha256(evidence_bytes).hexdigest()
            evidence["byte_size"] = len(evidence_bytes)
        duplicate_path = Path(temporary_dir) / "candidates.yaml"
        atomic_write_yaml(duplicate_path, duplicate_manifest)
        duplicate_spec["sourcing"]["artifacts"]["candidates"] = str(duplicate_path)
        duplicate_result = CheckResult()
        evaluate_candidate_manifest(
            duplicate_spec, spec_path, duplicate_result, policy=policy, force=True, as_of=AS_OF
        )
    if any("is split across requirements" in issue for issue in duplicate_result.issues):
        print("duplicate supplier part requires quantity merge: PASS")
    else:
        failures.append(f"duplicate selected supplier parts should fail: {duplicate_result.issues}")

    relock_spec = copy.deepcopy(base)
    lock = load_data_file(resolved_path(get_path(base, "sourcing.artifacts.part_lock")))
    changed_lock = copy.deepcopy(lock)
    changed_lock["selections"][0]["candidate_id"] = "REPLACEMENT_CANDIDATE"
    manifest = load_data_file(resolved_path(get_path(base, "sourcing.artifacts.candidates")))
    selected_candidates = {
        selection["requirement_id"]: candidate_map(manifest)[(selection["requirement_id"], lock["selections"][index]["candidate_id"])]
        for index, selection in enumerate(changed_lock["selections"])
    }
    updated, changed = apply_selections_to_spec(relock_spec, changed_lock, selected_candidates, policy)
    if changed and get_path(updated, "sourcing.downstream_binding.status") == "invalidated":
        print("relock downstream invalidation: PASS")
    else:
        failures.append("changing a selected candidate must invalidate downstream bindings")

    if failures:
        for failure in failures:
            print(f"ISSUE: {failure}")
        return 1
    print("sourcing-stage regressions: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
