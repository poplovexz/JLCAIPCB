#!/usr/bin/env python3
"""Validate beginner/use-case-only intake profiles for conservative MVP PCB work."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pcb_skill_checks import CheckResult, get_path, load_spec, load_yaml, print_result, string_value  # noqa: E402


def mapping_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def policy_path(spec: dict[str, Any]) -> Path:
    default = Path(__file__).resolve().parents[1] / "assets" / "beginner-intake-policy.yaml"
    configured = get_path(spec, "validation.beginner_intake_policy_file")
    if string_value(configured):
        builtin = load_yaml(default)
        if beginner_policy_override_forbidden(spec, builtin):
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


def string_set(policy: dict[str, Any], key: str) -> set[str]:
    value = policy.get(key, [])
    if not isinstance(value, list):
        return set()
    return {str(item) for item in value if string_value(item)}


def beginner_profile_required(spec: dict[str, Any], policy: dict[str, Any], require: bool = False) -> bool:
    if require:
        return True
    validation = mapping_value(spec.get("validation"))
    beginner_config = mapping_value(validation.get("beginner_intake"))
    if beginner_config.get("required") is True:
        return True

    user_profile = mapping_value(spec.get("user_profile"))
    assumption_profile = mapping_value(spec.get("assumption_profile"))
    experience = str(user_profile.get("experience", "")).strip()
    input_style = str(user_profile.get("input_style", "")).strip()
    level = str(assumption_profile.get("level", "")).strip()
    return (
        experience in string_set(policy, "beginner_experience_values")
        or input_style in string_set(policy, "use_case_input_values")
        or level in string_set(policy, "allowed_assumption_levels")
    )


def beginner_policy_override_forbidden(spec: dict[str, Any], policy: dict[str, Any]) -> bool:
    stage = str(get_path(spec, "project.stage") or "").strip()
    return beginner_profile_required(spec, policy) or stage in string_set(policy, "blocked_claim_stages")


def check_required_fields(section_name: str, data: dict[str, Any], fields: set[str], result: CheckResult) -> None:
    for field in sorted(fields):
        value = data.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            result.issue(f"{section_name}.{field} must be declared for beginner/use-case-only intake")


def structured_todo_categories(spec: dict[str, Any]) -> set[str]:
    categories: set[str] = set()
    for item in list_value(spec.get("todo")):
        if isinstance(item, dict) and string_value(item.get("category")):
            categories.add(str(item["category"]))
    return categories


def check_beginner_intake(spec: dict[str, Any], result: CheckResult, require: bool = False) -> None:
    policy = load_policy(spec)
    if (
        string_value(get_path(spec, "validation.beginner_intake_policy_file"))
        and beginner_policy_override_forbidden(spec, policy)
    ):
        result.issue("beginner/production intake policy override is forbidden; use the bundled trusted policy")
    if not beginner_profile_required(spec, policy, require=require):
        return

    user_profile = mapping_value(spec.get("user_profile"))
    assumption_profile = mapping_value(spec.get("assumption_profile"))
    if not user_profile:
        result.issue("user_profile must be declared for beginner/use-case-only intake")
    if not assumption_profile:
        result.issue("assumption_profile must be declared for beginner/use-case-only intake")

    check_required_fields("user_profile", user_profile, string_set(policy, "required_user_profile_fields"), result)
    check_required_fields("assumption_profile", assumption_profile, string_set(policy, "required_assumption_profile_fields"), result)

    experience = str(user_profile.get("experience", "")).strip()
    input_style = str(user_profile.get("input_style", "")).strip()
    level = str(assumption_profile.get("level", "")).strip()
    if experience and experience not in string_set(policy, "beginner_experience_values"):
        result.issue(f"user_profile.experience is not a beginner value: {experience}")
    if input_style and input_style not in string_set(policy, "use_case_input_values"):
        result.issue(f"user_profile.input_style is not a use-case-only value: {input_style}")
    if level and level not in string_set(policy, "allowed_assumption_levels"):
        result.issue(f"assumption_profile.level is not allowed for beginner MVP: {level}")

    for flag in sorted(string_set(policy, "required_false_flags")):
        if assumption_profile.get(flag) is not False:
            result.issue(f"assumption_profile.{flag} must be false for beginner MVP")
    for flag in sorted(string_set(policy, "required_true_flags")):
        if assumption_profile.get(flag) is not True:
            result.issue(f"assumption_profile.{flag} must be true for beginner MVP")

    stage = str(get_path(spec, "project.stage") or "").strip()
    if not stage:
        result.issue("project.stage must be declared for beginner MVP")
    elif stage in string_set(policy, "blocked_claim_stages"):
        result.issue(f"project.stage {stage} is not allowed for beginner/use-case-only intake")
    elif stage not in string_set(policy, "allowed_mvp_stages"):
        result.warning(f"project.stage {stage} is outside beginner MVP stages")

    unresolved = assumption_profile.get("unresolved_professional_decisions", [])
    if unresolved:
        if not isinstance(unresolved, list):
            result.issue("assumption_profile.unresolved_professional_decisions must be a list when present")
        elif not structured_todo_categories(spec) & string_set(policy, "professional_decision_todo_categories"):
            result.issue("professional decision assumptions must be mirrored as structured TODO items")

    result.warning("beginner/use-case-only intake active: conservative MVP may continue, but production/order-ready claims are blocked")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Validate beginner/use-case-only intake downgrade rules.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--require", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])

    result = CheckResult()
    try:
        check_beginner_intake(load_spec(args.spec), result, require=args.require)
    except Exception as error:
        result.issue(str(error))
    return print_result("beginner_intake_check", result, args.json_output)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
