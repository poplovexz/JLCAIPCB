#!/usr/bin/env python3
"""Deterministic readiness checks for PCB production intent, power, and TODO blockers."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pcb_skill_checks import CheckResult, get_path, load_spec, print_result, string_value  # noqa: E402


DEFAULT_REQUIREMENT_AREAS = [
    "function",
    "power",
    "key_parts",
    "interfaces",
    "mechanical",
    "manufacturing",
    "risk_review",
]
DEFAULT_ACCEPTED_STATUSES = {"confirmed", "locked", "not_applicable"}
DEFAULT_BLOCKING_TODO_PATTERNS = [
    r"\bTODO\b",
    r"\bTBD\b",
    r"\bFIXME\b",
    r"\bconfirm\b",
    r"\bselect\b",
    r"\bunknown\b",
    r"\bassumption\b",
    r"待确认",
    r"未确认",
    r"未知",
    r"假设",
]
DEFAULT_POST_FAB_TODO_CATEGORIES = {
    "post_fab_validation",
    "post_fab_bringup",
    "post_fab_lab_test",
    "post_fab_mechanical_fit",
}


def list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def mapping_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def number_value(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def positive_number(value: Any) -> bool:
    return number_value(value) and float(value) > 0


def configured_validation(spec: dict[str, Any], key: str) -> dict[str, Any]:
    validation = spec.get("validation", {})
    if not isinstance(validation, dict):
        return {}
    value = validation.get(key, {})
    return value if isinstance(value, dict) else {}


def check_requirements(spec: dict[str, Any], result: CheckResult, strict: bool = False) -> None:
    config = configured_validation(spec, "requirements")
    required = strict or bool(config.get("required", False))
    if not required and "requirements" not in spec:
        return

    requirements = spec.get("requirements", {})
    if not isinstance(requirements, dict):
        result.issue("requirements must be a mapping")
        return

    required_areas = [str(item) for item in config.get("required_areas", DEFAULT_REQUIREMENT_AREAS)]
    accepted_statuses = {str(item) for item in config.get("accepted_statuses", sorted(DEFAULT_ACCEPTED_STATUSES))}
    allow_assumptions = bool(config.get("allow_assumptions", False))

    for area in required_areas:
        item = requirements.get(area)
        if not isinstance(item, dict):
            result.issue(f"requirements.{area} is required and must be a mapping")
            continue
        status = str(item.get("status", "")).strip()
        if status not in accepted_statuses:
            result.issue(f"requirements.{area}.status must be one of {', '.join(sorted(accepted_statuses))}; got {status or '<missing>'}")
        summary = item.get("summary", item.get("description"))
        if status != "not_applicable" and not string_value(summary):
            result.issue(f"requirements.{area} must include a non-empty summary or description")
        assumptions = item.get("assumptions", [])
        if assumptions and not allow_assumptions:
            result.issue(f"requirements.{area} contains unresolved assumptions")
        decisions = item.get("decisions", [])
        if decisions is not None and not isinstance(decisions, list):
            result.issue(f"requirements.{area}.decisions must be a list when present")


def load_current_a(load: dict[str, Any]) -> float:
    for key in ["peak_current_a", "stall_current_a", "inrush_current_a", "current_a", "max_current_a"]:
        if positive_number(load.get(key)):
            return float(load[key])
    return 0.0


def check_power_domains(spec: dict[str, Any], result: CheckResult, strict: bool = False) -> None:
    config = configured_validation(spec, "power_budget")
    required = strict or bool(config.get("required", False))
    domains = spec.get("power_domains", [])
    if not required and not domains:
        return
    if not isinstance(domains, list) or not domains:
        result.issue("power_domains must be a non-empty list")
        return

    default_margin = float(config.get("default_margin_percent", 20.0))
    for index, domain in enumerate(domains):
        if not isinstance(domain, dict):
            result.issue(f"power_domains[{index}] must be a mapping")
            continue
        name = str(domain.get("name", f"power_domains[{index}]"))
        voltage = mapping_value(domain.get("voltage"))
        source = mapping_value(domain.get("source"))
        loads = list_value(domain.get("loads"))
        margin_percent = float(domain.get("required_margin_percent", default_margin))
        multiplier = 1.0 + margin_percent / 100.0

        if not string_value(domain.get("name")):
            result.issue(f"{name}.name must be a non-empty string")
        if not positive_number(voltage.get("nominal_v")):
            result.issue(f"{name}.voltage.nominal_v must be a positive number")
        if not positive_number(source.get("current_limit_a")):
            result.issue(f"{name}.source.current_limit_a must be a positive number")
        if not isinstance(loads, list) or not loads:
            result.issue(f"{name}.loads must be a non-empty list")
            loads = []

        total_peak = 0.0
        for load_index, load in enumerate(loads):
            if not isinstance(load, dict):
                result.issue(f"{name}.loads[{load_index}] must be a mapping")
                continue
            load_name = str(load.get("name", f"loads[{load_index}]"))
            current = load_current_a(load)
            if current <= 0:
                result.issue(f"{name}.{load_name} must declare current_a, max_current_a, peak_current_a, stall_current_a, or inrush_current_a")
            total_peak += current * float(load.get("quantity", 1) if positive_number(load.get("quantity", 1)) else 1)

        source_limit = float(source.get("current_limit_a", 0.0)) if positive_number(source.get("current_limit_a")) else 0.0
        required_current = total_peak * multiplier
        if source_limit and total_peak and source_limit < required_current:
            result.issue(
                f"{name}.source current_limit_a {source_limit:.3g}A is below load peak {total_peak:.3g}A with {margin_percent:.3g}% margin ({required_current:.3g}A)"
            )

        for conductor in list_value(domain.get("conductors")):
            if not isinstance(conductor, dict):
                result.issue(f"{name}.conductors entries must be mappings")
                continue
            rating = conductor.get("current_rating_a")
            label = str(conductor.get("name", "conductor"))
            if not positive_number(rating):
                result.issue(f"{name}.{label}.current_rating_a must be a positive number")
            elif total_peak and float(rating) < required_current:
                result.issue(f"{name}.{label}.current_rating_a {float(rating):.3g}A is below required {required_current:.3g}A")

        protection = mapping_value(domain.get("protection"))
        if bool(config.get("require_protection", False)) and not protection:
            result.issue(f"{name}.protection is required by validation.power_budget.require_protection")
        if protection:
            fuse = mapping_value(protection.get("fuse"))
            if fuse and positive_number(fuse.get("current_rating_a")) and total_peak and float(fuse["current_rating_a"]) < total_peak:
                result.issue(f"{name}.protection.fuse.current_rating_a is below declared peak load current")


def todo_strings(spec: dict[str, Any]) -> list[str]:
    items = spec.get("todo", [])
    output: list[str] = []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, str):
                output.append(item)
            elif isinstance(item, dict):
                text = item.get("text", item.get("summary", item.get("description", "")))
                status = str(item.get("status", ""))
                blocking = item.get("blocking")
                output.append(f"{text} status={status} blocking={blocking}")
    return output


def post_fab_todo_categories(spec: dict[str, Any]) -> set[str]:
    config = configured_validation(spec, "todo_blockers")
    return {
        str(item)
        for item in config.get("post_fab_nonblocking_categories", sorted(DEFAULT_POST_FAB_TODO_CATEGORIES))
        if string_value(item)
    }


def todo_phase(item: dict[str, Any], post_fab_categories: set[str]) -> str:
    category = str(item.get("category", "")).strip()
    phase = str(item.get("phase", item.get("stage", item.get("validation_stage", "")))).strip()
    if category in post_fab_categories or (category in {"bringup", "lab_test"} and phase == "post_fab"):
        return "post_fab"
    return "pre_fab"


def todo_text(item: dict[str, Any]) -> str:
    return str(item.get("text", item.get("summary", item.get("description", ""))))


def todo_phase_report(spec: dict[str, Any]) -> dict[str, Any]:
    post_fab_categories = post_fab_todo_categories(spec)
    report: dict[str, Any] = {"pre_fab_blockers": [], "post_fab_validation_plan": [], "malformed": []}
    for index, item in enumerate(spec.get("todo", []) if isinstance(spec.get("todo", []), list) else []):
        if not isinstance(item, dict):
            report["malformed"].append({"index": index, "item": item})
            continue
        entry = {
            "index": index,
            "category": item.get("category"),
            "phase": item.get("phase", item.get("stage", item.get("validation_stage"))),
            "blocking": item.get("blocking"),
            "status": item.get("status"),
            "text": todo_text(item),
            "next_action": item.get("next_action"),
        }
        if todo_phase(item, post_fab_categories) == "post_fab":
            report["post_fab_validation_plan"].append(entry)
        elif item.get("blocking") is not False and str(item.get("status", "")).strip().lower() not in {"done", "closed", "non_blocking", "not_applicable"}:
            report["pre_fab_blockers"].append(entry)
    return report


def check_todo_blockers(spec: dict[str, Any], result: CheckResult, production: bool = False) -> None:
    config = configured_validation(spec, "todo_blockers")
    required = production or bool(config.get("required", False))
    if not required and "todo" not in spec:
        return

    patterns = [re.compile(str(pattern), re.I) for pattern in config.get("blocking_patterns", DEFAULT_BLOCKING_TODO_PATTERNS)]
    allow_patterns = [re.compile(str(pattern), re.I) for pattern in config.get("allow_patterns", [])]
    post_fab_categories = post_fab_todo_categories(spec)

    for index, item in enumerate(spec.get("todo", []) if isinstance(spec.get("todo", []), list) else []):
        if isinstance(item, dict):
            status = str(item.get("status", "")).strip().lower()
            blocking = item.get("blocking")
            category = str(item.get("category", "")).strip()
            text = str(item.get("text", item.get("summary", item.get("description", ""))))
            if status in {"done", "closed", "non_blocking", "not_applicable"} or blocking is False:
                continue
            if todo_phase(item, post_fab_categories) == "post_fab":
                continue
            if blocking is True or required:
                result.issue(f"todo[{index}] blocks production: {text or item}")
                continue
        elif isinstance(item, str):
            if any(pattern.search(item) for pattern in allow_patterns):
                continue
            if required or any(pattern.search(item) for pattern in patterns):
                result.issue(f"todo[{index}] blocks production: {item}")
        else:
            result.issue(f"todo[{index}] must be a string or mapping")

    if bool(config.get("include_project_todo_md", False)):
        todo_path = Path(str(get_path(spec, "project.output_dir") or "")) / "TODO.md"
        if todo_path.exists():
            text = todo_path.read_text(encoding="utf-8", errors="replace")
            for pattern in patterns:
                if pattern.search(text):
                    result.issue(f"Project TODO.md contains blocker pattern: {todo_path}")
                    break


def requirements_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Validate machine-enforced requirements completeness.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])
    result = CheckResult()
    try:
        check_requirements(load_spec(args.spec), result, strict=args.strict)
    except Exception as error:
        result.issue(str(error))
    return print_result("requirements_gate", result, args.json_output)


def power_budget_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Validate declared power domains against current budgets.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])
    result = CheckResult()
    try:
        check_power_domains(load_spec(args.spec), result, strict=args.strict)
    except Exception as error:
        result.issue(str(error))
    return print_result("power_budget_check", result, args.json_output)


def todo_blocker_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Block production claims when unresolved TODO items remain.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])
    result = CheckResult()
    try:
        check_todo_blockers(load_spec(args.spec), result, production=args.production)
    except Exception as error:
        result.issue(str(error))
    return print_result("todo_blocker_check", result, args.json_output)


def todo_phase_report_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Report pre-fabrication TODO blockers separately from post-fabrication validation items.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])
    try:
        report = todo_phase_report(load_spec(args.spec))
    except Exception as error:
        result = CheckResult()
        result.issue(str(error))
        return print_result("todo_phase_report", result, args.json_output)
    if args.json_output:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("todo_phase_report: PASS")
        print(f"pre_fab_blockers: {len(report['pre_fab_blockers'])}")
        for item in report["pre_fab_blockers"]:
            print(f"PRE_FAB[{item['index']}]: {item['text']}")
        print(f"post_fab_validation_plan: {len(report['post_fab_validation_plan'])}")
        for item in report["post_fab_validation_plan"]:
            print(f"POST_FAB[{item['index']}]: {item['text']}")
        if report["malformed"]:
            print(f"malformed: {len(report['malformed'])}")
    return 0
