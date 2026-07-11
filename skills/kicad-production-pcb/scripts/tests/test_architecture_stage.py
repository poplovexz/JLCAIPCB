#!/usr/bin/env python3
"""Focused regression checks for architecture boundary and confirmation gates."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from _pcb_skill_checks import CheckResult, load_spec  # noqa: E402
from architecture_gate import architecture_report_path, check_architecture, load_policy  # noqa: E402


def expect_issue(name: str, spec: dict, expected: str) -> list[str]:
    result = CheckResult()
    check_architecture(spec, result, force=True)
    if any(expected in issue for issue in result.issues):
        print(f"{name}: PASS")
        return []
    return [f"{name}: expected issue containing {expected!r}; got {result.issues}"]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: test_architecture_stage.py <passing-architecture-spec>", file=sys.stderr)
        return 2
    base = load_spec(Path(argv[1]))
    policy = load_policy(base)
    failures: list[str] = []

    missing_boundary = copy.deepcopy(base)
    architecture = missing_boundary["architecture"]
    architecture["interfaces"] = []
    architecture["external_connectors"] = []
    architecture["risk_paths"] = []
    power_domain_ids = {item.get("id") for item in architecture["power_domains"] if isinstance(item, dict)}
    architecture["protection_intents"] = [
        item for item in architecture["protection_intents"] if item.get("boundary_id") in power_domain_ids
    ]
    failures.extend(
        expect_issue(
            "boundary edge without interface",
            missing_boundary,
            "boundary block edge has no matching external interface",
        )
    )

    wrong_connector = copy.deepcopy(base)
    external_interface = next(
        item for item in wrong_connector["architecture"]["interfaces"] if item.get("external") is True
    )
    interface_endpoints = {external_interface.get("from"), external_interface.get("to")}
    connector_categories = {str(value) for value in policy.get("connector_block_categories", [])}
    unrelated_block = next(
        item["id"]
        for item in wrong_connector["architecture"]["blocks"]
        if item.get("id") not in interface_endpoints and item.get("category") in connector_categories
    )
    wrong_connector["architecture"]["external_connectors"][0]["block_id"] = unrelated_block
    failures.extend(
        expect_issue(
            "connector on unrelated block",
            wrong_connector,
            "must be the board-side endpoint",
        )
    )

    missing_external_risk = copy.deepcopy(base)
    missing_external_risk["architecture"]["risk_paths"] = []
    missing_external_risk["architecture"]["external_connectors"][0]["exposure"] = "internal_service"
    failures.extend(
        expect_issue(
            "external service interface without cable risk",
            missing_external_risk,
            "external interface is missing an external-cable risk path",
        )
    )

    unresolved_constraint = copy.deepcopy(base)
    constrained_block = next(
        item for item in unresolved_constraint["architecture"]["blocks"] if item.get("selection_constraints")
    )
    constrained_block["selection_constraints"][0]["statement"] = "TBD"
    failures.extend(
        expect_issue(
            "unresolved sourcing constraint",
            unresolved_constraint,
            "statement contains unresolved token",
        )
    )

    stale_confirmation = copy.deepcopy(base)
    stale_confirmation["architecture"]["summary"] += " Changed after confirmation."
    result = CheckResult()
    check_architecture(stale_confirmation, result, force=True, before_sourcing=True)
    expected = "practical_choice_confirmation.architecture_sha256 is stale"
    if any(expected in issue for issue in result.issues):
        print("stale practical confirmation: PASS")
    else:
        failures.append(f"stale practical confirmation: expected {expected!r}; got {result.issues}")

    unsafe_path = copy.deepcopy(base)
    unsafe_path["architecture"]["outputs"]["report_path"] = "../architecture-outside-artifacts.md"
    try:
        architecture_report_path(unsafe_path, load_policy(unsafe_path))
    except ValueError as error:
        if "must stay under project artifacts directory" in str(error):
            print("report path containment: PASS")
        else:
            failures.append(f"report path containment: unexpected error: {error}")
    else:
        failures.append("report path containment: unsafe path was accepted")

    if failures:
        for failure in failures:
            print(f"ISSUE: {failure}")
        return 1
    print("architecture stage regressions: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
