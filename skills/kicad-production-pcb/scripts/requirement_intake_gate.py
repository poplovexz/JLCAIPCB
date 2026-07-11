#!/usr/bin/env python3
"""Validate the first PCB workflow stage and its transition into generation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pcb_skill_checks import CheckResult, get_path, load_yaml, print_result, string_value  # noqa: E402


def mapping_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def int_value(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def policy_path(data: dict[str, Any]) -> Path:
    default = Path(__file__).resolve().parents[1] / "assets" / "requirement-intake-policy.yaml"
    configured = get_path(data, "validation.requirement_intake.policy_file")
    if string_value(configured):
        builtin = load_yaml(default)
        if intake_policy_override_forbidden(data, builtin):
            return default
    if string_value(configured):
        path = Path(str(configured))
        return path if path.is_absolute() else Path.cwd() / path
    return default


def load_policy(data: dict[str, Any]) -> dict[str, Any]:
    try:
        return load_yaml(policy_path(data))
    except (OSError, ValueError, yaml.YAMLError):
        return {}


def string_list(policy: dict[str, Any], key: str) -> list[str]:
    return [str(item) for item in list_value(policy.get(key)) if string_value(item)]


def bool_value(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def intake_report(data: dict[str, Any]) -> dict[str, Any]:
    if isinstance(data.get("requirement_intake"), dict):
        return data["requirement_intake"]
    if isinstance(data.get("intake"), dict) and isinstance(data.get("decision"), dict):
        return data
    return {}


def should_run(data: dict[str, Any], force: bool) -> bool:
    if force:
        return True
    if intake_report(data):
        return True
    return get_path(data, "validation.requirement_intake.required") is True


def intake_policy_override_forbidden(data: dict[str, Any], policy: dict[str, Any]) -> bool:
    report = intake_report(data)
    targets = {
        str(get_path(report, "intake.desired_end_target") or "").strip(),
        str(get_path(report, "decision.current_target") or "").strip(),
    }
    protected_targets = set(string_list(policy, "protected_policy_override_targets"))
    stage = str(get_path(data, "project.stage") or "").strip()
    protected_stages = set(string_list(policy, "protected_policy_override_stages"))
    return bool(targets & protected_targets) or stage in protected_stages


def check_mapping(report: dict[str, Any], section: str, result: CheckResult) -> dict[str, Any]:
    value = report.get(section)
    if not isinstance(value, dict):
        result.issue(f"{section} must be a mapping in requirement intake report")
        return {}
    return value


def check_required_strings(section_name: str, section: dict[str, Any], fields: list[str], result: CheckResult) -> None:
    for field in fields:
        if not string_value(section.get(field)):
            result.issue(f"{section_name}.{field} must be a non-empty string")


def check_required_lists(section_name: str, section: dict[str, Any], fields: list[str], result: CheckResult) -> None:
    for field in fields:
        if not isinstance(section.get(field), list):
            result.issue(f"{section_name}.{field} must be a list")


def check_required_mixed_fields(section_name: str, section: dict[str, Any], fields: list[str], result: CheckResult) -> None:
    for field in fields:
        value = section.get(field)
        if isinstance(value, str):
            if not value.strip():
                result.issue(f"{section_name}.{field} must not be empty")
        elif isinstance(value, (list, bool)):
            continue
        elif int_value(value) is not None:
            continue
        else:
            result.issue(f"{section_name}.{field} must be declared")


def check_assumptions(section_name: str, values: Any, required_fields: list[str], result: CheckResult) -> None:
    if not isinstance(values, list):
        result.issue(f"{section_name} must be a list")
        return
    identifiers: set[str] = set()
    for index, item in enumerate(values):
        if not isinstance(item, dict):
            result.issue(f"{section_name}[{index}] must be a mapping")
            continue
        for field in required_fields:
            if not string_value(item.get(field)):
                result.issue(f"{section_name}[{index}].{field} must be a non-empty string")
        identifier = str(item.get("id", "")).strip()
        if identifier and identifier in identifiers:
            result.issue(f"{section_name}[{index}].id is duplicated: {identifier}")
        identifiers.add(identifier)


def normalized(value: Any) -> str:
    return str(value).strip().lower()


def text_contains_term(text: str, terms: list[str]) -> str | None:
    lowered = text.lower()
    for term in terms:
        if term.lower() in lowered:
            return term
    return None


def check_intake(intake: dict[str, Any], policy: dict[str, Any], result: CheckResult) -> None:
    check_required_strings("intake", intake, string_list(policy, "required_intake_fields"), result)
    desired = str(intake.get("desired_end_target", "")).strip()
    allowed = string_list(policy, "desired_end_targets")
    if desired and desired not in allowed:
        result.issue(f"intake.desired_end_target must be one of {', '.join(allowed)}; got {desired}")


def check_budget_intent(
    budget: dict[str, Any], decision: dict[str, Any], policy: dict[str, Any], result: CheckResult
) -> None:
    check_required_mixed_fields(
        "budget_intent", budget, string_list(policy, "budget_required_fields"), result
    )
    status = str(budget.get("status", "")).strip()
    allowed_statuses = set(string_list(policy, "budget_statuses"))
    if status and status not in allowed_statuses:
        result.issue(f"budget_intent.status must be one of {', '.join(sorted(allowed_statuses))}; got {status}")

    scope = str(budget.get("scope", "")).strip()
    allowed_scopes = set(string_list(policy, "budget_scopes"))
    if scope and scope not in allowed_scopes:
        result.issue(f"budget_intent.scope must be one of {', '.join(sorted(allowed_scopes))}; got {scope}")

    priority = str(budget.get("priority", "")).strip()
    priorities = set(string_list(policy, "budget_priorities"))
    if priority and priority not in priorities:
        result.issue(f"budget_intent.priority must be one of {', '.join(sorted(priorities))}; got {priority}")

    includes = budget.get("includes")
    allowed_categories = set(string_list(policy, "budget_include_categories"))
    if not isinstance(includes, list) or any(not string_value(item) for item in includes):
        result.issue("budget_intent.includes must be a list of non-empty cost categories")
        includes = []
    normalized_includes = [str(item).strip() for item in includes]
    if len(normalized_includes) != len(set(normalized_includes)):
        result.issue("budget_intent.includes must not contain duplicates")
    for category in normalized_includes:
        if category not in allowed_categories:
            result.issue(f"budget_intent.includes contains unsupported category: {category}")

    if bool_value(budget.get("allow_mvp_without_limit")) is None:
        result.issue("budget_intent.allow_mvp_without_limit must be true or false")

    minimum = float(policy.get("budget_minimum_amount", 0.01))
    maximum = float(policy.get("budget_maximum_amount", 1000000000))
    target_amount = budget.get("target_amount")
    maximum_amount = budget.get("maximum_amount")
    quantity_basis = budget.get("quantity_basis")
    known = status in set(string_list(policy, "budget_known_statuses"))
    if known:
        if not string_value(budget.get("currency")):
            result.issue("budget_intent.currency is required when a budget limit is known")
        elif str(budget.get("currency", "")).strip() in set(string_list(policy, "budget_unresolved_currency_values")):
            result.issue("budget_intent.currency must be resolved when a budget limit is known")
        if not isinstance(maximum_amount, (int, float)) or isinstance(maximum_amount, bool):
            result.issue("budget_intent.maximum_amount must be numeric when the budget is known")
        elif not minimum <= float(maximum_amount) <= maximum:
            result.issue(f"budget_intent.maximum_amount must be between {minimum} and {maximum}")
        if target_amount is not None:
            if not isinstance(target_amount, (int, float)) or isinstance(target_amount, bool):
                result.issue("budget_intent.target_amount must be numeric when declared")
            elif not minimum <= float(target_amount) <= maximum:
                result.issue(f"budget_intent.target_amount must be between {minimum} and {maximum}")
            elif isinstance(maximum_amount, (int, float)) and float(target_amount) > float(maximum_amount):
                result.issue("budget_intent.target_amount must not exceed maximum_amount")
        if int_value(quantity_basis) is None or int(quantity_basis) < 1:
            result.issue("budget_intent.quantity_basis must be a positive integer when the budget is known")
        elif int(quantity_basis) > int(policy.get("budget_maximum_quantity_basis", 1000000)):
            result.issue("budget_intent.quantity_basis exceeds the policy maximum")
    elif status == "unknown":
        if target_amount is not None or maximum_amount is not None:
            result.issue("unknown budget must not invent target_amount or maximum_amount")
        if scope != str(policy.get("budget_unknown_scope", "unknown")):
            result.issue("unknown budget must use the policy-defined unknown scope")
        if budget.get("allow_mvp_without_limit") is not policy.get("budget_unknown_allows_mvp", True):
            result.issue("unknown budget must explicitly allow only the policy-defined MVP fallback")

    current_target = str(decision.get("current_target", "")).strip()
    production_targets = set(string_list(policy, "budget_targets_requiring_known_limit"))
    if current_target in production_targets:
        if not known:
            result.issue(f"budget_intent must have a known limit before current target {current_target}")
        missing = set(string_list(policy, "budget_required_production_categories")) - set(normalized_includes)
        if missing:
            result.issue(
                "budget_intent.includes is missing production cost categories: " + ", ".join(sorted(missing))
            )
    elif status == "unknown":
        result.warning("budget is unknown; requirements and local MVP may continue, but production cost closure is blocked")


def check_decision(decision: dict[str, Any], policy: dict[str, Any], result: CheckResult) -> None:
    check_required_mixed_fields("decision", decision, string_list(policy, "required_decision_fields"), result)
    target = str(decision.get("current_target", "")).strip()
    allowed_targets = string_list(policy, "allowed_targets")
    if target and target not in allowed_targets:
        result.issue(f"decision.current_target must be one of {', '.join(allowed_targets)}; got {target}")
    for field in string_list(policy, "decision_boolean_fields"):
        if bool_value(decision.get(field)) is None:
            result.issue(f"decision.{field} must be true or false")


def check_safety_screening(
    intake: dict[str, Any], screening: dict[str, Any], policy: dict[str, Any], result: CheckResult
) -> None:
    check_required_mixed_fields("safety_screening", screening, string_list(policy, "safety_required_fields"), result)
    level = str(screening.get("level", "")).strip()
    levels = string_list(policy, "safety_levels")
    if level and level not in levels:
        result.issue(f"safety_screening.level must be one of {', '.join(levels)}; got {level}")
    hazards = screening.get("hazards")
    if not isinstance(hazards, list):
        result.issue("safety_screening.hazards must be a list")
        hazards = []
    if level in set(string_list(policy, "safety_levels_requiring_hazards")) and not hazards:
        result.issue(f"safety_screening.hazards must identify the elevated hazards for level {level}")
    if level in set(string_list(policy, "safety_levels_requiring_block")) and screening.get("blocks_automatic_assumptions") is not True:
        result.issue(f"safety_screening.blocks_automatic_assumptions must be true for level {level}")

    screening_text = " ".join(
        str(intake.get(field, "")) for field in string_list(policy, "safety_scan_intake_fields")
    )
    elevated_term = text_contains_term(screening_text, string_list(policy, "safety_elevated_terms"))
    if elevated_term and level == str(policy.get("safety_standard_level", "")):
        result.issue(
            f"safety_screening.level cannot be standard because intake contains elevated-risk term '{elevated_term}'"
        )


def check_evidence_inputs(evidence: dict[str, Any], policy: dict[str, Any], result: CheckResult) -> None:
    check_required_lists("evidence_inputs", evidence, string_list(policy, "evidence_required_fields"), result)
    allowed_types = set(string_list(policy, "evidence_types"))
    requested = evidence.get("requested")
    if not isinstance(requested, list):
        return
    for index, item in enumerate(requested):
        if not isinstance(item, dict):
            result.issue(f"evidence_inputs.requested[{index}] must be a mapping")
            continue
        check_required_strings(
            f"evidence_inputs.requested[{index}]",
            item,
            string_list(policy, "evidence_request_required_fields"),
            result,
        )
        evidence_type = str(item.get("type", "")).strip()
        if evidence_type and evidence_type not in allowed_types:
            result.issue(
                f"evidence_inputs.requested[{index}].type must be one of {', '.join(sorted(allowed_types))}; got {evidence_type}"
            )


def check_confirmation(
    confirmation: dict[str, Any], decision: dict[str, Any], policy: dict[str, Any], result: CheckResult
) -> None:
    check_required_mixed_fields(
        "confirmation", confirmation, string_list(policy, "confirmation_required_fields"), result
    )
    status = str(confirmation.get("status", "")).strip()
    statuses = string_list(policy, "confirmation_statuses")
    if status and status not in statuses:
        result.issue(f"confirmation.status must be one of {', '.join(statuses)}; got {status}")

    intake_revision = int_value(confirmation.get("intake_revision"))
    confirmed_revision = int_value(confirmation.get("confirmed_revision"))
    if intake_revision is None or intake_revision < 1:
        result.issue("confirmation.intake_revision must be a positive integer")
    if confirmed_revision is None or confirmed_revision < 0:
        result.issue("confirmation.confirmed_revision must be a non-negative integer")

    confirmed_by = str(confirmation.get("confirmed_by", "")).strip()
    confirmed_status = str(policy.get("confirmation_confirmed_status", ""))
    unconfirmed_statuses = set(string_list(policy, "confirmation_unconfirmed_statuses"))
    if status == confirmed_status:
        if confirmed_by not in set(string_list(policy, "confirmation_user_values")):
            result.issue("confirmation.confirmed_by must identify the user for confirmed intake")
        if intake_revision is not None and confirmed_revision != intake_revision:
            result.issue("confirmation.confirmed_revision must equal intake_revision; stale confirmation is not valid")
        for field in string_list(policy, "confirmation_confirmed_must_be_true"):
            if decision.get(field) is not True:
                result.issue(f"decision.{field} must be true after user confirmation")
    elif status in unconfirmed_statuses:
        if confirmed_by not in set(string_list(policy, "confirmation_unconfirmed_values")):
            result.issue("confirmation.confirmed_by must be none until the user confirms the intake")
        if confirmed_revision not in {None, 0}:
            result.issue("confirmation.confirmed_revision must be 0 until the user confirms the intake")
        target = str(decision.get("current_target", "")).strip()
        if target not in set(string_list(policy, "confirmation_unconfirmed_targets")):
            result.issue("decision.current_target must remain requirements-only until user confirmation")
        for field in string_list(policy, "confirmation_unconfirmed_must_be_false"):
            if decision.get(field) is not False:
                result.issue(f"decision.{field} must be false until user confirmation")


def beginner_mode_enabled(report: dict[str, Any], policy: dict[str, Any]) -> bool:
    if isinstance(report.get("beginner"), dict):
        return True
    intake = mapping_value(report.get("intake"))
    return str(intake.get("input_style", "")).strip() in set(string_list(policy, "beginner_input_styles"))


def contains_unknown_choice(choice: str, tokens: list[str]) -> bool:
    value = normalized(choice)
    return any(normalized(token) in value for token in tokens)


def check_beginner_questions(beginner: dict[str, Any], policy: dict[str, Any], result: CheckResult) -> None:
    section_name = str(policy.get("beginner_required_section", "beginner"))
    questions = beginner.get("questions")
    if not isinstance(questions, list):
        result.issue(f"{section_name}.questions must be a list")
        return

    max_questions = int(policy.get("beginner_max_questions", 5))
    declared_max = int_value(beginner.get("max_questions"))
    if declared_max is None or declared_max < 1 or declared_max > max_questions:
        result.issue(f"{section_name}.max_questions must be between 1 and {max_questions}")
    if len(questions) > max_questions:
        result.issue(f"{section_name}.questions has {len(questions)} items; maximum is {max_questions}")

    allowed_categories = set(string_list(policy, "beginner_question_categories"))
    allowed_blocks = set(string_list(policy, "beginner_question_blocks"))
    forbidden_terms = string_list(policy, "beginner_forbidden_question_terms")
    required_fields = string_list(policy, "beginner_question_required_fields")
    unknown_tokens = string_list(policy, "beginner_unknown_choice_tokens")
    min_choices = int(policy.get("beginner_min_choices", 2))
    max_choices = int(policy.get("beginner_max_choices", 6))
    identifiers: set[str] = set()

    for index, question in enumerate(questions):
        prefix = f"{section_name}.questions[{index}]"
        if not isinstance(question, dict):
            result.issue(f"{prefix} must be a mapping")
            continue
        check_required_mixed_fields(prefix, question, required_fields, result)

        identifier = str(question.get("id", "")).strip()
        if identifier and identifier in identifiers:
            result.issue(f"{prefix}.id is duplicated: {identifier}")
        identifiers.add(identifier)

        category = str(question.get("category", "")).strip()
        if category and category not in allowed_categories:
            result.issue(f"{prefix}.category must be one of {', '.join(sorted(allowed_categories))}; got {category}")
        blocks = str(question.get("blocks", "")).strip()
        if blocks and blocks not in allowed_blocks:
            result.issue(f"{prefix}.blocks must be one of {', '.join(sorted(allowed_blocks))}; got {blocks}")

        choices = question.get("choices")
        if not isinstance(choices, list) or any(not string_value(choice) for choice in choices):
            result.issue(f"{prefix}.choices must be a list of non-empty strings")
            choices = []
        if choices and not min_choices <= len(choices) <= max_choices:
            result.issue(f"{prefix}.choices must contain between {min_choices} and {max_choices} choices")
        normalized_choices = [normalized(choice) for choice in choices]
        if len(normalized_choices) != len(set(normalized_choices)):
            result.issue(f"{prefix}.choices must not contain duplicates")
        if question.get("allow_unknown") is not True:
            result.issue(f"{prefix}.allow_unknown must be true for beginner intake")
        if choices and not any(contains_unknown_choice(str(choice), unknown_tokens) for choice in choices):
            result.issue(f"{prefix}.choices must include an explicit unknown/not-sure option")

        recommended = str(question.get("recommended_choice", "")).strip()
        if recommended and normalized(recommended) not in normalized_choices:
            result.issue(f"{prefix}.recommended_choice must match one of the declared choices")
        if recommended and contains_unknown_choice(recommended, unknown_tokens):
            result.issue(f"{prefix}.recommended_choice must be a concrete choice, not the unknown option")

        searchable = " ".join(
            [str(question.get("question", "")), str(question.get("reason", "")), *[str(choice) for choice in choices]]
        )
        forbidden = text_contains_term(searchable, forbidden_terms)
        if forbidden:
            result.issue(f"{prefix} uses professional term '{forbidden}' in a beginner first-round question")


def check_beginner_mode(report: dict[str, Any], policy: dict[str, Any], result: CheckResult) -> None:
    if not beginner_mode_enabled(report, policy):
        return
    section_name = str(policy.get("beginner_required_section", "beginner"))
    beginner = report.get(section_name)
    if not isinstance(beginner, dict):
        result.issue(f"{section_name} section is required when intake.input_style is beginner/use-case-only")
        return

    check_required_mixed_fields(section_name, beginner, string_list(policy, "beginner_required_fields"), result)
    input_style = str(beginner.get("input_style", "")).strip()
    if input_style and input_style not in set(string_list(policy, "beginner_input_styles")):
        result.issue(f"{section_name}.input_style must be one of {', '.join(string_list(policy, 'beginner_input_styles'))}")

    max_rounds = int(policy.get("beginner_max_rounds", 2))
    declared_rounds = int_value(beginner.get("max_rounds"))
    current_round = int_value(beginner.get("round"))
    if declared_rounds is None or declared_rounds < 1 or declared_rounds > max_rounds:
        result.issue(f"{section_name}.max_rounds must be between 1 and {max_rounds}")
    if current_round is None or current_round < 1 or (declared_rounds is not None and current_round > declared_rounds):
        result.issue(f"{section_name}.round must be between 1 and the declared max_rounds")

    resolution_mode = str(beginner.get("resolution_mode", "")).strip()
    modes = string_list(policy, "beginner_resolution_modes")
    if resolution_mode and resolution_mode not in modes:
        result.issue(f"{section_name}.resolution_mode must be one of {', '.join(modes)}; got {resolution_mode}")

    check_beginner_questions(beginner, policy, result)
    questions = beginner.get("questions") if isinstance(beginner.get("questions"), list) else []
    question_mode = str(policy.get("beginner_question_resolution_mode", ""))
    if resolution_mode == question_mode and not questions:
        result.issue(f"{section_name}.resolution_mode {question_mode} requires at least one question")
    if resolution_mode != question_mode and questions:
        result.issue(f"{section_name}.questions must be empty when resolution_mode is {resolution_mode}")

    decision = mapping_value(report.get("decision"))
    target = str(decision.get("current_target", "")).strip()
    if target and target not in set(string_list(policy, "beginner_allowed_targets")):
        result.issue(f"beginner intake cannot currently target {target}; use requirements-only, draft-spec, or local-mvp")

    safe_count = len(report.get("safe_assumptions") if isinstance(report.get("safe_assumptions"), list) else [])
    unsafe_count = len(report.get("unsafe_assumptions") if isinstance(report.get("unsafe_assumptions"), list) else [])
    if safe_count < int(policy.get("beginner_min_safe_assumptions", 1)):
        result.issue("beginner intake must include safe_assumptions instead of asking every detail")
    if unsafe_count < int(policy.get("beginner_min_unsafe_assumptions", 1)):
        result.issue("beginner intake must include unsafe_assumptions for professional unknowns that block production/order-ready")
    if not isinstance(beginner.get("deferred_professional_topics"), list):
        result.issue(f"{section_name}.deferred_professional_topics must be a list")


def check_before_generation(
    data: dict[str, Any], report: dict[str, Any], policy: dict[str, Any], result: CheckResult
) -> None:
    decision = mapping_value(report.get("decision"))
    confirmation = mapping_value(report.get("confirmation"))
    if confirmation.get("status") != policy.get("confirmation_confirmed_status"):
        result.issue("Cannot generate KiCad until the user confirms the current intake revision")
    target = str(decision.get("current_target", "")).strip()
    stage = str(get_path(data, "project.stage") or "").strip()
    if target in set(string_list(policy, "blocked_generation_targets")):
        result.issue(f"Cannot generate KiCad from requirement intake target: {target}")
    if stage in set(string_list(policy, "blocked_generation_stages")):
        result.issue(f"Cannot generate KiCad while project.stage is {stage}")
    if decision.get("can_generate_kicad") is not True:
        result.issue("Requirement intake decision does not allow KiCad generation")
    if decision.get("can_run_erc_drc") is not True:
        result.issue("Requirement intake decision does not allow ERC/DRC")


def check_requirement_intake(
    data: dict[str, Any], result: CheckResult, force: bool = False, before_generation: bool = False
) -> dict[str, Any]:
    policy = load_policy(data)
    details = {"policy": str(policy_path(data)), "enabled": should_run(data, force)}
    if not details["enabled"]:
        result.warning("requirement intake gate not required for this input")
        return details
    if (
        string_value(get_path(data, "validation.requirement_intake.policy_file"))
        and intake_policy_override_forbidden(data, policy)
    ):
        result.issue("production requirement-intake policy override is forbidden; use the bundled trusted policy")

    report = intake_report(data)
    if not report:
        result.issue("requirement_intake report is required before spec, KiCad, ERC/DRC, or production work")
        return details

    for section in string_list(policy, "required_top_sections"):
        if section not in report:
            result.issue(f"requirement intake report missing section: {section}")

    intake = check_mapping(report, "intake", result)
    safety = check_mapping(report, "safety_screening", result)
    evidence = check_mapping(report, "evidence_inputs", result)
    budget = check_mapping(report, "budget_intent", result)
    missing = check_mapping(report, "missing_information", result)
    confirmation = check_mapping(report, "confirmation", result)
    decision = check_mapping(report, "decision", result)

    check_intake(intake, policy, result)
    check_budget_intent(budget, decision, policy, result)
    check_safety_screening(intake, safety, policy, result)
    check_evidence_inputs(evidence, policy, result)
    check_required_lists("missing_information", missing, string_list(policy, "required_missing_information_fields"), result)
    check_assumptions(
        "safe_assumptions", report.get("safe_assumptions"), string_list(policy, "required_safe_assumption_fields"), result
    )
    check_assumptions(
        "unsafe_assumptions", report.get("unsafe_assumptions"), string_list(policy, "required_unsafe_assumption_fields"), result
    )
    check_decision(decision, policy, result)
    check_confirmation(confirmation, decision, policy, result)
    check_beginner_mode(report, policy, result)
    if before_generation:
        check_before_generation(data, report, policy, result)

    details["current_target"] = decision.get("current_target")
    details["desired_end_target"] = intake.get("desired_end_target")
    details["confirmation_status"] = confirmation.get("status")
    details["can_create_spec"] = decision.get("can_create_spec")
    details["can_generate_kicad"] = decision.get("can_generate_kicad")
    details["can_run_erc_drc"] = decision.get("can_run_erc_drc")
    return details


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Validate requirement intake reports before PCB workflow advances.")
    parser.add_argument("path", type=Path)
    parser.add_argument("--require", action="store_true")
    parser.add_argument("--before-generation", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])

    result = CheckResult()
    details: dict[str, Any] = {}
    try:
        details = check_requirement_intake(
            load_yaml(args.path), result, force=args.require, before_generation=args.before_generation
        )
    except Exception as error:
        result.issue(str(error))

    if args.json_output:
        print(
            json.dumps(
                {
                    "check": "requirement_intake_gate",
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
            "requirement_intake_gate state: "
            f"enabled={details.get('enabled')} "
            f"target={details.get('current_target') or '<none>'} "
            f"desired={details.get('desired_end_target') or '<none>'} "
            f"confirmation={details.get('confirmation_status') or '<none>'} "
            f"can_generate_kicad={details.get('can_generate_kicad')}"
        )
    return print_result("requirement_intake_gate", result, False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
