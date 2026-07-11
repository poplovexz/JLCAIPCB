#!/usr/bin/env python3
"""Adversarial regressions for production sourcing trust and stage boundaries."""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from _part_lock import check_part_lock, recover_transaction, roundtrip_spec_bytes  # noqa: E402
from _pcb_skill_checks import CheckResult, get_path, load_spec, sha256_file  # noqa: E402
from _sourcing_stage import (  # noqa: E402
    architecture_maps,
    atomic_write_yaml,
    check_sourcing_context,
    effective_now,
    evaluate_candidate,
    load_data_file,
    load_policy,
    project_root,
    resolved_path,
)
from part_selection_check import (  # noqa: E402
    check_part_selection,
    is_production_candidate,
    load_policy as load_selection_policy,
)
from library_binding_transaction import prepare_binding  # noqa: E402
from architecture_gate import check_architecture  # noqa: E402
from flow_state_gate import check_flow_state  # noqa: E402
from requirement_intake_gate import check_requirement_intake  # noqa: E402
from _pcb_skill_checks import check_connectivity_batches  # noqa: E402


AS_OF = "2026-07-10T12:00:00Z"


def expect_issue(name: str, issues: list[str], expected: str) -> list[str]:
    if any(expected in issue for issue in issues):
        print(f"{name}: PASS")
        return []
    return [f"{name}: expected issue containing {expected!r}; got {issues}"]


