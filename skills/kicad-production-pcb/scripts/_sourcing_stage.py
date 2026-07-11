#!/usr/bin/env python3
"""Deterministic sourcing-stage validation and ranking helpers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from _pcb_skill_checks import CheckResult, get_path, load_json, load_yaml, sha256_file, string_value
from architecture_gate import architecture_digest


def mapping_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def string_list(value: Any) -> list[str]:
    return [str(item).strip() for item in list_value(value) if string_value(item)]


def normalized(value: Any) -> str:
    return str(value).strip().lower().replace("_", "-") if value is not None else ""


def normalized_mapping(value: Any) -> dict[str, Any]:
    return {normalized(key): item for key, item in mapping_value(value).items()}


def integer_value(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def number_value(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def has_unknown_token(value: Any, policy: dict[str, Any]) -> bool:
    if not string_value(value):
        return True
    text = str(value).upper()
    return any(token.upper() in text for token in policy_strings(policy, "unknown_tokens"))


def policy_path(spec: dict[str, Any]) -> Path:
    default = Path(__file__).resolve().parents[1] / "assets" / "sourcing-stage-policy.yaml"
    configured = get_path(spec, "validation.part_selection.sourcing_policy_file")
    if string_value(configured):
        builtin = load_yaml(default)
        stage = normalized(get_path(spec, "project.stage"))
        production = {normalized(value) for value in policy_strings(builtin, "production_stage_values")}
        if stage in production:
            return default
    if string_value(configured):
        path = Path(str(configured))
        return path if path.is_absolute() else Path.cwd() / path
    return default


def load_policy(spec: dict[str, Any]) -> dict[str, Any]:
    return load_yaml(policy_path(spec))


def policy_strings(policy: dict[str, Any], dotted_path: str) -> list[str]:
    return string_list(get_path(policy, dotted_path))


def parse_timestamp(value: Any) -> datetime | None:
    if not string_value(value):
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def effective_now(as_of: str | datetime | None = None) -> datetime:
    if isinstance(as_of, datetime):
        if as_of.tzinfo is None:
            raise ValueError("as-of timestamp must include a timezone")
        return as_of.astimezone(timezone.utc)
    if as_of is not None:
        parsed = parse_timestamp(as_of)
        if parsed is None:
            raise ValueError("as-of timestamp must be ISO-8601 with a timezone")
        return parsed
    return datetime.now(timezone.utc)


def timestamp_issue(value: Any, now: datetime, max_age_hours: float, label: str) -> str | None:
    parsed = parse_timestamp(value)
    if parsed is None:
        return f"{label} must be an ISO-8601 timestamp with a timezone"
    age_hours = (now - parsed).total_seconds() / 3600.0
    if age_hours < -0.1:
        return f"{label} is in the future"
    if age_hours > max_age_hours:
        return f"{label} is stale ({age_hours:.1f}h > {max_age_hours:.1f}h)"
    return None


def sourcing_required(spec: dict[str, Any], policy: dict[str, Any], force: bool = False) -> bool:
    if force:
        return True
    config = mapping_value(get_path(spec, "validation.part_selection"))
    if config.get("required") is True:
        return True
    if isinstance(spec.get("sourcing"), dict):
        return True
    if config.get("enabled") is False:
        return False
    stage = normalized(get_path(spec, "project.stage"))
    production_stages = {normalized(value) for value in policy_strings(policy, "production_stage_values")}
    target = normalized(get_path(spec, "manufacturing.target"))
    targets = {normalized(value) for value in policy_strings(policy, "manufacturing_targets")}
    return stage in production_stages and target in targets


def resolved_path(value: Any, root: Path | None = None) -> Path:
    path = Path(str(value))
    return path.resolve() if path.is_absolute() else ((root or Path.cwd()) / path).resolve()


def project_root(
    spec: dict[str, Any],
    spec_path: Path | None,
    policy: dict[str, Any],
    result: CheckResult | None = None,
) -> Path:
    root_field = str(get_path(policy, "path_resolution.project_root_field") or "project.root_dir")
    configured = get_path(spec, root_field)
    stage = normalized(get_path(spec, "project.stage"))
    production = {normalized(value) for value in policy_strings(policy, "production_stage_values")}
    if string_value(configured):
        base = spec_path.resolve().parent if spec_path is not None else Path.cwd()
        root = resolved_path(configured, base)
        if result is not None and get_path(policy, "path_resolution.forbid_filesystem_root") is True and root == root.parent:
            result.issue(f"{root_field} must not be the filesystem root")
        if (
            result is not None
            and spec_path is not None
            and get_path(policy, "path_resolution.require_spec_under_project_root") is True
        ):
            try:
                spec_path.resolve().relative_to(root)
            except ValueError:
                result.issue(f"spec path must stay under {root_field}: {root}")
        return root
    if (
        result is not None
        and stage in production
        and get_path(policy, "path_resolution.require_explicit_root_in_production") is True
    ):
        result.issue(f"{root_field} is required for deterministic production path resolution")
    return Path.cwd().resolve()


def configured_artifact_path(
    spec: dict[str, Any],
    policy: dict[str, Any],
    field_key: str,
    result: CheckResult,
    root: Path | None = None,
) -> Path | None:
    dotted_path = get_path(policy, f"artifact_rules.{field_key}")
    if not string_value(dotted_path):
        result.issue(f"sourcing policy artifact_rules.{field_key} must name a spec field")
        return None
    configured = get_path(spec, str(dotted_path))
    if not string_value(configured) or has_unknown_token(configured, policy):
        result.issue(f"{dotted_path} must be a non-empty path")
        return None
    target = resolved_path(configured, root)
    if get_path(policy, "artifact_rules.require_under_project_artifacts") is True:
        artifacts = get_path(spec, "project.artifacts_dir")
        if not string_value(artifacts):
            result.issue("project.artifacts_dir is required for sourcing artifacts")
            return target
        artifacts_root = resolved_path(artifacts, root)
        if root is not None and get_path(policy, "path_resolution.require_artifacts_under_project_root") is True:
            try:
                artifacts_root.relative_to(root)
            except ValueError:
                result.issue(f"project.artifacts_dir must stay under project.root_dir: {artifacts_root}")
        try:
            target.relative_to(artifacts_root)
        except ValueError:
            result.issue(f"{dotted_path} must stay under project.artifacts_dir: {target}")
    return target


def load_data_file(path: Path) -> dict[str, Any]:
    if path.suffix.lower() == ".json":
        return load_json(path)
    return load_yaml(path)


def atomic_write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    os.replace(temporary, path)


def architecture_maps(spec: dict[str, Any], policy: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    architecture = mapping_value(spec.get("architecture"))
    blocks = {
        str(item["id"]).strip(): item
        for item in list_value(architecture.get("blocks"))
        if isinstance(item, dict) and string_value(item.get("id"))
    }
    constraint_field = str(
        get_path(policy, "architecture_requirements.selection_constraints_field") or "selection_constraints"
    )
    constraints: dict[str, dict[str, Any]] = {}
    for block_id, block in blocks.items():
        for item in list_value(block.get(constraint_field)):
            if isinstance(item, dict) and string_value(item.get("id")):
                constraints[str(item["id"]).strip()] = {"block_id": block_id, **item}
    return blocks, constraints


def candidate_map_for_manifest(manifest: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    output: dict[tuple[str, str], dict[str, Any]] = {}
    for batch in list_value(manifest.get("batches")):
        if not isinstance(batch, dict):
            continue
        requirement_id = str(batch.get("requirement_id", "")).strip()
        for candidate in list_value(batch.get("candidates")):
            if isinstance(candidate, dict) and string_value(candidate.get("id")):
                output[(requirement_id, str(candidate["id"]).strip())] = candidate
    return output


def validate_criterion(
    criterion: Any,
    label: str,
    policy: dict[str, Any],
    result: CheckResult,
) -> dict[str, Any] | None:
    if not isinstance(criterion, dict):
        result.issue(f"{label} must be a mapping")
        return None
    compound_fields = policy_strings(policy, "machine_constraint.compound_fields")
    declared_compounds = [field for field in compound_fields if field in criterion]
    if len(declared_compounds) > 1:
        result.issue(f"{label} must not combine multiple compound criterion fields")
        return criterion
    if declared_compounds:
        field = declared_compounds[0]
        children = criterion.get(field)
        if not isinstance(children, list) or not children:
            result.issue(f"{label}.{field} must be a non-empty list")
        else:
            for index, child in enumerate(children):
                validate_criterion(child, f"{label}.{field}[{index}]", policy, result)
        condition_field = str(get_path(policy, "machine_constraint.condition_field") or "when")
        if condition_field in criterion:
            validate_criterion(criterion.get(condition_field), f"{label}.{condition_field}", policy, result)
        return criterion

    condition_field = str(get_path(policy, "machine_constraint.condition_field") or "when")
    if condition_field in criterion:
        validate_criterion(criterion.get(condition_field), f"{label}.{condition_field}", policy, result)
    required_fields = policy_strings(policy, "machine_constraint.required_fields")
    for field in required_fields:
        if not string_value(criterion.get(field)) or has_unknown_token(criterion.get(field), policy):
            result.issue(f"{label}.{field} must be a non-empty string")
    operator = normalized(criterion.get("operator"))
    operators = {normalized(value) for value in policy_strings(policy, "machine_constraint.operators")}
    if operator not in operators:
        result.issue(f"{label}.operator is unsupported: {criterion.get('operator')}")
    optional_value = {normalized(value) for value in policy_strings(policy, "machine_constraint.value_optional_operators")}
    if operator not in optional_value and "value" not in criterion:
        result.issue(f"{label}.value is required for operator {operator}")
    if isinstance(criterion.get("value"), str) and has_unknown_token(criterion.get("value"), policy):
        result.issue(f"{label}.value contains an unresolved placeholder")
    if isinstance(criterion.get("value"), list) and any(
        isinstance(item, str) and has_unknown_token(item, policy) for item in criterion["value"]
    ):
        result.issue(f"{label}.value contains an unresolved placeholder")
    numeric_operators = {normalized(value) for value in policy_strings(policy, "machine_constraint.numeric_operators")}
    if operator in numeric_operators and number_value(criterion.get("value")) is None:
        result.issue(f"{label}.value must be numeric for operator {operator}")
    minimum_multiplier = Decimal(str(get_path(policy, "machine_constraint.minimum_multiplier") or 0))
    maximum_multiplier = Decimal(str(get_path(policy, "machine_constraint.maximum_multiplier") or 10))
    for field in policy_strings(policy, "machine_constraint.numeric_adjustment_fields"):
        if field not in criterion:
            continue
        multiplier = decimal_value(criterion.get(field))
        if multiplier is None or multiplier < minimum_multiplier or multiplier > maximum_multiplier:
            result.issue(f"{label}.{field} must be between {minimum_multiplier} and {maximum_multiplier}")
    return criterion


def decimal_value(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        return None
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    return parsed if parsed.is_finite() else None


def architecture_collection_map(spec: dict[str, Any], collection: str) -> dict[str, dict[str, Any]]:
    return {
        str(item["id"]).strip(): item
        for item in list_value(get_path(spec, f"architecture.{collection}"))
        if isinstance(item, dict) and string_value(item.get("id"))
    }


def check_cost_budget(
    spec: dict[str, Any],
    context: dict[str, Any],
    policy: dict[str, Any],
    result: CheckResult,
    production: bool,
) -> dict[str, Any]:
    budget = mapping_value(context.get("cost_budget"))
    allowed_mvp = {normalized(value) for value in policy_strings(policy, "cost_budget.mvp_statuses")}
    required_status = normalized(get_path(policy, "cost_budget.production_status"))
    status = normalized(budget.get("status"))
    if not budget:
        if production:
            result.issue("sourcing.context.cost_budget is required for production sourcing")
        return {"status": "missing", "estimate_total": 0.0}
    if production and status != required_status:
        result.issue(f"sourcing.context.cost_budget.status must be {required_status} for production sourcing")
    elif not production and status not in allowed_mvp:
        result.issue("sourcing.context.cost_budget.status is unsupported")

    for field in policy_strings(policy, "cost_budget.required_fields"):
        if field not in budget or budget.get(field) is None:
            result.issue(f"sourcing.context.cost_budget.{field} must be declared")
    currency = str(budget.get("currency", "")).strip()
    if currency != str(context.get("currency", "")).strip():
        result.issue("sourcing.context.cost_budget.currency must match sourcing.context.currency")
    quantity_basis = integer_value(budget.get("quantity_basis"))
    board_quantity = integer_value(context.get("board_quantity"))
    if quantity_basis is None or quantity_basis < 1:
        result.issue("sourcing.context.cost_budget.quantity_basis must be a positive integer")
    elif board_quantity is not None and quantity_basis != board_quantity:
        result.issue("sourcing.context.cost_budget.quantity_basis must equal sourcing.context.board_quantity")

    minimum = Decimal(str(get_path(policy, "cost_budget.minimum_amount") or 0))
    maximum = Decimal(str(get_path(policy, "cost_budget.maximum_amount") or 1000000000))
    numeric_fields = (
        "maximum_component_cost_per_board",
        "maximum_component_order_total",
        "maximum_order_total",
    )
    amounts: dict[str, Decimal] = {}
    for field in numeric_fields:
        parsed = decimal_value(budget.get(field))
        if parsed is None or parsed < minimum or parsed > maximum:
            result.issue(f"sourcing.context.cost_budget.{field} must be between {minimum} and {maximum}")
        else:
            amounts[field] = parsed
    if (
        "maximum_component_order_total" in amounts
        and "maximum_order_total" in amounts
        and amounts["maximum_component_order_total"] > amounts["maximum_order_total"]
    ):
        result.issue("maximum_component_order_total must not exceed maximum_order_total")

    intake_report = mapping_value(spec.get("requirement_intake")) or spec
    intake_budget = mapping_value(intake_report.get("budget_intent"))
    if production and not intake_budget:
        result.issue("confirmed requirement_intake.budget_intent is required before production sourcing")
    if intake_budget:
        if str(intake_budget.get("currency", "")).strip() != currency:
            result.issue("sourcing cost budget currency must match confirmed intake budget currency")
        source_revision = integer_value(budget.get("source_intake_revision"))
        confirmed_revision = integer_value(get_path(intake_report, "confirmation.confirmed_revision"))
        if source_revision is None or confirmed_revision is None or source_revision != confirmed_revision:
            result.issue("sourcing cost budget source_intake_revision must match the confirmed intake revision")
        intake_maximum = decimal_value(intake_budget.get("maximum_amount"))
        intake_quantity = integer_value(intake_budget.get("quantity_basis"))
        if intake_maximum is not None and intake_quantity and board_quantity and "maximum_order_total" in amounts:
            normalized_limit = intake_maximum * Decimal(board_quantity) / Decimal(intake_quantity)
            if amounts["maximum_order_total"] > normalized_limit:
                result.issue("sourcing maximum_order_total exceeds the user-confirmed budget limit")

    estimate_lines = budget.get("estimate_lines")
    if not isinstance(estimate_lines, list):
        result.issue("sourcing.context.cost_budget.estimate_lines must be a list")
        estimate_lines = []
    line_ids: set[str] = set()
    categories: set[str] = set()
    estimate_total = Decimal(0)
    allowed_bases = {normalized(value) for value in policy_strings(policy, "cost_budget.estimate_line_bases")}
    allowed_confidence = {normalized(value) for value in policy_strings(policy, "cost_budget.confidence_values")}
    for index, line in enumerate(estimate_lines):
        prefix = f"sourcing.context.cost_budget.estimate_lines[{index}]"
        if not isinstance(line, dict):
            result.issue(f"{prefix} must be a mapping")
            continue
        for field in policy_strings(policy, "cost_budget.required_estimate_line_fields"):
            if field not in line or line.get(field) is None or (isinstance(line.get(field), str) and not line[field].strip()):
                result.issue(f"{prefix}.{field} must be declared")
        identifier = str(line.get("id", "")).strip()
        if identifier in line_ids:
            result.issue(f"{prefix}.id is duplicated: {identifier}")
        line_ids.add(identifier)
        category = str(line.get("category", "")).strip()
        categories.add(category)
        amount = decimal_value(line.get("amount"))
        if amount is None or amount < minimum or amount > maximum:
            result.issue(f"{prefix}.amount must be between {minimum} and {maximum}")
        else:
            estimate_total += amount
        if normalized(line.get("basis")) not in allowed_bases:
            result.issue(f"{prefix}.basis is unsupported")
        if normalized(line.get("confidence")) not in allowed_confidence:
            result.issue(f"{prefix}.confidence is unsupported")

    intake_categories = set(string_list(intake_budget.get("includes"))) if intake_budget else set()
    if production and get_path(context, "assembly.enabled") is True and "assembly" not in intake_categories:
        result.issue("confirmed intake budget must include assembly when PCBA sourcing is enabled")
    required_categories = intake_categories - {"components"}
    for category in sorted(required_categories - categories):
        result.issue(f"sourcing cost estimate is missing intake budget category: {category}")
    return {
        "status": status,
        "currency": currency,
        "estimate_total": float(estimate_total),
        "maximum_component_cost_per_board": float(amounts.get("maximum_component_cost_per_board", 0)),
        "maximum_component_order_total": float(amounts.get("maximum_component_order_total", 0)),
        "maximum_order_total": float(amounts.get("maximum_order_total", 0)),
    }


def check_component_roles(
    spec: dict[str, Any],
    sourcing: dict[str, Any],
    requirements: dict[str, dict[str, Any]],
    components: dict[str, dict[str, Any]],
    blocks: dict[str, dict[str, Any]],
    constraints: dict[str, dict[str, Any]],
    policy: dict[str, Any],
    result: CheckResult,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    roles: dict[str, dict[str, Any]] = {}
    role_owner: dict[str, str] = {}
    allowed_kinds = {normalized(value) for value in policy_strings(policy, "component_roles.kinds")}
    reference_collections = mapping_value(get_path(policy, "component_roles.architecture_reference_collections"))
    covered_architecture: dict[str, set[str]] = {str(value): set() for value in reference_collections.values()}
    power_domains = architecture_collection_map(spec, "power_domains")
    external_connectors = architecture_collection_map(spec, "external_connectors")

    def owned_blocks(collection: str, item: dict[str, Any]) -> set[str]:
        if collection == "external_connectors":
            return {str(item.get("block_id", ""))} - {""}
        if collection == "test_and_debug":
            return set(string_list(item.get("target_blocks")))
        if collection == "power_domains":
            return {str(item.get("source_block", ""))} - {""}
        if collection == "protection_intents":
            boundary_id = str(item.get("boundary_id", ""))
            if boundary_id in power_domains:
                return {str(power_domains[boundary_id].get("source_block", ""))} - {""}
            if boundary_id in external_connectors:
                return {str(external_connectors[boundary_id].get("block_id", ""))} - {""}
        return set()
    for index, role in enumerate(list_value(sourcing.get("roles"))):
        prefix = f"sourcing.roles[{index}]"
        if not isinstance(role, dict):
            result.issue(f"{prefix} must be a mapping")
            continue
        for field in policy_strings(policy, "component_roles.required_fields"):
            if field not in role or role.get(field) is None:
                result.issue(f"{prefix}.{field} must be declared")
        role_id = str(role.get("id", "")).strip()
        if not role_id or has_unknown_token(role_id, policy):
            result.issue(f"{prefix}.id must be a non-empty string")
            continue
        if role_id in roles:
            result.issue(f"duplicate sourcing role id: {role_id}")
        roles[role_id] = role
        if normalized(role.get("kind")) not in allowed_kinds:
            result.issue(f"{prefix}.kind is unsupported: {role.get('kind')}")
        if role.get("required") is not True:
            result.issue(f"{prefix}.required must be true; deferred roles belong in architecture open decisions")
        refs = string_list(role.get("component_refs"))
        if not refs:
            result.issue(f"{prefix}.component_refs must not be empty")
        for ref in refs:
            if ref not in components:
                result.issue(f"{prefix}.component_refs references unknown component: {ref}")
        for block_id in string_list(role.get("block_ids")):
            if block_id not in blocks:
                result.issue(f"{prefix}.block_ids references unknown architecture block: {block_id}")
        for constraint_id in string_list(role.get("constraint_ids")):
            if constraint_id not in constraints:
                result.issue(f"{prefix}.constraint_ids references unknown architecture constraint: {constraint_id}")
        for criterion_index, criterion in enumerate(list_value(role.get("criteria"))):
            validate_criterion(criterion, f"{prefix}.criteria[{criterion_index}]", policy, result)
        architecture_refs = mapping_value(role.get("architecture_refs"))
        for ref_field, collection_value in reference_collections.items():
            collection = str(collection_value)
            known = architecture_collection_map(spec, collection)
            for item_id in string_list(architecture_refs.get(ref_field)):
                if item_id not in known:
                    result.issue(f"{prefix}.architecture_refs.{ref_field} references unknown {collection} id {item_id}")
                else:
                    covered_architecture.setdefault(collection, set()).add(item_id)
                    owners = owned_blocks(collection, known[item_id])
                    if owners and not owners.intersection(string_list(role.get("block_ids"))):
                        result.issue(
                            f"{prefix}.architecture_refs.{ref_field} does not belong to any role block: {item_id}"
                        )
    if not roles:
        result.issue("sourcing.roles must decompose architecture functions into component-level roles")

    for requirement_id, requirement in requirements.items():
        role_ids = string_list(requirement.get("role_ids"))
        if not role_ids:
            result.issue(f"sourcing requirement {requirement_id}.role_ids must not be empty")
        role_refs: set[str] = set()
        for role_id in role_ids:
            role = roles.get(role_id)
            if role is None:
                result.issue(f"sourcing requirement {requirement_id} references unknown role {role_id}")
                continue
            if role_id in role_owner:
                result.issue(f"sourcing role {role_id} is assigned to multiple requirements")
            role_owner[role_id] = requirement_id
            role_refs.update(string_list(role.get("component_refs")))
            if not set(string_list(role.get("block_ids"))).issubset(
                set(string_list(requirement.get("block_ids")))
            ):
                result.issue(f"sourcing role {role_id} block_ids are outside owning requirement {requirement_id}")
            if not set(string_list(role.get("constraint_ids"))).issubset(
                set(string_list(requirement.get("constraint_ids")))
            ):
                result.issue(f"sourcing role {role_id} constraint_ids are outside owning requirement {requirement_id}")
        if role_refs != set(string_list(requirement.get("component_refs"))):
            result.issue(f"sourcing requirement {requirement_id} role component_refs do not match requirement component_refs")
    for role_id in sorted(set(roles) - set(role_owner)):
        result.issue(f"sourcing role has no owning requirement: {role_id}")

    dependencies = {role_id: string_list(role.get("dependency_role_ids")) for role_id, role in roles.items()}
    for role_id, dependency_ids in dependencies.items():
        for dependency_id in dependency_ids:
            if dependency_id not in roles:
                result.issue(f"sourcing role {role_id} references unknown dependency role {dependency_id}")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(role_id: str) -> None:
        if role_id in visiting:
            result.issue(f"sourcing role dependency cycle includes {role_id}")
            return
        if role_id in visited:
            return
        visiting.add(role_id)
        for dependency_id in dependencies.get(role_id, []):
            if dependency_id in roles:
                visit(dependency_id)
        visiting.remove(role_id)
        visited.add(role_id)

    for role_id in roles:
        visit(role_id)

    for collection in policy_strings(policy, "component_roles.required_architecture_collections"):
        known_ids = set(architecture_collection_map(spec, collection))
        for item_id in sorted(known_ids - covered_architecture.get(collection, set())):
            result.issue(f"architecture {collection} item has no component-role coverage: {item_id}")
    return roles, {"role_count": len(roles), "owned_roles": len(role_owner)}


def check_compatibility_schema(
    spec: dict[str, Any],
    sourcing: dict[str, Any],
    requirements: dict[str, dict[str, Any]],
    policy: dict[str, Any],
    result: CheckResult,
) -> dict[str, Any]:
    constraints = list_value(sourcing.get("compatibility_constraints"))
    reference_collections = mapping_value(
        get_path(policy, "selected_set_compatibility.architecture_reference_collections")
    )
    covered: dict[str, set[str]] = {str(value): set() for value in reference_collections.values()}
    identifiers: set[str] = set()
    blocks = architecture_collection_map(spec, "blocks")
    required_scopes = {
        normalized(value) for value in policy_strings(policy, "architecture_requirements.required_block_scopes")
    }

    def requirement_blocks(requirement_id: str) -> set[str]:
        return set(string_list(mapping_value(requirements.get(requirement_id)).get("block_ids")))

    def check_endpoints(
        prefix: str,
        collection: str,
        item_id: str,
        item: dict[str, Any],
        selected_blocks: set[str],
    ) -> None:
        if collection in {"block_edges", "interfaces"}:
            endpoints = {str(item.get("from", "")), str(item.get("to", ""))} - {""}
            required_endpoints = {
                endpoint
                for endpoint in endpoints
                if endpoint in blocks and normalized(blocks[endpoint].get("scope")) in required_scopes
            }
            missing = required_endpoints - selected_blocks
            if missing:
                result.issue(
                    f"{prefix}.architecture_refs does not cover {collection} {item_id} internal endpoint blocks: "
                    + ", ".join(sorted(missing))
                )
        elif collection == "power_domains":
            source = str(item.get("source_block", ""))
            consumers = set(string_list(item.get("consumer_blocks")))
            if source and source not in selected_blocks:
                result.issue(f"{prefix}.architecture_refs does not cover power domain {item_id} source block {source}")
            if consumers and not consumers.intersection(selected_blocks):
                result.issue(f"{prefix}.architecture_refs does not cover any consumer of power domain {item_id}")
    for index, constraint in enumerate(constraints):
        prefix = f"sourcing.compatibility_constraints[{index}]"
        if not isinstance(constraint, dict):
            result.issue(f"{prefix} must be a mapping")
            continue
        for field in policy_strings(policy, "selected_set_compatibility.required_fields"):
            if field not in constraint or constraint.get(field) is None:
                result.issue(f"{prefix}.{field} must be declared")
        identifier = str(constraint.get("id", "")).strip()
        if identifier in identifiers:
            result.issue(f"{prefix}.id is duplicated: {identifier}")
        identifiers.add(identifier)
        left_id = str(constraint.get("left_requirement_id", "")).strip()
        right_id = str(constraint.get("right_requirement_id", "")).strip()
        if left_id not in requirements:
            result.issue(f"{prefix}.left_requirement_id references unknown requirement {left_id}")
        if right_id and right_id not in requirements:
            result.issue(f"{prefix}.right_requirement_id references unknown requirement {right_id}")
        if bool(right_id) == ("value" in constraint):
            result.issue(f"{prefix} must declare exactly one of right_requirement_id or value")
        if right_id and not string_value(constraint.get("right_attribute")):
            result.issue(f"{prefix}.right_attribute is required for cross-candidate compatibility")
        validate_criterion(
            {
                "attribute": constraint.get("left_attribute"),
                "operator": constraint.get("operator"),
                **({"value": constraint.get("value")} if "value" in constraint else {"value": 0}),
                **({"unit": constraint.get("unit")} if "unit" in constraint else {}),
            },
            prefix,
            policy,
            result,
        )
        architecture_refs = mapping_value(constraint.get("architecture_refs"))
        selected_blocks = requirement_blocks(left_id) | requirement_blocks(right_id)
        for ref_field, collection_value in reference_collections.items():
            collection = str(collection_value)
            known = architecture_collection_map(spec, collection)
            for item_id in string_list(architecture_refs.get(ref_field)):
                if item_id not in known:
                    result.issue(f"{prefix}.architecture_refs.{ref_field} references unknown {collection} id {item_id}")
                else:
                    covered.setdefault(collection, set()).add(item_id)
                    check_endpoints(prefix, collection, item_id, known[item_id], selected_blocks)
    if not constraints:
        result.issue("sourcing.compatibility_constraints must validate the selected candidate set")

    edge_kinds = {
        normalized(value) for value in policy_strings(policy, "selected_set_compatibility.covered_block_edge_kinds")
    }
    required_edges = {
        item_id
        for item_id, item in architecture_collection_map(spec, "block_edges").items()
        if normalized(item.get("kind")) in edge_kinds
    }
    for item_id in sorted(required_edges - covered.get("block_edges", set())):
        result.issue(f"architecture block edge has no selected-set compatibility coverage: {item_id}")
    for collection in policy_strings(policy, "selected_set_compatibility.required_architecture_collections"):
        known_ids = set(architecture_collection_map(spec, collection))
        for item_id in sorted(known_ids - covered.get(collection, set())):
            result.issue(f"architecture {collection} item has no selected-set compatibility coverage: {item_id}")
    return {"constraint_count": len(constraints)}


def component_exempt_from_sourcing(component: dict[str, Any], policy: dict[str, Any]) -> bool:
    if any(get_path(component, field) is True for field in policy_strings(policy, "component_sourcing_exemptions.boolean_fields")):
        return True
    status = normalized(component.get("status") or get_path(component, "assembly.status"))
    if status in {normalized(value) for value in policy_strings(policy, "component_sourcing_exemptions.status_values")}:
        return True
    ref = str(component.get("ref", ""))
    if any(ref.startswith(prefix) for prefix in policy_strings(policy, "component_sourcing_exemptions.ref_prefixes")):
        return True
    footprint = str(component.get("footprint", ""))
    return any(token in footprint for token in policy_strings(policy, "component_sourcing_exemptions.footprint_tokens"))


def context_number(
    context: dict[str, Any],
    field: str,
    minimum: float,
    maximum: float,
    result: CheckResult,
    integer: bool = False,
) -> float | int | None:
    value = integer_value(context.get(field)) if integer else number_value(context.get(field))
    if value is None:
        kind = "integer" if integer else "number"
        result.issue(f"sourcing.context.{field} must be a {kind}")
        return None
    if value < minimum or value > maximum:
        result.issue(f"sourcing.context.{field} must be between {minimum} and {maximum}")
    return value


def check_sourcing_context(
    spec: dict[str, Any],
    result: CheckResult,
    policy: dict[str, Any] | None = None,
    force: bool = False,
    spec_path: Path | None = None,
) -> dict[str, Any]:
    policy = policy or load_policy(spec)
    enabled = sourcing_required(spec, policy, force=force)
    details: dict[str, Any] = {"enabled": enabled}
    if not enabled:
        result.warning("sourcing context gate not required for this spec stage")
        return details

    stage = normalized(get_path(spec, "project.stage"))
    production_stages = {normalized(value) for value in policy_strings(policy, "production_stage_values")}
    configured_policy = get_path(spec, "validation.part_selection.sourcing_policy_file")
    if stage in production_stages and string_value(configured_policy):
        result.issue("production sourcing policy override is forbidden; use the bundled trusted policy")
    root = project_root(spec, spec_path, policy, result)

    sourcing = mapping_value(spec.get("sourcing"))
    if not sourcing:
        result.issue("sourcing must be declared before production part selection")
        return details
    schema_version = integer_value(sourcing.get("schema_version"))
    supported = {integer_value(value) for value in list_value(policy.get("schema_versions"))}
    if schema_version not in supported:
        result.issue(f"sourcing.schema_version must be one of {sorted(value for value in supported if value is not None)}")

    context = mapping_value(sourcing.get("context"))
    if not context:
        result.issue("sourcing.context must be a mapping")
    for field in ("region", "currency"):
        if not string_value(context.get(field)) or has_unknown_token(context.get(field), policy):
            result.issue(f"sourcing.context.{field} must be a non-empty string")

    limits = mapping_value(policy.get("context_limits"))
    board_quantity = context_number(
        context,
        "board_quantity",
        float(limits.get("minimum_board_quantity", 1)),
        float(limits.get("maximum_board_quantity", 1000000)),
        result,
        integer=True,
    )
    attrition_rate = context_number(
        context,
        "attrition_rate",
        float(limits.get("minimum_attrition_rate", 0)),
        float(limits.get("maximum_attrition_rate", 0.5)),
        result,
    )
    max_candidates = context_number(
        context,
        "max_candidates_per_requirement",
        float(limits.get("minimum_candidates_per_requirement", 1)),
        float(limits.get("maximum_candidates_per_requirement", 12)),
        result,
        integer=True,
    )
    max_rounds = context_number(
        context,
        "max_search_rounds",
        float(limits.get("minimum_search_rounds", 1)),
        float(limits.get("maximum_search_rounds", 8)),
        result,
        integer=True,
    )
    stock_age = context_number(
        context,
        "stock_max_age_hours",
        float(limits.get("minimum_stock_max_age_hours", 1)),
        float(limits.get("maximum_stock_max_age_hours", 720)),
        result,
    )
    lock_age = context_number(
        context,
        "lock_max_age_hours",
        float(limits.get("minimum_lock_max_age_hours", 1)),
        float(limits.get("maximum_lock_max_age_hours", 720)),
        result,
    )
    budget_details = check_cost_budget(
        spec,
        context,
        policy,
        result,
        production=stage in production_stages,
    )

    providers: dict[str, dict[str, Any]] = {}
    allowed_provider_kinds = {normalized(value) for value in policy_strings(policy, "provider_kinds")}
    trusted_profiles = mapping_value(policy.get("trusted_provider_profiles"))
    for index, provider in enumerate(list_value(context.get("providers"))):
        label = f"sourcing.context.providers[{index}]"
        if not isinstance(provider, dict):
            result.issue(f"{label} must be a mapping")
            continue
        provider_id = str(provider.get("id", "")).strip()
        if not provider_id or has_unknown_token(provider_id, policy):
            result.issue(f"{label}.id must be a non-empty string")
            continue
        if provider_id in providers:
            result.issue(f"duplicate sourcing provider id: {provider_id}")
        providers[provider_id] = provider
        if normalized(provider.get("kind")) not in allowed_provider_kinds:
            result.issue(f"{label}.kind is unsupported: {provider.get('kind')}")
        trust_profile = str(provider.get("trust_profile", "")).strip()
        trusted = mapping_value(trusted_profiles.get(trust_profile))
        if stage in production_stages:
            if not trusted:
                result.issue(f"{label}.trust_profile must reference a bundled trusted provider profile")
            elif normalized(provider.get("kind")) != normalized(trusted.get("kind")):
                result.issue(f"{label}.kind does not match trusted provider profile {trust_profile}")
        domains = string_list(provider.get("domains"))
        if not domains:
            result.issue(f"{label}.domains must contain at least one domain")
        for domain in domains:
            if has_unknown_token(domain, policy) or urlparse(f"//{domain}").hostname != domain.lower().strip("."):
                result.issue(f"{label}.domains contains an invalid domain: {domain}")
        if trusted:
            trusted_domains = {normalized(value) for value in string_list(trusted.get("domains"))}
            untrusted_domains = {normalized(value) for value in domains} - trusted_domains
            if untrusted_domains:
                result.issue(
                    f"{label}.domains contains values outside trusted profile {trust_profile}: "
                    + ", ".join(sorted(untrusted_domains))
                )
        part_field = provider.get("component_part_field")
        allowed_fields = set(policy_strings(policy, "allowed_component_part_fields"))
        if string_value(part_field) and str(part_field) not in allowed_fields:
            result.issue(f"{label}.component_part_field is unsupported: {part_field}")
    if not providers:
        result.issue("sourcing.context.providers must contain at least one provider")

    manufacturing_assembly = get_path(spec, "manufacturing.jlcpcb.assembly.enabled") is True
    assembly = mapping_value(context.get("assembly"))
    if bool(assembly.get("enabled")) != manufacturing_assembly:
        result.issue("sourcing.context.assembly.enabled must match manufacturing assembly intent")
    if assembly.get("enabled") is True:
        provider_id = str(assembly.get("provider_id", "")).strip()
        if provider_id not in providers:
            result.issue("sourcing.context.assembly.provider_id must reference a declared provider")
        elif normalized(providers[provider_id].get("kind")) != "assembler":
            result.issue("sourcing.context.assembly.provider_id must reference an assembler provider")
        if assembly.get("require_availability") is not True:
            result.issue("sourcing.context.assembly.require_availability must be true for PCBA sourcing")

    blocks, constraints = architecture_maps(spec, policy)
    required_scopes = {
        normalized(value) for value in policy_strings(policy, "architecture_requirements.required_block_scopes")
    }
    exempt_categories = {
        normalized(value) for value in policy_strings(policy, "architecture_requirements.exempt_block_categories")
    }
    required_blocks = {
        block_id
        for block_id, block in blocks.items()
        if block.get("required") is True
        and normalized(block.get("scope")) in required_scopes
        and normalized(block.get("category")) not in exempt_categories
    }
    required_constraints = {
        constraint_id
        for constraint_id, constraint in constraints.items()
        if constraint.get("required") is True and constraint.get("block_id") in required_blocks
    }
    criteria_field = str(get_path(policy, "machine_constraint.criteria_field") or "criteria")
    for constraint_id in sorted(required_constraints):
        criteria = constraints[constraint_id].get(criteria_field)
        if not isinstance(criteria, list) or not criteria:
            result.issue(f"architecture constraint {constraint_id}.{criteria_field} must contain machine-readable criteria")
            continue
        for index, criterion in enumerate(criteria):
            validate_criterion(criterion, f"architecture constraint {constraint_id}.{criteria_field}[{index}]", policy, result)

    requirements: dict[str, dict[str, Any]] = {}
    component_owner: dict[str, str] = {}
    covered_blocks: set[str] = set()
    covered_constraints: set[str] = set()
    allowed_dispositions = {normalized(value) for value in policy_strings(policy, "assembly_dispositions")}
    components = {
        str(item.get("ref")).strip(): item
        for item in list_value(spec.get("components"))
        if isinstance(item, dict) and string_value(item.get("ref"))
    }
    for index, requirement in enumerate(list_value(sourcing.get("requirements"))):
        label = f"sourcing.requirements[{index}]"
        if not isinstance(requirement, dict):
            result.issue(f"{label} must be a mapping")
            continue
        requirement_id = str(requirement.get("id", "")).strip()
        if not requirement_id or has_unknown_token(requirement_id, policy):
            result.issue(f"{label}.id must be a non-empty string")
            continue
        if requirement_id in requirements:
            result.issue(f"duplicate sourcing requirement id: {requirement_id}")
        requirements[requirement_id] = requirement
        if requirement.get("required") is not True:
            result.issue(f"{label}.required must be true; optional parts use an explicit nonblocking disposition")
        disposition = normalized(requirement.get("disposition"))
        if disposition not in allowed_dispositions:
            result.issue(f"{label}.disposition is unsupported: {requirement.get('disposition')}")
        refs = string_list(requirement.get("component_refs"))
        if not refs:
            result.issue(f"{label}.component_refs must not be empty")
        if len(set(refs)) != len(refs):
            result.issue(f"{label}.component_refs must not contain duplicates")
        quantity = integer_value(requirement.get("quantity_per_board"))
        if quantity is None or quantity <= 0:
            result.issue(f"{label}.quantity_per_board must be a positive integer")
        elif quantity != len(refs):
            result.issue(f"{label}.quantity_per_board must equal the number of component_refs")
        for ref in refs:
            if ref not in components:
                result.issue(f"{label}.component_refs references unknown component: {ref}")
            if ref in component_owner:
                result.issue(f"component {ref} is assigned to multiple sourcing requirements")
            component_owner[ref] = requirement_id
        block_ids = string_list(requirement.get("block_ids"))
        constraint_ids = string_list(requirement.get("constraint_ids"))
        if not block_ids:
            result.issue(f"{label}.block_ids must not be empty")
        for block_id in block_ids:
            if block_id not in blocks:
                result.issue(f"{label}.block_ids references unknown architecture block: {block_id}")
            else:
                covered_blocks.add(block_id)
        for constraint_id in constraint_ids:
            constraint = constraints.get(constraint_id)
            if constraint is None:
                result.issue(f"{label}.constraint_ids references unknown architecture constraint: {constraint_id}")
            elif constraint.get("block_id") not in block_ids:
                result.issue(f"{label} constraint {constraint_id} belongs to a block not listed in block_ids")
            else:
                covered_constraints.add(constraint_id)
        extra_criteria = requirement.get("criteria", [])
        if extra_criteria is not None and not isinstance(extra_criteria, list):
            result.issue(f"{label}.criteria must be a list")
        for criterion_index, criterion in enumerate(list_value(extra_criteria)):
            validate_criterion(criterion, f"{label}.criteria[{criterion_index}]", policy, result)
    if not requirements:
        result.issue("sourcing.requirements must contain at least one requirement")
    for block_id in sorted(required_blocks - covered_blocks):
        result.issue(f"required architecture block has no sourcing requirement: {block_id}")
    for constraint_id in sorted(required_constraints - covered_constraints):
        result.issue(f"required architecture constraint has no sourcing requirement: {constraint_id}")
    for ref, component in components.items():
        if not component_exempt_from_sourcing(component, policy) and ref not in component_owner:
            result.issue(f"procured component has no sourcing requirement and part-lock coverage: {ref}")

    roles, role_details = check_component_roles(
        spec,
        sourcing,
        requirements,
        components,
        blocks,
        constraints,
        policy,
        result,
    )
    compatibility_details = check_compatibility_schema(
        spec,
        sourcing,
        requirements,
        policy,
        result,
    )

    for field_key in ("candidate_path_field", "ranking_path_field", "part_lock_path_field"):
        configured_artifact_path(spec, policy, field_key, result, root=root)

    details.update(
        {
            "schema_version": schema_version,
            "context_sha256": canonical_sha256(context),
            "board_quantity": board_quantity,
            "attrition_rate": attrition_rate,
            "max_candidates_per_requirement": max_candidates,
            "max_search_rounds": max_rounds,
            "stock_max_age_hours": stock_age,
            "lock_max_age_hours": lock_age,
            "provider_count": len(providers),
            "requirement_count": len(requirements),
            "required_blocks": len(required_blocks),
            "required_constraints": len(required_constraints),
            "cost_budget": budget_details,
            "component_roles": role_details,
            "selected_set_compatibility": compatibility_details,
            "project_root": str(root),
        }
    )
    return details


def host_allowed(url: Any, provider: dict[str, Any]) -> bool:
    if not string_value(url):
        return False
    host = (urlparse(str(url)).hostname or "").lower().strip(".")
    return any(host == domain.lower().strip(".") or host.endswith("." + domain.lower().strip(".")) for domain in string_list(provider.get("domains")))


def forbidden_identity(value: Any, policy: dict[str, Any]) -> bool:
    if not string_value(value):
        return True
    return any(re.search(pattern, str(value)) for pattern in policy_strings(policy, "evidence.forbidden_identity_patterns"))


def forbidden_url(url: Any, policy: dict[str, Any]) -> bool:
    if not string_value(url):
        return True
    parsed = urlparse(str(url))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return True
    host = parsed.hostname.lower().strip(".")
    forbidden = {value.lower().strip(".") for value in policy_strings(policy, "evidence.forbidden_url_hosts")}
    return any(host == item or host.endswith("." + item) for item in forbidden)


def required_order_quantity(board_quantity: int, per_board: int, attrition_rate: float, moq: int, multiple: int) -> int:
    needed_decimal = Decimal(board_quantity) * Decimal(per_board) * (Decimal(1) + Decimal(str(attrition_rate)))
    needed = int(needed_decimal.to_integral_value(rounding=ROUND_CEILING))
    orderable = max(needed, moq)
    return int(math.ceil(orderable / multiple) * multiple)


def needed_quantity(board_quantity: int, per_board: int, attrition_rate: float) -> int:
    value = Decimal(board_quantity) * Decimal(per_board) * (Decimal(1) + Decimal(str(attrition_rate)))
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def price_at_quantity(pricing: dict[str, Any], quantity: int, failures: list[str]) -> Decimal:
    base = decimal_value(pricing.get("unit_price"))
    if base is None or base < 0:
        failures.append("pricing.unit_price must be a non-negative number")
        base = Decimal(0)
    selected = base
    previous_quantity = 0
    for index, tier in enumerate(list_value(pricing.get("price_breaks"))):
        if not isinstance(tier, dict):
            failures.append(f"pricing.price_breaks[{index}] must be a mapping")
            continue
        minimum_quantity = integer_value(tier.get("minimum_quantity"))
        unit_price = decimal_value(tier.get("unit_price"))
        if minimum_quantity is None or minimum_quantity <= previous_quantity:
            failures.append("pricing.price_breaks minimum_quantity values must be positive and strictly increasing")
            continue
        previous_quantity = minimum_quantity
        if unit_price is None or unit_price < 0:
            failures.append(f"pricing.price_breaks[{index}].unit_price must be non-negative")
            continue
        if minimum_quantity <= quantity:
            selected = unit_price
    return selected


def normalized_numeric(value: Any, unit: Any, policy: dict[str, Any]) -> tuple[Decimal, str] | None:
    number = decimal_value(value)
    if number is None:
        return None
    if not string_value(unit):
        return number, ""
    conversion = mapping_value(get_path(policy, f"unit_conversions.{unit}"))
    if not conversion:
        return number, str(unit)
    factor = decimal_value(conversion.get("factor"))
    dimension = str(conversion.get("dimension", unit))
    if factor is None:
        return None
    return number * factor, dimension


def evaluate_criterion(
    criterion: dict[str, Any], capability: Any, policy: dict[str, Any]
) -> tuple[bool, str]:
    attribute = str(criterion.get("attribute", "")).strip()
    operator = normalized(criterion.get("operator"))
    if capability is None:
        return False, f"missing capability {attribute}"
    capability_map = capability if isinstance(capability, dict) else {"value": capability}
    if operator == "exists":
        return True, ""
    expected = criterion.get("value")
    actual = capability_map.get("values") if "values" in capability_map else capability_map.get("value")
    if operator in {"range-contains", "ranges-overlap"}:
        expected_range = mapping_value(expected)
        actual_range = mapping_value(capability_map)
        expected_min = normalized_numeric(expected_range.get("min"), criterion.get("unit"), policy)
        expected_max = normalized_numeric(expected_range.get("max"), criterion.get("unit"), policy)
        actual_min = normalized_numeric(actual_range.get("min"), capability_map.get("unit"), policy)
        actual_max = normalized_numeric(actual_range.get("max"), capability_map.get("unit"), policy)
        values = (expected_min, expected_max, actual_min, actual_max)
        if any(value is None for value in values):
            return False, f"capability {attribute} has an incomplete numeric range"
        assert expected_min is not None and expected_max is not None and actual_min is not None and actual_max is not None
        if len({expected_min[1], expected_max[1], actual_min[1], actual_max[1]}) != 1:
            return False, f"capability {attribute} has incompatible range units"
        if expected_min[0] > expected_max[0] or actual_min[0] > actual_max[0]:
            return False, f"capability {attribute} has an inverted numeric range"
        passed = (
            actual_min[0] <= expected_min[0] and actual_max[0] >= expected_max[0]
            if operator == "range-contains"
            else actual_max[0] >= expected_min[0] and expected_max[0] >= actual_min[0]
        )
        return (True, "") if passed else (False, f"capability {attribute} does not satisfy {operator}")

    numeric_operators = {"gt", "gte", "lt", "lte"}
    if operator in numeric_operators or (
        operator in {"eq", "ne"} and number_value(expected) is not None and number_value(actual) is not None
    ):
        expected_number = normalized_numeric(expected, criterion.get("unit"), policy)
        actual_number = normalized_numeric(actual, capability_map.get("unit"), policy)
        if expected_number is None or actual_number is None or expected_number[1] != actual_number[1]:
            return False, f"capability {attribute} has incompatible numeric value or unit"
        actual_multiplier = decimal_value(criterion.get("actual_multiplier")) or Decimal(1)
        required_multiplier = decimal_value(criterion.get("required_multiplier")) or Decimal(1)
        left, right = actual_number[0] * actual_multiplier, expected_number[0] * required_multiplier
        comparisons = {
            "gt": left > right,
            "gte": left >= right,
            "lt": left < right,
            "lte": left <= right,
            "eq": left == right,
            "ne": left != right,
        }
        passed = comparisons[operator]
    elif operator == "eq":
        passed = actual == expected
    elif operator == "ne":
        passed = actual != expected
    elif operator == "in":
        passed = isinstance(expected, list) and actual in expected
    elif operator == "contains":
        passed = isinstance(actual, list) and expected in actual
    elif operator == "contains-all":
        passed = isinstance(actual, list) and isinstance(expected, list) and set(expected).issubset(set(actual))
    elif operator == "intersects":
        passed = isinstance(actual, list) and isinstance(expected, list) and bool(set(actual) & set(expected))
    else:
        return False, f"unsupported criterion operator {operator}"
    return (True, "") if passed else (False, f"capability {attribute} does not satisfy {operator} {expected!r}")


def evaluate_candidate_criterion(
    criterion: dict[str, Any], capabilities: dict[str, Any], policy: dict[str, Any]
) -> tuple[bool, str]:
    condition_field = str(get_path(policy, "machine_constraint.condition_field") or "when")
    condition = criterion.get(condition_field)
    if isinstance(condition, dict):
        condition_passed, _ = evaluate_candidate_criterion(condition, capabilities, policy)
        if not condition_passed:
            return True, "condition not applicable"
    for field in policy_strings(policy, "machine_constraint.compound_fields"):
        if field not in criterion:
            continue
        outcomes = [
            evaluate_candidate_criterion(child, capabilities, policy)
            for child in list_value(criterion.get(field))
            if isinstance(child, dict)
        ]
        if not outcomes:
            return False, f"compound criterion {field} has no children"
        passed = all(item[0] for item in outcomes) if field == "all_of" else any(item[0] for item in outcomes)
        reasons = "; ".join(item[1] for item in outcomes if not item[0])
        return (True, "") if passed else (False, f"compound criterion {field} failed: {reasons}")
    attribute = str(criterion.get("attribute", "")).strip()
    return evaluate_criterion(criterion, capabilities.get(attribute), policy)


def criterion_leaves(criterion: dict[str, Any], policy: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    condition_field = str(get_path(policy, "machine_constraint.condition_field") or "when")
    if isinstance(criterion.get(condition_field), dict):
        output.extend(criterion_leaves(criterion[condition_field], policy))
    for field in policy_strings(policy, "machine_constraint.compound_fields"):
        if field in criterion:
            for child in list_value(criterion.get(field)):
                if isinstance(child, dict):
                    output.extend(criterion_leaves(child, policy))
            return output
    output.append(criterion)
    return output


def requirement_criteria(
    requirement: dict[str, Any],
    constraints: dict[str, dict[str, Any]],
    policy: dict[str, Any],
    roles: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    criteria_field = str(get_path(policy, "machine_constraint.criteria_field") or "criteria")
    output: list[dict[str, Any]] = []
    for constraint_id in string_list(requirement.get("constraint_ids")):
        constraint = constraints.get(constraint_id, {})
        for index, criterion in enumerate(list_value(constraint.get(criteria_field))):
            if isinstance(criterion, dict):
                output.append({"source": f"architecture:{constraint_id}:{index}", **criterion})
    for index, criterion in enumerate(list_value(requirement.get("criteria"))):
        if isinstance(criterion, dict):
            output.append({"source": f"requirement:{requirement.get('id')}:{index}", **criterion})
    for role_id in string_list(requirement.get("role_ids")):
        role = mapping_value((roles or {}).get(role_id))
        for index, criterion in enumerate(list_value(role.get("criteria"))):
            if isinstance(criterion, dict):
                output.append({"source": f"role:{role_id}:{index}", **criterion})
    return output


def evidence_index(
    candidate: dict[str, Any],
    providers: dict[str, dict[str, Any]],
    policy: dict[str, Any],
    now: datetime,
    stock_max_age_hours: float,
    artifacts_root: Path,
    fixture_mode: bool = False,
    root: Path | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    failures: list[str] = []
    evidence_by_id: dict[str, dict[str, Any]] = {}
    candidate_identity = {
        "manufacturer": candidate.get("manufacturer"),
        "mpn": candidate.get("mpn"),
        "supplier_part": get_path(candidate, "supplier.part_number"),
    }
    freshness_kinds = {normalized(value) for value in policy_strings(policy, "evidence.freshness_kinds")}
    supplier_kinds = {normalized(value) for value in policy_strings(policy, "evidence.supplier_kinds")}
    assertion_rules = normalized_mapping(get_path(policy, "evidence.identity_assertions"))
    evidence_file_owners: dict[Path, str] = {}
    evidence_digest_owners: dict[str, str] = {}
    required_capture_fields = policy_strings(policy, "evidence.required_capture_fields")
    allowed_capture_methods = {normalized(value) for value in policy_strings(policy, "evidence.capture_methods")}
    textual_media_types = set(policy_strings(policy, "evidence.textual_media_types"))
    fixture_media_types = set(policy_strings(policy, "evidence.fixture_media_types"))
    minimum_file_bytes = int(get_path(policy, "evidence.minimum_file_bytes") or 1)
    for index, evidence in enumerate(list_value(candidate.get("evidence"))):
        label = f"evidence[{index}]"
        if not isinstance(evidence, dict):
            failures.append(f"{label} must be a mapping")
            continue
        evidence_id = str(evidence.get("id", "")).strip()
        kind = normalized(evidence.get("kind"))
        if not evidence_id:
            failures.append(f"{label}.id is missing")
            continue
        if evidence_id in evidence_by_id:
            failures.append(f"duplicate evidence id {evidence_id}")
        evidence_by_id[evidence_id] = evidence
        url = evidence.get("url")
        if get_path(policy, "evidence.require_source_url") is True and forbidden_url(url, policy):
            failures.append(f"{evidence_id} has a missing, invalid, or forbidden source URL")
        if kind in supplier_kinds:
            provider_id = str(evidence.get("provider_id", "")).strip()
            provider = providers.get(provider_id)
            if provider is None:
                failures.append(f"{evidence_id} references unknown provider {provider_id}")
            elif not host_allowed(url, provider):
                failures.append(f"{evidence_id} URL host is outside provider {provider_id} domains")
        if kind in freshness_kinds:
            issue = timestamp_issue(evidence.get("captured_at"), now, stock_max_age_hours, f"{evidence_id}.captured_at")
            if issue:
                failures.append(issue)
        if get_path(policy, "evidence.require_capture_metadata") is True:
            for field in required_capture_fields:
                value = evidence.get(field)
                if field == "byte_size":
                    if integer_value(value) is None or int(value) < minimum_file_bytes:
                        failures.append(f"{evidence_id}.byte_size must be an integer of at least {minimum_file_bytes}")
                elif not string_value(value):
                    failures.append(f"{evidence_id}.{field} is required")
            if normalized(evidence.get("capture_method")) not in allowed_capture_methods:
                failures.append(f"{evidence_id}.capture_method is unsupported")
            if evidence.get("final_url") != url:
                failures.append(f"{evidence_id}.final_url must match the captured evidence URL")
            media_type = str(evidence.get("media_type", "")).strip().lower()
            production_types = {
                str(value).strip().lower()
                for value in list_value(get_path(policy, f"evidence.production_media_types.{kind}"))
            }
            allowed_media = fixture_media_types if fixture_mode else production_types
            if media_type not in allowed_media:
                profile = "fixture" if fixture_mode else "production"
                failures.append(f"{evidence_id}.media_type {media_type or '<missing>'} is not allowed for {profile} {kind}")
        file_value = evidence.get("file")
        if get_path(policy, "evidence.require_local_file") is True:
            if not string_value(file_value):
                failures.append(f"{evidence_id}.file is required")
            else:
                evidence_path = resolved_path(file_value, root)
                if get_path(policy, "evidence.require_distinct_raw_evidence_files") is True:
                    previous = evidence_file_owners.get(evidence_path)
                    if previous is not None:
                        failures.append(
                            f"{evidence_id} must use distinct raw evidence from {previous}; reused files cannot prove independent sources"
                        )
                    evidence_file_owners[evidence_path] = evidence_id
                try:
                    evidence_path.relative_to(artifacts_root)
                except ValueError:
                    failures.append(f"{evidence_id}.file must stay under project.artifacts_dir")
                if not evidence_path.is_file():
                    failures.append(f"{evidence_id}.file does not exist: {evidence_path}")
                else:
                    actual_size = evidence_path.stat().st_size
                    if evidence.get("byte_size") != actual_size:
                        failures.append(f"{evidence_id}.byte_size does not match {evidence_path}")
                    expected_sha = str(evidence.get("sha256", "")).lower()
                    if get_path(policy, "evidence.require_sha256") is True and not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
                        failures.append(f"{evidence_id}.sha256 must be a lowercase SHA256 digest")
                    elif expected_sha and sha256_file(evidence_path) != expected_sha:
                        failures.append(f"{evidence_id}.sha256 does not match {evidence_path}")
                    if expected_sha:
                        previous_digest = evidence_digest_owners.get(expected_sha)
                        if previous_digest is not None:
                            failures.append(
                                f"{evidence_id} must use distinct raw evidence from {previous_digest}; reused content cannot prove independent sources"
                            )
                        evidence_digest_owners[expected_sha] = evidence_id
                    media_type = str(evidence.get("media_type", "")).strip().lower()
                    evidence_text = ""
                    if media_type in textual_media_types:
                        try:
                            evidence_text = evidence_path.read_text(encoding="utf-8")
                        except UnicodeDecodeError:
                            failures.append(f"{evidence_id}.file is not valid UTF-8 textual evidence")
                    elif string_value(evidence.get("extracted_text_file")):
                        extracted_path = resolved_path(evidence.get("extracted_text_file"), root)
                        if not extracted_path.is_file():
                            failures.append(f"{evidence_id}.extracted_text_file does not exist")
                        elif sha256_file(extracted_path) != str(evidence.get("extracted_text_sha256", "")):
                            failures.append(f"{evidence_id}.extracted_text_sha256 does not match")
                        else:
                            evidence_text = extracted_path.read_text(encoding="utf-8")
                    elif not fixture_mode:
                        failures.append(f"{evidence_id} binary evidence requires a hashed extracted_text_file")
                    evidence["_verified_text"] = evidence_text
        assertions = mapping_value(evidence.get("assertions"))
        for field in string_list(assertion_rules.get(kind)):
            expected = candidate_identity.get(field)
            if assertions.get(field) != expected:
                failures.append(f"{evidence_id}.assertions.{field} does not match candidate identity")
        verified_text = str(evidence.get("_verified_text", "")).casefold()
        for field in string_list(assertion_rules.get(kind)):
            asserted = assertions.get(field)
            if string_value(asserted) and str(asserted).casefold() not in verified_text:
                failures.append(f"{evidence_id}.assertions.{field} is not present in captured content")
    return evidence_by_id, failures


def check_role_assertions(
    role: str,
    source: dict[str, Any],
    evidence: dict[str, Any] | None,
    policy: dict[str, Any],
    failures: list[str],
) -> None:
    if evidence is None:
        return
    asserted = mapping_value(get_path(evidence, f"assertions.{role}"))
    for field in policy_strings(policy, f"evidence.role_assertion_fields.{role}"):
        if asserted.get(field) != source.get(field):
            failures.append(f"{role} evidence assertion {field} does not match candidate data")
        value = asserted.get(field)
        verified_text = str(evidence.get("_verified_text", "")).casefold()
        for scalar in scalar_values(value):
            if str(scalar).casefold() not in verified_text:
                failures.append(f"{role} evidence assertion {field} is not present in captured content")


def scalar_values(value: Any) -> list[Any]:
    if isinstance(value, dict):
        output: list[Any] = []
        for item in value.values():
            output.extend(scalar_values(item))
        return output
    if isinstance(value, list):
        output = []
        for item in value:
            output.extend(scalar_values(item))
        return output
    return [] if value is None else [value]


def role_evidence(
    role: str,
    evidence_id: Any,
    evidence_by_id: dict[str, dict[str, Any]],
    policy: dict[str, Any],
    failures: list[str],
) -> dict[str, Any] | None:
    identifier = str(evidence_id or "").strip()
    evidence = evidence_by_id.get(identifier)
    if evidence is None:
        failures.append(f"{role}.evidence_id must reference candidate evidence")
        return None
    expected_kind = normalized(get_path(policy, f"candidate_manifest.evidence_kind_by_role.{role}"))
    if expected_kind and normalized(evidence.get("kind")) != expected_kind:
        failures.append(f"{role}.evidence_id must reference {expected_kind} evidence")
    return evidence


def evaluate_candidate(
    candidate: Any,
    requirement: dict[str, Any],
    constraints: dict[str, dict[str, Any]],
    providers: dict[str, dict[str, Any]],
    context: dict[str, Any],
    policy: dict[str, Any],
    now: datetime,
    artifacts_root: Path,
    roles: dict[str, dict[str, Any]] | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    failures: list[str] = []
    if not isinstance(candidate, dict):
        return {"candidate_id": "<invalid>", "qualified": False, "reasons": ["candidate must be a mapping"], "score": 0.0}
    candidate_id = str(candidate.get("id", "")).strip()
    if not candidate_id:
        failures.append("candidate.id is missing")
        candidate_id = "<missing>"
    for field in policy_strings(policy, "candidate_manifest.required_identity_fields"):
        if forbidden_identity(candidate.get(field), policy):
            failures.append(f"{field} is missing or contains a forbidden placeholder")
    supplier = mapping_value(candidate.get("supplier"))
    provider_id = str(supplier.get("provider_id", "")).strip()
    provider = providers.get(provider_id)
    supplier_part = supplier.get("part_number")
    if provider is None:
        failures.append(f"supplier.provider_id references unknown provider {provider_id}")
    elif normalized(provider.get("kind")) not in {
        normalized(value) for value in policy_strings(policy, "candidate_supplier_provider_kinds")
    }:
        failures.append(f"supplier provider {provider_id} has unsupported kind {provider.get('kind')}")
    if forbidden_identity(supplier_part, policy):
        failures.append("supplier.part_number is missing or contains a forbidden placeholder")
    if provider is not None and not host_allowed(supplier.get("source_url"), provider):
        failures.append("supplier.source_url is outside the declared provider domains")

    package = mapping_value(candidate.get("package"))
    for field in policy_strings(policy, "candidate_manifest.required_package_fields"):
        if not string_value(package.get(field)):
            failures.append(f"package.{field} is missing")
    for field in policy_strings(policy, "candidate_manifest.required_package_numeric_fields"):
        value = package.get(field)
        if field == "pin_count":
            if integer_value(value) is None or int(value) <= 0:
                failures.append(f"package.{field} must be a positive integer")
        elif number_value(value) is None or float(value) <= 0:
            failures.append(f"package.{field} must be a positive number")
    confidence_scores = normalized_mapping(get_path(policy, "candidate_manifest.package_confidence_scores"))
    package_confidence = normalized(package.get("confidence"))
    if package_confidence not in {normalized(key) for key in confidence_scores}:
        failures.append(f"package.confidence is unsupported: {package.get('confidence')}")

    stock_max_age = float(context.get("stock_max_age_hours", 0))
    fixture_mode = (
        os.environ.get("KICAD_PCB_TEST_MODE") == "1"
        and normalized(context.get("evidence_profile")) == normalized(get_path(policy, "evidence.fixture_profile"))
    )
    evidence_by_id, evidence_failures = evidence_index(
        candidate,
        providers,
        policy,
        now,
        stock_max_age,
        artifacts_root,
        fixture_mode=fixture_mode,
        root=root,
    )
    failures.extend(evidence_failures)
    package_evidence_ids = string_list(package.get("evidence_ids"))
    if get_path(policy, "candidate_manifest.require_package_evidence_ids") is True and not package_evidence_ids:
        failures.append("package.evidence_ids must not be empty")
    for evidence_id in package_evidence_ids:
        if evidence_id not in evidence_by_id:
            failures.append(f"package.evidence_ids references unknown evidence {evidence_id}")
            continue
        evidence = evidence_by_id[evidence_id]
        asserted_package = mapping_value(get_path(evidence, "assertions.package"))
        for field in policy_strings(policy, "evidence.package_assertion_fields"):
            if asserted_package.get(field) != package.get(field):
                failures.append(f"package evidence {evidence_id} assertion {field} does not match candidate package")
            for scalar in scalar_values(asserted_package.get(field)):
                if str(scalar).casefold() not in str(evidence.get("_verified_text", "")).casefold():
                    failures.append(f"package evidence {evidence_id} assertion {field} is not present in captured content")
    evidence_kinds = {normalized(item.get("kind")) for item in evidence_by_id.values()}
    required_evidence = {normalized(value) for value in policy_strings(policy, "evidence.required_kinds")}
    disposition = normalized(requirement.get("disposition"))
    if disposition == "pcba":
        required_evidence.update(normalized(value) for value in policy_strings(policy, "evidence.pcba_required_kinds"))
    for missing_kind in sorted(required_evidence - evidence_kinds):
        failures.append(f"missing required evidence kind {missing_kind}")

    inventory = mapping_value(candidate.get("inventory"))
    in_stock = integer_value(inventory.get("in_stock"))
    moq = integer_value(inventory.get("moq"))
    order_multiple = integer_value(inventory.get("order_multiple"))
    if in_stock is None or in_stock < 0:
        failures.append("inventory.in_stock must be a non-negative integer")
    if moq is None or moq <= 0:
        failures.append("inventory.moq must be a positive integer")
    if order_multiple is None or order_multiple <= 0:
        failures.append("inventory.order_multiple must be a positive integer")
    inventory_evidence_id = str(inventory.get("evidence_id", "")).strip()
    inventory_evidence = role_evidence("inventory", inventory_evidence_id, evidence_by_id, policy, failures)
    check_role_assertions("inventory", inventory, inventory_evidence, policy, failures)
    if inventory_evidence is not None:
        if inventory_evidence.get("url") != supplier.get("source_url"):
            failures.append("inventory evidence URL must match supplier.source_url")
        if inventory_evidence.get("captured_at") != inventory.get("observed_at"):
            failures.append("inventory.observed_at must match its evidence captured_at")
    inventory_time_issue = timestamp_issue(
        inventory.get("observed_at"), now, stock_max_age, "inventory.observed_at"
    )
    if inventory_time_issue:
        failures.append(inventory_time_issue)
    order_quantity = 0
    if None not in (in_stock, moq, order_multiple):
        order_quantity = required_order_quantity(
            int(context.get("board_quantity", 0)),
            int(requirement.get("quantity_per_board", 0)),
            float(context.get("attrition_rate", 0)),
            int(moq),
            int(order_multiple),
        )
        if int(in_stock) < order_quantity:
            failures.append(f"inventory stock {in_stock} is below orderable requirement {order_quantity}")

    pricing = mapping_value(candidate.get("pricing"))
    applied_unit_price = price_at_quantity(pricing, order_quantity, failures)
    if pricing.get("currency") != context.get("currency"):
        failures.append("pricing.currency must match sourcing.context.currency")
    pricing_evidence = role_evidence("pricing", pricing.get("evidence_id"), evidence_by_id, policy, failures)
    check_role_assertions("pricing", pricing, pricing_evidence, policy, failures)

    lifecycle = mapping_value(candidate.get("lifecycle"))
    lifecycle_status = normalized(lifecycle.get("status"))
    allowed_lifecycle = {
        normalized(value) for value in policy_strings(policy, "candidate_manifest.qualified_lifecycle_status_values")
    }
    if lifecycle_status not in allowed_lifecycle:
        failures.append(f"lifecycle.status is not qualified for a new design: {lifecycle.get('status')}")
    lifecycle_evidence = role_evidence("lifecycle", lifecycle.get("evidence_id"), evidence_by_id, policy, failures)
    check_role_assertions("lifecycle", lifecycle, lifecycle_evidence, policy, failures)

    assembly_class = "standard"
    if disposition == "pcba":
        assembly = mapping_value(candidate.get("assembly"))
        expected_provider = str(get_path(context, "assembly.provider_id") or "")
        if assembly.get("provider_id") != expected_provider:
            failures.append("assembly.provider_id does not match sourcing context")
        if assembly.get("supported") is not True:
            failures.append("assembly.supported must be true for a PCBA requirement")
        assembly_stock = integer_value(assembly.get("in_stock"))
        if get_path(policy, "candidate_manifest.require_assembly_stock") is True:
            if assembly_stock is None or assembly_stock < 0:
                failures.append("assembly.in_stock must be a non-negative integer")
            elif assembly_stock < order_quantity:
                failures.append(
                    f"assembly stock {assembly_stock} is below orderable requirement {order_quantity}"
                )
        assembly_class = normalized(assembly.get("library_class"))
        assembly_scores = normalized_mapping(get_path(policy, "candidate_manifest.assembly_library_class_scores"))
        if assembly_class not in {normalized(key) for key in assembly_scores}:
            failures.append(f"assembly.library_class is unsupported: {assembly.get('library_class')}")
        assembly_evidence = role_evidence(
            "assembly", assembly.get("evidence_id"), evidence_by_id, policy, failures
        )
        check_role_assertions("assembly", assembly, assembly_evidence, policy, failures)
        if assembly_evidence is not None and assembly_evidence.get("captured_at") != assembly.get("observed_at"):
            failures.append("assembly.observed_at must match its evidence captured_at")
        issue = timestamp_issue(assembly.get("observed_at"), now, stock_max_age, "assembly.observed_at")
        if issue:
            failures.append(issue)

    capabilities = mapping_value(candidate.get("capabilities"))
    criteria = requirement_criteria(requirement, constraints, policy, roles=roles)
    require_capability_evidence = get_path(policy, "machine_constraint.require_evidence_ids") is True
    for criterion in criteria:
        for leaf in criterion_leaves(criterion, policy):
            attribute = str(leaf.get("attribute", "")).strip()
            capability = capabilities.get(attribute)
            if isinstance(capability, dict) and require_capability_evidence:
                evidence_ids = string_list(capability.get("evidence_ids"))
                if not evidence_ids:
                    failures.append(f"capability {attribute} must reference evidence_ids")
                for evidence_id in evidence_ids:
                    if evidence_id not in evidence_by_id:
                        failures.append(f"capability {attribute} references unknown evidence {evidence_id}")
                        continue
                    evidence = evidence_by_id[evidence_id]
                    actual_capability = capability.get("values") if "values" in capability else capability.get("value")
                    asserted_capability = get_path(evidence, f"assertions.capabilities.{attribute}")
                    if asserted_capability != actual_capability:
                        failures.append(
                            f"capability {attribute} evidence {evidence_id} assertion does not match candidate capability"
                        )
                    verified_text = str(evidence.get("_verified_text", "")).casefold()
                    for scalar in scalar_values(asserted_capability):
                        if str(scalar).casefold() not in verified_text:
                            failures.append(
                                f"capability {attribute} evidence {evidence_id} assertion is not present in captured content"
                            )
        passed, reason = evaluate_candidate_criterion(criterion, capabilities, policy)
        if not passed:
            failures.append(f"{criterion.get('source')}: {reason}")

    weights = mapping_value(get_path(policy, "ranking.weights"))
    score_scale = float(get_path(policy, "ranking.score_scale") or 100.0)
    stock_cap = float(get_path(policy, "ranking.stock_ratio_cap") or 10.0)
    stock_ratio = min((float(in_stock or 0) / max(order_quantity, 1)), stock_cap) / stock_cap
    assembly_scores = normalized_mapping(get_path(policy, "candidate_manifest.assembly_library_class_scores"))
    assembly_factor = float(assembly_scores.get(assembly_class, 0.0)) if disposition == "pcba" else 1.0
    package_factor = float(confidence_scores.get(package_confidence, 0.0))
    extended_cost = applied_unit_price * Decimal(order_quantity)
    maximum_component_total = decimal_value(get_path(context, "cost_budget.maximum_component_order_total"))
    if maximum_component_total is not None and maximum_component_total > 0:
        cost_factor = max(0.0, 1.0 - min(float(extended_cost / maximum_component_total), 1.0))
    else:
        cost_factor = 1.0 / (1.0 + float(extended_cost))
    evidence_factor = 1.0 if required_evidence.issubset(evidence_kinds) else 0.0
    score = score_scale * (
        float(weights.get("stock_margin", 0.0)) * stock_ratio
        + float(weights.get("assembly_class", 0.0)) * assembly_factor
        + float(weights.get("landed_cost", 0.0)) * cost_factor
        + float(weights.get("evidence", 0.0)) * evidence_factor
        + float(weights.get("package_confidence", 0.0)) * package_factor
    )
    return {
        "candidate_id": candidate_id,
        "qualified": not failures,
        "reasons": failures,
        "score": round(score, 6) if not failures else 0.0,
        "order_quantity": order_quantity,
        "in_stock": in_stock,
        "unit_price": float(applied_unit_price),
        "extended_cost": float(extended_cost),
        "needed_quantity": needed_quantity(
            int(context.get("board_quantity", 0)),
            int(requirement.get("quantity_per_board", 0)),
            float(context.get("attrition_rate", 0)),
        ),
        "moq_waste_quantity": max(
            0,
            order_quantity
            - needed_quantity(
                int(context.get("board_quantity", 0)),
                int(requirement.get("quantity_per_board", 0)),
                float(context.get("attrition_rate", 0)),
            ),
        ),
        "manufacturer": candidate.get("manufacturer"),
        "mpn": candidate.get("mpn"),
        "supplier_part": supplier_part,
    }


def candidate_capability(candidate: dict[str, Any], attribute: str) -> Any:
    return mapping_value(candidate.get("capabilities")).get(attribute)


def validate_selected_set(
    spec: dict[str, Any],
    manifest: dict[str, Any],
    requirement_results: list[dict[str, Any]],
    policy: dict[str, Any],
    result: CheckResult,
) -> dict[str, Any]:
    selected_candidates: dict[str, dict[str, Any]] = {}
    selected_evaluations: dict[str, dict[str, Any]] = {}
    manifest_candidates = candidate_map_for_manifest(manifest)
    for requirement_result in requirement_results:
        requirement_id = str(requirement_result.get("requirement_id", ""))
        candidate_id = str(requirement_result.get("selected_candidate_id", ""))
        candidate = manifest_candidates.get((requirement_id, candidate_id))
        if candidate is not None:
            selected_candidates[requirement_id] = candidate
        for evaluation in list_value(requirement_result.get("candidates")):
            if isinstance(evaluation, dict) and str(evaluation.get("candidate_id")) == candidate_id:
                selected_evaluations[requirement_id] = evaluation

    compatibility_results: list[dict[str, Any]] = []
    for constraint in list_value(get_path(spec, "sourcing.compatibility_constraints")):
        if not isinstance(constraint, dict):
            continue
        identifier = str(constraint.get("id", ""))
        left_id = str(constraint.get("left_requirement_id", ""))
        right_id = str(constraint.get("right_requirement_id", ""))
        left_candidate = selected_candidates.get(left_id)
        if left_candidate is None:
            result.issue(f"selected-set compatibility {identifier} has no selected left candidate")
            continue
        left_attribute = str(constraint.get("left_attribute", ""))
        left_capability = candidate_capability(left_candidate, left_attribute)
        if right_id:
            right_candidate = selected_candidates.get(right_id)
            if right_candidate is None:
                result.issue(f"selected-set compatibility {identifier} has no selected right candidate")
                continue
            right_attribute = str(constraint.get("right_attribute", ""))
            right_capability = candidate_capability(right_candidate, right_attribute)
            right_map = right_capability if isinstance(right_capability, dict) else {"value": right_capability}
            expected = right_map.get("values") if "values" in right_map else right_map.get("value")
            unit = right_map.get("unit")
        else:
            expected = constraint.get("value")
            unit = constraint.get("unit")
        criterion = {
            "attribute": left_attribute,
            "operator": constraint.get("operator"),
            "value": expected,
            **({"unit": unit} if unit is not None else {}),
        }
        passed, reason = evaluate_criterion(criterion, left_capability, policy)
        compatibility_results.append({"id": identifier, "passed": passed, "reason": reason})
        if not passed:
            result.issue(f"selected-set compatibility {identifier} failed: {reason}")

    component_total = sum(
        (Decimal(str(item.get("extended_cost", 0))) for item in selected_evaluations.values()),
        Decimal(0),
    )
    context = mapping_value(get_path(spec, "sourcing.context"))
    board_quantity = integer_value(context.get("board_quantity")) or 0
    component_per_board = component_total / Decimal(board_quantity) if board_quantity > 0 else Decimal(0)
    estimate_total = sum(
        (
            decimal_value(line.get("amount")) or Decimal(0)
            for line in list_value(get_path(context, "cost_budget.estimate_lines"))
            if isinstance(line, dict)
        ),
        Decimal(0),
    )
    order_total = component_total + estimate_total
    budget_limits = {
        "maximum_component_cost_per_board": component_per_board,
        "maximum_component_order_total": component_total,
        "maximum_order_total": order_total,
    }
    for field, actual in budget_limits.items():
        limit = decimal_value(get_path(context, f"cost_budget.{field}"))
        if limit is not None and actual > limit:
            result.issue(f"selected-set cost {field} {actual} exceeds budget {limit}")
    return {
        "compatibility": compatibility_results,
        "component_order_total": float(component_total),
        "component_cost_per_board": float(component_per_board),
        "non_component_estimate_total": float(estimate_total),
        "estimated_order_total": float(order_total),
        "currency": context.get("currency"),
        "moq_waste_quantity": sum(
            int(item.get("moq_waste_quantity", 0)) for item in selected_evaluations.values()
        ),
    }


def evaluate_candidate_manifest(
    spec: dict[str, Any],
    spec_path: Path | None,
    result: CheckResult,
    policy: dict[str, Any] | None = None,
    force: bool = False,
    as_of: str | datetime | None = None,
) -> dict[str, Any]:
    policy = policy or load_policy(spec)
    context_details = check_sourcing_context(
        spec, result, policy=policy, force=force, spec_path=spec_path
    )
    details: dict[str, Any] = {"enabled": context_details.get("enabled", False), "context": context_details}
    if not details["enabled"] or result.issues:
        return details
    if as_of is not None and not (
        os.environ.get("KICAD_PCB_TEST_MODE") == "1"
        and get_path(spec, "validation.part_selection.allow_test_clock") is True
    ):
        result.issue("test clock override requires KICAD_PCB_TEST_MODE=1 and validation.part_selection.allow_test_clock: true")
        return details
    now = effective_now(as_of)
    root = Path(str(context_details.get("project_root") or project_root(spec, spec_path, policy, result)))
    candidate_path = configured_artifact_path(spec, policy, "candidate_path_field", result, root=root)
    if candidate_path is None:
        return details
    if not candidate_path.is_file():
        result.issue(f"candidate manifest does not exist: {candidate_path}")
        return details
    try:
        manifest = load_data_file(candidate_path)
    except (OSError, ValueError, yaml.YAMLError, json.JSONDecodeError) as error:
        result.issue(f"cannot load candidate manifest {candidate_path}: {error}")
        return details
    manifest_sha = sha256_file(candidate_path)
    candidate_path_field = get_path(policy, "artifact_rules.candidate_path_field")
    candidate_configured = get_path(spec, str(candidate_path_field))
    sourcing = mapping_value(spec.get("sourcing"))
    context = mapping_value(sourcing.get("context"))
    requirements = {
        str(item["id"]).strip(): item
        for item in list_value(sourcing.get("requirements"))
        if isinstance(item, dict) and string_value(item.get("id"))
    }
    roles = {
        str(item["id"]).strip(): item
        for item in list_value(sourcing.get("roles"))
        if isinstance(item, dict) and string_value(item.get("id"))
    }
    providers = {
        str(item["id"]).strip(): item
        for item in list_value(context.get("providers"))
        if isinstance(item, dict) and string_value(item.get("id"))
    }
    _, constraints = architecture_maps(spec, policy)
    if integer_value(manifest.get("schema_version")) not in {
        integer_value(value) for value in list_value(policy.get("schema_versions"))
    }:
        result.issue("candidate manifest schema_version is unsupported")
    project_name = get_path(spec, "project.name")
    if manifest.get("project_name") != project_name:
        result.issue("candidate manifest project_name does not match the spec")
    expected_architecture_sha = architecture_digest(mapping_value(spec.get("architecture")))
    if manifest.get("architecture_sha256") != expected_architecture_sha:
        result.issue("candidate manifest architecture_sha256 is stale")
    expected_context_sha = canonical_sha256(context)
    if manifest.get("sourcing_context_sha256") != expected_context_sha:
        result.issue("candidate manifest sourcing_context_sha256 is stale")
    manifest_time_issue = timestamp_issue(
        manifest.get("created_at"),
        now,
        float(context.get("stock_max_age_hours", 0)),
        "candidate_manifest.created_at",
    )
    if manifest_time_issue:
        result.issue(manifest_time_issue)

    artifacts_root = resolved_path(get_path(spec, "project.artifacts_dir"), root)
    batches: dict[str, dict[str, Any]] = {}
    for index, batch in enumerate(list_value(manifest.get("batches"))):
        if not isinstance(batch, dict):
            result.issue(f"candidate manifest batches[{index}] must be a mapping")
            continue
        requirement_id = str(batch.get("requirement_id", "")).strip()
        if requirement_id not in requirements:
            result.issue(f"candidate batch references unknown requirement: {requirement_id}")
            continue
        if requirement_id in batches:
            result.issue(f"duplicate candidate batch for requirement: {requirement_id}")
        batches[requirement_id] = batch

    requirement_results: list[dict[str, Any]] = []
    successful_stops = {normalized(value) for value in policy_strings(policy, "candidate_manifest.successful_stop_reasons")}
    allowed_stops = {normalized(value) for value in policy_strings(policy, "candidate_manifest.stop_reasons")}
    max_candidates = int(context.get("max_candidates_per_requirement", 0))
    max_rounds = int(context.get("max_search_rounds", 0))
    for requirement_id, requirement in requirements.items():
        batch = batches.get(requirement_id)
        if batch is None:
            result.issue(f"required sourcing requirement has no candidate batch: {requirement_id}")
            continue
        search = mapping_value(batch.get("search"))
        rounds = integer_value(search.get("rounds"))
        if rounds is None or rounds < 1 or rounds > max_rounds:
            result.issue(f"candidate batch {requirement_id} search.rounds must be between 1 and {max_rounds}")
        stop_reason = normalized(search.get("stop_reason"))
        if stop_reason not in allowed_stops:
            result.issue(f"candidate batch {requirement_id} has unsupported stop_reason: {search.get('stop_reason')}")
        queries = list_value(search.get("queries"))
        if not queries:
            result.issue(f"candidate batch {requirement_id} must record at least one search query")
        for query_index, query in enumerate(queries):
            if not isinstance(query, dict):
                result.issue(f"candidate batch {requirement_id} search.queries[{query_index}] must be a mapping")
                continue
            if str(query.get("provider_id", "")).strip() not in providers:
                result.issue(f"candidate batch {requirement_id} search query references an unknown provider")
            if not string_value(query.get("query")):
                result.issue(f"candidate batch {requirement_id} search query text is missing")
            elif has_unknown_token(query.get("query"), policy):
                result.issue(f"candidate batch {requirement_id} search query contains an unresolved placeholder")
            query_time_issue = timestamp_issue(
                query.get("captured_at"),
                now,
                float(context.get("stock_max_age_hours", 0)),
                f"candidate batch {requirement_id} search query captured_at",
            )
            if query_time_issue:
                result.issue(query_time_issue)
        candidates = list_value(batch.get("candidates"))
        if len(candidates) > max_candidates:
            result.issue(f"candidate batch {requirement_id} exceeds max_candidates_per_requirement {max_candidates}")
        candidate_ids: set[str] = set()
        evaluations: list[dict[str, Any]] = []
        for candidate in candidates:
            evaluation = evaluate_candidate(
                candidate,
                requirement,
                constraints,
                providers,
                context,
                policy,
                now,
                artifacts_root,
                roles=roles,
                root=root,
            )
            candidate_id = str(evaluation.get("candidate_id"))
            if candidate_id in candidate_ids:
                result.issue(f"candidate batch {requirement_id} contains duplicate candidate id {candidate_id}")
            candidate_ids.add(candidate_id)
            evaluations.append(evaluation)
        ranked = sorted(evaluations, key=lambda item: (-float(item.get("score", 0.0)), str(item.get("candidate_id"))))
        for rank, item in enumerate([entry for entry in ranked if entry.get("qualified")], start=1):
            item["rank"] = rank
        qualified = [item for item in ranked if item.get("qualified")]
        for item in ranked:
            if not item.get("qualified"):
                result.warning(
                    f"candidate {requirement_id}/{item.get('candidate_id')} rejected: "
                    + "; ".join(str(reason) for reason in item.get("reasons", []))
                )
        if not qualified:
            result.issue(f"sourcing requirement {requirement_id} has no qualified candidate")
        if qualified and stop_reason not in successful_stops:
            result.issue(f"candidate batch {requirement_id} found qualified candidates but stop_reason is {stop_reason}")
        if not qualified and stop_reason in successful_stops:
            result.issue(f"candidate batch {requirement_id} claims success without a qualified candidate")
        requirement_results.append(
            {
                "requirement_id": requirement_id,
                "selected_candidate_id": qualified[0]["candidate_id"] if qualified else None,
                "candidates": ranked,
            }
        )

    selected_supplier_parts: dict[tuple[str, str], str] = {}
    manifest_candidates = candidate_map_for_manifest(manifest)
    for requirement_result in requirement_results:
        requirement_id = str(requirement_result.get("requirement_id"))
        candidate_id = requirement_result.get("selected_candidate_id")
        if not string_value(candidate_id):
            continue
        candidate = manifest_candidates.get((requirement_id, str(candidate_id)))
        if not isinstance(candidate, dict):
            continue
        key = (
            str(get_path(candidate, "supplier.provider_id") or ""),
            str(get_path(candidate, "supplier.part_number") or ""),
        )
        previous = selected_supplier_parts.get(key)
        if previous is not None:
            result.issue(
                f"selected supplier part {key[0]}/{key[1]} is split across requirements {previous} and {requirement_id}; combine component_refs so stock is counted once"
            )
        selected_supplier_parts[key] = requirement_id

    selected_set = validate_selected_set(spec, manifest, requirement_results, policy, result)

    details.update(
        {
            "candidate_manifest": candidate_configured,
            "candidate_manifest_resolved": str(candidate_path),
            "candidate_manifest_sha256": manifest_sha,
            "architecture_sha256": expected_architecture_sha,
            "sourcing_context_sha256": expected_context_sha,
            "evaluated_at": now.isoformat().replace("+00:00", "Z"),
            "requirements": requirement_results,
            "qualified_requirements": sum(1 for item in requirement_results if item.get("selected_candidate_id")),
            "selected_set": selected_set,
        }
    )
    return details


def ranking_report(spec: dict[str, Any], evaluation: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "project_name": get_path(spec, "project.name"),
        "evaluated_at": evaluation.get("evaluated_at"),
        "architecture_sha256": evaluation.get("architecture_sha256"),
        "sourcing_context_sha256": evaluation.get("sourcing_context_sha256"),
        "candidate_manifest": {
            "path": evaluation.get("candidate_manifest"),
            "sha256": evaluation.get("candidate_manifest_sha256"),
        },
        "requirements": evaluation.get("requirements", []),
        "selected_set": evaluation.get("selected_set", {}),
    }
