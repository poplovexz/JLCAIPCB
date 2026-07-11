#!/usr/bin/env python3
"""Check whether a real PCB spec has closed production-impacting decisions."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pcb_skill_checks import CheckResult, load_spec, net_names, print_result, string_value  # noqa: E402
from _readiness_checks import check_power_domains, check_requirements  # noqa: E402


DEFAULT_REQUIRED_AREAS = [
    "requirements",
    "power_domains",
    "verification",
    "todo_disposition",
    "stage",
]
DEFAULT_CLOSURE_STAGES = {
    "draft",
    "local-mvp",
    "local_mvp",
    "production-package",
    "production_package",
    "order-ready-blocked",
    "order_ready_blocked",
    "bring-up-required",
    "bring_up_required",
}
PRODUCTION_STAGE_VALUES = {
    "fabrication",
    "order-ready",
    "order_ready",
    "production",
    "production-package",
    "production_package",
    "release",
}
TODO_DONE_STATUSES = {"done", "closed", "non_blocking", "not_applicable"}
TODO_ALLOWED_CATEGORIES = {
    "user_input",
    "datasheet_review",
    "web_dfm",
    "bringup",
    "lab_test",
    "simulation",
    "non_blocking_note",
    "post_fab_validation",
    "post_fab_bringup",
    "post_fab_lab_test",
    "post_fab_mechanical_fit",
}


def mapping_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def validation_config(spec: dict[str, Any], key: str) -> dict[str, Any]:
    validation = mapping_value(spec.get("validation"))
    return mapping_value(validation.get(key))


def closure_config(spec: dict[str, Any]) -> dict[str, Any]:
    return validation_config(spec, "spec_closure")


def bool_config(config: dict[str, Any], key: str, default: bool = False) -> bool:
    value = config.get(key)
    if value is None:
        return default
    return bool(value)


def has_jlcpcb_release(spec: dict[str, Any]) -> bool:
    jlcpcb = mapping_value(mapping_value(spec.get("manufacturing")).get("jlcpcb"))
    return isinstance(jlcpcb.get("release"), dict)


def is_production_candidate(spec: dict[str, Any], forced: bool = False) -> bool:
    if forced:
        return True
    project_stage = str(mapping_value(spec.get("project")).get("stage", "")).strip().lower()
    manufacturing = mapping_value(spec.get("manufacturing"))
    return (
        project_stage in PRODUCTION_STAGE_VALUES
        or bool(manufacturing.get("production_ready"))
        or bool(manufacturing.get("order_ready"))
        or has_jlcpcb_release(spec)
    )


def should_run(spec: dict[str, Any], strict: bool = False) -> bool:
    config = closure_config(spec)
    return (
        strict
        or bool_config(config, "required")
        or is_production_candidate(spec)
        or "requirements" in spec
        or "power_domains" in spec
        or "verification" in spec
    )


def configured_areas(spec: dict[str, Any]) -> set[str]:
    config = closure_config(spec)
    areas = config.get("required_areas", DEFAULT_REQUIRED_AREAS)
    if not isinstance(areas, list):
        return set(DEFAULT_REQUIRED_AREAS)
    return {str(area) for area in areas}


def power_net_candidates(spec: dict[str, Any]) -> set[str]:
    config = closure_config(spec)
    explicit = config.get("power_nets")
    if isinstance(explicit, list):
        return {str(item) for item in explicit if string_value(item)}
    candidates: set[str] = set()
    for net in list_value(spec.get("nets")):
        if not isinstance(net, dict) or not string_value(net.get("name")):
            continue
        net_class = str(net.get("class", "")).strip().lower()
        name = str(net["name"])
        if "power" in net_class:
            candidates.add(name)
    return candidates


def power_domain_nets(spec: dict[str, Any]) -> set[str]:
    covered: set[str] = set()
    for domain in list_value(spec.get("power_domains")):
        if not isinstance(domain, dict):
            continue
        if string_value(domain.get("name")):
            covered.add(str(domain["name"]))
        for net in list_value(domain.get("nets")):
            if string_value(net):
                covered.add(str(net))
        source = mapping_value(domain.get("source"))
        if string_value(source.get("net")):
            covered.add(str(source["net"]))
        for load in list_value(domain.get("loads")):
            if not isinstance(load, dict):
                continue
            if string_value(load.get("net")):
                covered.add(str(load["net"]))
            for net in list_value(load.get("nets")):
                if string_value(net):
                    covered.add(str(net))
    return covered


def check_stage(spec: dict[str, Any], result: CheckResult, strict: bool = False) -> None:
    config = closure_config(spec)
    allowed = config.get("allowed_stages", sorted(DEFAULT_CLOSURE_STAGES))
    allowed_stages = {str(stage) for stage in allowed} if isinstance(allowed, list) else set(DEFAULT_CLOSURE_STAGES)
    stage = mapping_value(spec.get("project")).get("stage")
    if not string_value(stage):
        result.issue("project.stage must declare the current lifecycle stage")
        result.warning("QUESTION[stage]: Is this spec draft, local-mvp, production-package, order-ready-blocked, or bring-up-required?")
        return
    if str(stage) not in allowed_stages and strict:
        result.issue(f"project.stage must be one of {', '.join(sorted(allowed_stages))}; got {stage}")


def check_requirements_closed(spec: dict[str, Any], result: CheckResult) -> None:
    before = len(result.issues)
    check_requirements(spec, result, strict=True)
    if len(result.issues) > before:
        result.warning("QUESTION[requirements]: Confirm function, power, key parts, interfaces, mechanical, manufacturing, and risk review decisions.")


def check_power_domains_closed(spec: dict[str, Any], result: CheckResult) -> None:
    before = len(result.issues)
    check_power_domains(spec, result, strict=True)
    declared_nets = net_names(spec)
    candidate_power_nets = power_net_candidates(spec)
    covered = power_domain_nets(spec)
    for net in sorted(candidate_power_nets):
        if net not in declared_nets:
            result.issue(f"validation.spec_closure.power_nets references undeclared net: {net}")
        elif net not in covered:
            result.issue(f"power_domains do not cover power net: {net}")
    if len(result.issues) > before:
        result.warning("QUESTION[power_domains]: Provide each rail source, voltage, current limit, loads, peak current, conductor rating, and protection.")


def check_verification_closed(spec: dict[str, Any], result: CheckResult, strict: bool = False) -> None:
    before = len(result.issues)
    verification = mapping_value(spec.get("verification"))
    if not verification:
        result.issue("verification must declare SI/PI/EMC/thermal risk precheck inputs or explicit not_applicable dispositions")
        result.warning("QUESTION[verification]: Which SI, PI, EMC, and thermal risks apply, and which are not applicable?")
        return
    required_sections = closure_config(spec).get("required_verification_sections", ["signal_integrity", "power_integrity", "thermal", "emc"])
    if not isinstance(required_sections, list):
        required_sections = ["signal_integrity", "power_integrity", "thermal", "emc"]
    for section in required_sections:
        value = verification.get(str(section))
        if isinstance(value, dict) and value.get("status") == "not_applicable":
            continue
        if not isinstance(value, dict) or not value:
            result.issue(f"verification.{section} must be declared or marked status: not_applicable")
    if len(result.issues) > before:
        result.warning("QUESTION[verification]: Add thresholds/evidence for declared risks, or mark non-applicable sections explicitly.")


def todo_status(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("status", "")).strip().lower()
    return ""


def check_todo_disposition(spec: dict[str, Any], result: CheckResult) -> None:
    before = len(result.issues)
    todos = spec.get("todo", [])
    if not isinstance(todos, list):
        result.issue("todo must be a list when present")
        return
    for index, item in enumerate(todos):
        if isinstance(item, str):
            result.issue(f"todo[{index}] must be structured with category, status, blocking, owner, and next_action")
            continue
        if not isinstance(item, dict):
            result.issue(f"todo[{index}] must be a mapping")
            continue
        status = todo_status(item)
        category = str(item.get("category", "")).strip()
        if category not in TODO_ALLOWED_CATEGORIES:
            result.issue(f"todo[{index}].category must be one of {', '.join(sorted(TODO_ALLOWED_CATEGORIES))}")
        if not status:
            result.issue(f"todo[{index}].status must be declared")
        if status not in TODO_DONE_STATUSES:
            if "blocking" not in item:
                result.issue(f"todo[{index}].blocking must be declared for open TODO items")
            if not string_value(item.get("next_action")):
                result.issue(f"todo[{index}].next_action must describe the next action")
            if not string_value(item.get("owner")):
                result.issue(f"todo[{index}].owner must identify user, codex, manufacturer, lab, or engineer")
    if len(result.issues) > before:
        result.warning("QUESTION[todo]: Classify each open TODO as user_input, datasheet_review, web_dfm, bringup, lab_test, simulation, or non_blocking_note.")


def run_closure_check(spec: dict[str, Any], strict: bool = False) -> CheckResult:
    result = CheckResult()
    if not should_run(spec, strict=strict):
        result.warning("spec_closure_check skipped: no closure data or production-stage signal declared")
        return result
    areas = configured_areas(spec)
    if "stage" in areas:
        check_stage(spec, result, strict=strict)
    if "requirements" in areas:
        check_requirements_closed(spec, result)
    if "power_domains" in areas:
        check_power_domains_closed(spec, result)
    if "verification" in areas:
        check_verification_closed(spec, result, strict=strict)
    if "todo_disposition" in areas:
        check_todo_disposition(spec, result)
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Check whether a real PCB specs.yaml has closed production-impacting decisions.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])

    result = CheckResult()
    try:
        result = run_closure_check(load_spec(args.spec), strict=args.strict)
    except Exception as error:
        result.issue(str(error))
    return print_result("spec_closure_check", result, args.json_output)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