def candidate_fixture(spec: dict) -> tuple[dict, dict, dict, dict, dict, Path]:
    policy = load_policy(spec)
    manifest = load_data_file(resolved_path(get_path(spec, "sourcing.artifacts.candidates")))
    requirement = spec["sourcing"]["requirements"][0]
    candidate = copy.deepcopy(manifest["batches"][0]["candidates"][0])
    _, constraints = architecture_maps(spec, policy)
    providers = {
        str(item["id"]): item for item in spec["sourcing"]["context"]["providers"]
    }
    context = spec["sourcing"]["context"]
    artifacts_root = resolved_path(spec["project"]["artifacts_dir"])
    return candidate, requirement, constraints, providers, context, artifacts_root


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: test_sourcing_adversarial.py <passing-part-selection-spec>", file=sys.stderr)
        return 2

    os.environ["KICAD_PCB_TEST_MODE"] = "1"
    base = load_spec(Path(argv[1]))
    failures: list[str] = []

    tampered = copy.deepcopy(base)
    tampered["sourcing"]["part_lock"]["path"] = "artifacts/not-the-locked-file.yaml"
    tampered["sourcing"]["part_lock"]["locked_at"] = "1999-01-01T00:00:00Z"
    tampered["sourcing"]["part_lock"]["selections"][0]["candidate_id"] = "BOGUS_SELECTION"
    lock_result = CheckResult()
    check_part_lock(tampered, Path(argv[1]), lock_result, force=True, as_of=AS_OF)
    failures.extend(expect_issue("tampered lock summary rejected", lock_result.issues, "metadata"))

    pending = copy.deepcopy(base)
    pending["sourcing"]["downstream_binding"]["status"] = "pending"
    pending_result = CheckResult()
    try:
        check_part_lock(
            pending,
            Path(argv[1]),
            pending_result,
            force=True,
            as_of=AS_OF,
            before_generation=True,
        )
    except TypeError as error:
        failures.append(f"pending downstream rejected before generation: missing gate API: {error}")
    else:
        failures.extend(
            expect_issue("pending downstream rejected before generation", pending_result.issues, "downstream binding")
        )

    rewind = copy.deepcopy(base)
    rewind.setdefault("validation", {}).setdefault("part_selection", {}).pop("allow_test_clock", None)
    rewind_result = CheckResult()
    from _sourcing_stage import evaluate_candidate_manifest  # noqa: PLC0415

    evaluate_candidate_manifest(rewind, Path(argv[1]), rewind_result, force=True, as_of=AS_OF)
    failures.extend(expect_issue("production clock rewind rejected", rewind_result.issues, "test clock"))

    override = copy.deepcopy(base)
    override.setdefault("validation", {}).setdefault("part_selection", {})[
        "sourcing_policy_file"
    ] = "assets/sourcing-stage-policy.yaml"
    override_result = CheckResult()
    check_sourcing_context(override, override_result, force=True)
    failures.extend(expect_issue("production policy override rejected", override_result.issues, "policy override"))

    selection_override = copy.deepcopy(base)
    selection_override.setdefault("validation", {}).setdefault("part_selection", {})[
        "policy_file"
    ] = "assets/jlc-lcsc-part-selection-policy.yaml"
    selection_override_result = CheckResult()
    check_part_selection(
        selection_override,
        selection_override_result,
        force=True,
        spec_path=Path(argv[1]),
        as_of=AS_OF,
    )
    failures.extend(
        expect_issue("production composite policy override rejected", selection_override_result.issues, "policy override")
    )

    flow_override = copy.deepcopy(base)
    flow_override.setdefault("validation", {}).setdefault("flow_state", {})[
        "policy_file"
    ] = "assets/flow-state-policy.yaml"
    flow_override_result = CheckResult()
    check_flow_state(flow_override, flow_override_result, True, False, False)
    failures.extend(expect_issue("production flow policy override rejected", flow_override_result.issues, "policy override"))

    architecture_override = copy.deepcopy(base)
    architecture_override.setdefault("validation", {}).setdefault("architecture", {})[
        "policy_file"
    ] = "assets/architecture-policy.yaml"
    architecture_override_result = CheckResult()
    check_architecture(architecture_override, architecture_override_result, force=True, before_sourcing=True)
    failures.extend(
        expect_issue("production architecture policy override rejected", architecture_override_result.issues, "policy override")
    )

    intake_override = copy.deepcopy(base)
    intake_override.setdefault("validation", {}).setdefault("requirement_intake", {})[
        "policy_file"
    ] = "assets/requirement-intake-policy.yaml"
    intake_override_result = CheckResult()
    check_requirement_intake(intake_override, intake_override_result, force=True)
    failures.extend(
        expect_issue("production intake policy override rejected", intake_override_result.issues, "policy override")
    )

    connectivity_override = copy.deepcopy(base)
    connectivity_override.setdefault("validation", {})[
        "connectivity_batch_policy_file"
    ] = "assets/connectivity-batch-policy.yaml"
    connectivity_override_result = CheckResult()
    check_connectivity_batches(connectivity_override, connectivity_override_result, auto_require=True)
    failures.extend(
        expect_issue("production connectivity policy override rejected", connectivity_override_result.issues, "policy override")
    )

    draft = copy.deepcopy(base)
    draft["project"]["stage"] = "draft"
    draft["manufacturing"]["jlcpcb"]["assembly"]["enabled"] = False
    draft["sourcing"]["context"]["assembly"]["enabled"] = False
    if is_production_candidate(draft, load_selection_policy(draft)):
        print("declared sourcing cannot draft-noop: PASS")
    else:
        failures.append("declared sourcing cannot draft-noop: gate disabled despite sourcing section")

    candidate, requirement, constraints, providers, context, artifacts_root = candidate_fixture(base)
    reused_evidence = copy.deepcopy(candidate)
    first_evidence = reused_evidence["evidence"][0]
    for evidence in reused_evidence["evidence"][1:]:
        evidence["file"] = first_evidence["file"]
        evidence["sha256"] = first_evidence["sha256"]
        evidence["byte_size"] = first_evidence["byte_size"]
    evidence_result = evaluate_candidate(
        reused_evidence,
        requirement,
        constraints,
        providers,
        context,
        load_policy(base),
        effective_now(AS_OF),
        artifacts_root,
    )
    if not evidence_result["qualified"] and any(
        "distinct raw evidence" in reason for reason in evidence_result["reasons"]
    ):
        print("reused self-asserted evidence rejected: PASS")
    else:
        failures.append(f"reused self-asserted evidence should fail: {evidence_result}")

    wrong_provider = copy.deepcopy(candidate)
    wrong_providers = copy.deepcopy(providers)
    supplier_id = str(get_path(wrong_provider, "supplier.provider_id"))
    wrong_providers[supplier_id]["kind"] = "manufacturer"
    provider_result = evaluate_candidate(
        wrong_provider,
        requirement,
        constraints,
        wrong_providers,
        context,
        load_policy(base),
        effective_now(AS_OF),
        artifacts_root,
    )
    if not provider_result["qualified"] and any(
        "supplier provider" in reason for reason in provider_result["reasons"]
    ):
        print("supplier provider kind enforced: PASS")
    else:
        failures.append(f"supplier provider kind should fail: {provider_result}")

    missing_roles = copy.deepcopy(base)
    missing_roles["sourcing"].pop("roles", None)
    roles_result = CheckResult()
    check_sourcing_context(missing_roles, roles_result, force=True)
    failures.extend(expect_issue("component role omission rejected", roles_result.issues, "sourcing.roles"))

    misplaced_role = copy.deepcopy(base)
    misplaced_role["sourcing"]["roles"][3]["block_ids"] = ["CONTROL_CORE"]
    misplaced_role_result = CheckResult()
    check_sourcing_context(misplaced_role, misplaced_role_result, force=True)
    failures.extend(expect_issue("misplaced component role rejected", misplaced_role_result.issues, "block_ids are outside"))

    unrelated_compatibility = copy.deepcopy(base)
    unrelated_compatibility["sourcing"]["compatibility_constraints"][0]["architecture_refs"][
        "block_edge_ids"
    ] = ["EDGE_CONTROL_IO"]
    unrelated_compatibility_result = CheckResult()
    check_sourcing_context(unrelated_compatibility, unrelated_compatibility_result, force=True)
    failures.extend(
        expect_issue(
            "compatibility endpoint ownership enforced",
            unrelated_compatibility_result.issues,
            "internal endpoint blocks",
        )
    )

    incompatible = copy.deepcopy(base)
    incompatible["sourcing"]["compatibility_constraints"][-1]["value"] = "incompatible-family"
    incompatible_result = CheckResult()
    evaluate_candidate_manifest(incompatible, Path(argv[1]), incompatible_result, force=True, as_of=AS_OF)
    failures.extend(
        expect_issue("incompatible selected set rejected", incompatible_result.issues, "selected-set compatibility")
    )

    over_budget = copy.deepcopy(base)
    over_budget["sourcing"]["context"]["cost_budget"]["maximum_component_cost_per_board"] = 1
    over_budget["sourcing"]["context"]["cost_budget"]["maximum_component_order_total"] = 10
    over_budget_result = CheckResult()
    evaluate_candidate_manifest(over_budget, Path(argv[1]), over_budget_result, force=True, as_of=AS_OF)
    failures.extend(expect_issue("aggregate sourcing budget enforced", over_budget_result.issues, "selected-set cost"))

    with tempfile.TemporaryDirectory() as temporary_dir:
        root = Path(temporary_dir)
        target = root / "spec.yaml"
        backup = root / ".spec.backup"
        temporary = root / ".spec.temporary"
        journal = root / "spec.sourcing-txn.json"
        target.write_text("state: partial\n", encoding="utf-8")
        backup.write_text("state: original\n", encoding="utf-8")
        temporary.write_text("state: next\n", encoding="utf-8")
        journal.write_text(
            '{"entries":[{"target":"%s","backup":"%s","temporary":"%s","existed":true}]}'
            % (target, backup, temporary),
            encoding="utf-8",
        )
        recover_transaction(journal)
        if target.read_text(encoding="utf-8") == "state: original\n" and not journal.exists():
            print("interrupted transaction recovery: PASS")
        else:
            failures.append("interrupted transaction recovery did not restore the original target")

        commented = root / "commented.yaml"
        commented.write_text("# retained comment\nstate: original\n", encoding="utf-8")
        rendered = roundtrip_spec_bytes(commented, {"state": "updated"}).decode("utf-8")
        if "# retained comment" in rendered and "state: updated" in rendered:
            print("round-trip spec comments retained: PASS")
        else:
            failures.append(f"round-trip spec update lost source comments: {rendered!r}")

    policy = load_policy(base)
    root = project_root(base, Path(argv[1]), policy)
    artifacts_root = resolved_path(base["project"]["artifacts_dir"], root)
    with tempfile.TemporaryDirectory(dir=artifacts_root) as temporary_dir:
        binding_root = Path(temporary_dir)
        ready = copy.deepcopy(base)
        lock = load_data_file(resolved_path(get_path(base, "sourcing.artifacts.part_lock"), root))
        bindings = []
        for selection in lock["selections"]:
            ref = str(selection["component_refs"][0])
            symbol_library = f"Fixture_{ref}"
            footprint_library = f"Fixture_{ref}"
            symbol_id = f"{symbol_library}:Symbol_{ref}"
            footprint = f"{footprint_library}:Part_{ref}"
            pin_count = int(selection["package"]["pin_count"])
            symbol_file = binding_root / f"{symbol_library}.kicad_sym"
            footprint_dir = binding_root / f"{footprint_library}.pretty"
            footprint_dir.mkdir()
            footprint_file = footprint_dir / f"Part_{ref}.kicad_mod"
            symbol_pins = " ".join(
                f'(pin input line (at 0 {index} 0) (length 2.54) (name "P{index}") (number "{index}"))'
                for index in range(1, pin_count + 1)
            )
            footprint_pads = " ".join(
                f'(pad "{index}" thru_hole circle (at 0 {index}) (size 1 1) (drill 0.5) (layers "*.Cu" "*.Mask"))'
                for index in range(1, pin_count + 1)
            )
            symbol_file.write_text(
                f'(kicad_symbol_lib (version 20231120) (generator "fixture") (symbol "Symbol_{ref}" {symbol_pins}))\n',
                encoding="utf-8",
            )
            footprint_file.write_text(
                f'(footprint "Part_{ref}" (version 20240108) (generator "fixture") {footprint_pads})\n',
                encoding="utf-8",
            )
            symbol_file_sha = sha256_file(symbol_file)
            footprint_file_sha = sha256_file(footprint_file)
            pinmap_path = binding_root / f"{ref}-pinmap.json"
            datasheet_record = next(
                item for item in selection["evidence_records"] if item["kind"] == "manufacturer-datasheet"
            )
            pinmap_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "ref": ref,
                        "symbol_id": symbol_id,
                        "footprint": footprint,
                        "status": "verified",
                        "source_evidence_ids": [datasheet_record["id"]],
                        "source_evidence_sha256": [datasheet_record["sha256"]],
                        "symbol_file_sha256": symbol_file_sha,
                        "footprint_file_sha256": footprint_file_sha,
                        "mappings": [
                            {"symbol_pin": str(index), "footprint_pad": str(index)}
                            for index in range(1, pin_count + 1)
                        ],
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            pinmap_sha = sha256_file(pinmap_path)
            binding = {
                "ref": ref,
                "requirement_id": selection["requirement_id"],
                "candidate_id": selection["candidate_id"],
                "symbol_library": symbol_library,
                "symbol_id": symbol_id,
                "footprint_library": footprint_library,
                "footprint": footprint,
                "source_footprint_candidate": selection["package"]["footprint_candidate"],
                "symbol_file_evidence": {"file": str(symbol_file), "sha256": symbol_file_sha},
                "footprint_file_evidence": {"file": str(footprint_file), "sha256": footprint_file_sha},
                "package_verification": {
                    "status": "verified",
                    "package_name": selection["package"]["name"],
                    "pin_count": pin_count,
                    "footprint_package_token": selection["package"]["name"],
                },
                "pinmap_evidence": {"file": str(pinmap_path), "sha256": pinmap_sha},
                "status": "verified",
            }
            bindings.append(binding)
        manifest_path = binding_root / "library-binding.yaml"
        atomic_write_yaml(
            manifest_path,
            {
                "schema_version": 1,
                "project_name": get_path(base, "project.name"),
                "part_lock_sha256": get_path(base, "sourcing.part_lock.sha256"),
                "generated_at": AS_OF,
                "bindings": bindings,
            },
        )
        binding_transaction_result = CheckResult()
        ready, _ = prepare_binding(
            ready,
            Path(argv[1]),
            manifest_path,
            binding_transaction_result,
            AS_OF,
        )
        ready_result = CheckResult()
        check_part_lock(
            ready,
            Path(argv[1]),
            ready_result,
            force=True,
            as_of=AS_OF,
            before_generation=True,
        )
        if binding_transaction_result.ok() and ready_result.ok():
            print("verified downstream binding advances generation: PASS")
        else:
            failures.append(
                "verified downstream binding should pass: "
                f"transaction={binding_transaction_result.issues}, check={ready_result.issues}"
            )

        manifest_original = manifest_path.read_bytes()
        first_pinmap_path = Path(bindings[0]["pinmap_evidence"]["file"])
        first_pinmap_original = first_pinmap_path.read_bytes()
        swapped_pinmap = json.loads(first_pinmap_original.decode("utf-8"))
        swapped_pinmap["mappings"][0]["footprint_pad"] = "2"
        swapped_pinmap["mappings"][1]["footprint_pad"] = "1"
        first_pinmap_path.write_text(json.dumps(swapped_pinmap, sort_keys=True) + "\n", encoding="utf-8")
        swapped_manifest = load_data_file(manifest_path)
        swapped_manifest["bindings"][0]["pinmap_evidence"]["sha256"] = sha256_file(first_pinmap_path)
        atomic_write_yaml(manifest_path, swapped_manifest)
        swapped_result = CheckResult()
        prepare_binding(copy.deepcopy(base), Path(argv[1]), manifest_path, swapped_result, AS_OF)
        failures.extend(
            expect_issue(
                "swapped pin-to-pad mapping rejected",
                swapped_result.issues,
                "symbol_pin must equal footprint_pad",
            )
        )
        first_pinmap_path.write_bytes(first_pinmap_original)
        manifest_path.write_bytes(manifest_original)

        first_symbol_file = Path(bindings[0]["symbol_file_evidence"]["file"])
        first_symbol_file.write_text(
            first_symbol_file.read_text(encoding="utf-8") + "; tampered\n",
            encoding="utf-8",
        )
        tampered_library_result = CheckResult()
        check_part_lock(
            ready,
            Path(argv[1]),
            tampered_library_result,
            force=True,
            as_of=AS_OF,
            before_generation=True,
        )
        failures.extend(
            expect_issue(
                "tampered bound library rejected",
                tampered_library_result.issues,
                "library file sha256",
            )
        )

    if failures:
        for failure in failures:
            print(f"ISSUE: {failure}")
        return 1
    print("sourcing adversarial regressions: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
