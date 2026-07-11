#!/usr/bin/env python3
"""Decide and enforce the current PCB flow state before running heavy gates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pcb_skill_checks import CheckResult, get_path, load_spec, load_yaml, print_result, string_value  # noqa: E402


def mapping_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def list_strings(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def bool_value(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"true", "yes", "1"}


def policy_path(spec: dict[str, Any]) -> Path:
    default = Path(__file__).resolve().parents[1] / "assets" / "flow-state-policy.yaml"
    configured = get_path(spec, "validation.flow_state.policy_file")
    if string_value(configured):
        builtin = load_yaml(default)
        if determine_state(spec, builtin)["production_required"]:
            return default
    if string_value(configured):
        path = Path(str(configured))
        return path if path.is_absolute() else Path.cwd() / path
    return default


def load_policy(spec: dict[str, Any]) -> dict[str, Any]:
    try:
        return load_yaml(policy_path(spec))
    except (OSError, ValueError, yaml.YAMLError):
        return {}


def dotted_exists(data: dict[str, Any], dotted_path: str) -> bool:
    current: Any = data
    for part in dotted_path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return current not in (None, False, "", [], {})


def stage_value(spec: dict[str, Any]) -> str:
    return str(mapping_value(spec.get("project")).get("stage", "")).strip().lower()


def stage_in(spec: dict[str, Any], values: list[str]) -> bool:
    stage = stage_value(spec)
    return bool(stage) and stage in {value.strip().lower() for value in values}


def has_any_signal(spec: dict[str, Any], signals: list[str]) -> bool:
    return any(dotted_exists(spec, signal) for signal in signals)


def determine_state(spec: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    production = stage_in(spec, list_strings(policy.get("production_stage_values"))) or has_any_signal(
        spec, list_strings(policy.get("production_signals"))
    )
    order_ready = stage_in(spec, list_strings(policy.get("order_ready_stage_values"))) or has_any_signal(
        spec, list_strings(policy.get("order_ready_signals"))
    )
    return {
        "stage": stage_value(spec) or None,
        "production_required": production,
        "order_ready_required": order_ready,
        "strict_violations_required": production and bool_value(policy.get("strict_for_production", True)),
        "jlcpcb_production_gate_required": production and get_path(spec, "manufacturing.target") == "jlcpcb",
    }


def check_flow_state(
    spec: dict[str, Any],
    result: CheckResult,
    strict_violations: bool,
    allow_violations: bool,
    continue_on_check_fail: bool,
) -> dict[str, Any]:
    policy = load_policy(spec)
    state = determine_state(spec, policy)
    if state["production_required"] and string_value(get_path(spec, "validation.flow_state.policy_file")):
        result.issue("production flow-state policy override is forbidden; use the bundled trusted policy")
    if state["strict_violations_required"] and not strict_violations:
        result.issue("production/order-ready flow requires strict ERC/DRC violation exits")
    if state["production_required"] and allow_violations and bool_value(policy.get("forbid_allow_violations_for_production", True)):
        result.issue("production/order-ready flow forbids --allow-violations")
    if state["production_required"] and continue_on_check_fail and bool_value(
        policy.get("forbid_continue_on_check_fail_for_production", True)
    ):
        result.issue("production/order-ready flow forbids --continue-on-check-fail")
    if not state["stage"]:
        result.warning("project.stage is not declared; treat output as draft/MVP unless another production signal exists")
    return {"policy": str(policy_path(spec)), **state}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Check PCB flow state and command flags.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--strict-violations", action="store_true")
    parser.add_argument("--allow-violations", action="store_true")
    parser.add_argument("--continue-on-check-fail", action="store_true")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])

    result = CheckResult()
    details: dict[str, Any] = {}
    try:
        spec = load_spec(args.spec)
        details = check_flow_state(
            spec,
            result,
            strict_violations=args.strict_violations,
            allow_violations=args.allow_violations,
            continue_on_check_fail=args.continue_on_check_fail,
        )
        if args.report_only:
            result.issues.clear()
    except Exception as error:
        result.issue(str(error))

    if args.json_output:
        print(
            json.dumps(
                {
                    "check": "flow_state_gate",
                    "ok": result.ok(),
                    "issues": result.issues,
                    "warnings": result.warnings,
                    "details": details,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if result.ok() else 1
    if details:
        print(
            "flow_state_gate state: "
            f"stage={details.get('stage') or '<missing>'} "
            f"production_required={details.get('production_required')} "
            f"order_ready_required={details.get('order_ready_required')} "
            f"strict_required={details.get('strict_violations_required')}"
        )
    return print_result("flow_state_gate", result, False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
