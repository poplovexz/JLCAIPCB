#!/usr/bin/env python3
"""Validate supply-chain-first part selection before production PCB generation."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pcb_skill_checks import CheckResult, get_path, load_spec, load_yaml, print_result, string_value  # noqa: E402
from architecture_gate import check_architecture  # noqa: E402
from _part_lock import check_part_lock  # noqa: E402


def mapping_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if string_value(item)]


def bool_config(config: dict[str, Any], key: str, default: bool = False) -> bool:
    value = config.get(key)
    if value is None:
        return default
    return bool(value)


def policy_path(spec: dict[str, Any]) -> Path:
    default = Path(__file__).resolve().parents[1] / "assets" / "jlc-lcsc-part-selection-policy.yaml"
    configured = get_path(spec, "validation.part_selection.policy_file")
    if string_value(configured):
        builtin = load_yaml(default)
        stage = normalized(get_path(spec, "project.stage"))
        production = {normalized(value) for value in policy_values(builtin, "production_stage_values")}
        if stage in production:
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


def policy_values(policy: dict[str, Any], key: str) -> list[str]:
    return string_list(policy.get(key))


def normalized(value: Any) -> str:
    return str(value).strip().lower() if value is not None else ""


def has_unknown_token(value: Any, policy: dict[str, Any]) -> bool:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return False
    if not string_value(value):
        return True
    text = str(value).strip().upper()
    return any(token.upper() in text for token in policy_values(policy, "unknown_tokens"))


def component_ref(component: dict[str, Any], index: int) -> str:
    ref = component.get("ref")
    return str(ref).strip() if string_value(ref) else f"components[{index}]"


def nested_mappings(component: dict[str, Any]) -> list[dict[str, Any]]:
    mappings = [component]
    for key in ("assembly", "sourcing", "source", "part"):
        value = component.get(key)
        if isinstance(value, dict):
            mappings.append(value)
    return mappings


def field_value(component: dict[str, Any], field: str) -> Any:
    if "." in field:
        value = get_path(component, field)
        if value is not None:
            return value
    for mapping in nested_mappings(component):
        if field in mapping:
            return mapping[field]
    return None


def has_any_field(component: dict[str, Any], fields: list[str], policy: dict[str, Any]) -> bool:
    return any(not has_unknown_token(field_value(component, field), policy) for field in fields)


def missing_fields(component: dict[str, Any], fields: list[str], policy: dict[str, Any]) -> list[str]:
    return [field for field in fields if has_unknown_token(field_value(component, field), policy)]


def status_values(component: dict[str, Any], policy: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for field in policy_values(policy, "status_fields"):
        value = field_value(component, field)
        if string_value(value):
            raw = str(value).strip()
            values.add(raw.lower())
            values.update(part for part in re.split(r"[^A-Za-z0-9_+-]+", raw.lower()) if part)
    return values


def has_boolean_marker(component: dict[str, Any], policy: dict[str, Any]) -> bool:
    for field in policy_values(policy, "dnp_fields"):
        value = field_value(component, field)
        if value is True:
            return True
    return False


def is_nonblocking_component(component: dict[str, Any], policy: dict[str, Any]) -> bool:
    if has_boolean_marker(component, policy):
        return True
    values = status_values(component, policy)
    nonblocking = {value.lower() for value in policy_values(policy, "nonblocking_status_values")}
    if values & nonblocking:
        return True
    ref = str(component.get("ref") or "")
    if any(ref.startswith(prefix) for prefix in policy_values(policy, "excluded_ref_prefixes")):
        return True
    footprint = str(component.get("footprint") or "")
    return any(token in footprint for token in policy_values(policy, "excluded_footprint_tokens"))


def nonprocured_trace_coverage_allowed(component: dict[str, Any], policy: dict[str, Any]) -> bool:
    for field in policy_values(policy, "architecture_trace_nonprocured_coverage_fields"):
        if field_value(component, field) is True:
            return True
    allowed_statuses = {
        value.lower() for value in policy_values(policy, "architecture_trace_nonprocured_coverage_status_values")
    }
    return bool(status_values(component, policy) & allowed_statuses)


def assembly_enabled(spec: dict[str, Any]) -> bool:
    assembly = get_path(spec, "manufacturing.jlcpcb.assembly")
    return isinstance(assembly, dict) and assembly.get("enabled") is True


def has_jlcpcb_release(spec: dict[str, Any]) -> bool:
    return isinstance(get_path(spec, "manufacturing.jlcpcb.release"), dict)


def is_production_candidate(spec: dict[str, Any], policy: dict[str, Any], force: bool = False) -> bool:
    if force:
        return True
    validation = mapping_value(spec.get("validation"))
    config = mapping_value(validation.get("part_selection"))
    if config.get("required") is True:
        return True
    if isinstance(spec.get("sourcing"), dict):
        return True
    if config.get("enabled") is False:
        return False
    manufacturing = mapping_value(spec.get("manufacturing"))
    target = normalized(manufacturing.get("target"))
    target_values = {normalized(value) for value in policy_values(policy, "manufacturing_targets")}
    target_matches = target in target_values if target_values else False
    stage = normalized(get_path(spec, "project.stage"))
    production_stages = {normalized(value) for value in policy_values(policy, "production_stage_values")}
    return (
        assembly_enabled(spec)
        or bool(manufacturing.get("production_ready"))
        or bool(manufacturing.get("order_ready"))
        or has_jlcpcb_release(spec)
        or (target_matches and stage in production_stages)
    )


def selected_part_ready(component: dict[str, Any], policy: dict[str, Any]) -> bool:
    if has_any_field(component, policy_values(policy, "selected_part_fields"), policy):
        return True
    if policy.get("allow_ready_status_without_supplier_part") is not True:
        return False
    values = status_values(component, policy)
    ready_values = {value.lower() for value in policy_values(policy, "ready_status_values")}
    return bool(values & ready_values)


def check_component(
    component: dict[str, Any],
    index: int,
    policy: dict[str, Any],
    result: CheckResult,
    require_assembly_package: bool,
    require_pcba_availability: bool,
) -> None:
    ref = component_ref(component, index)
    if is_nonblocking_component(component, policy):
        return

    if not selected_part_ready(component, policy):
        fields = ", ".join(policy_values(policy, "selected_part_fields"))
        result.issue(f"{ref} missing locked supplier part field before production design: {fields}")

    for field in missing_fields(component, policy_values(policy, "required_identity_fields"), policy):
        result.issue(f"{ref} missing part identity field: {field}")

    for field in missing_fields(component, policy_values(policy, "required_design_fields"), policy):
        result.issue(f"{ref} missing design-lock field: {field}")

    if not has_any_field(component, policy_values(policy, "evidence_any_fields"), policy):
        fields = ", ".join(policy_values(policy, "evidence_any_fields"))
        result.issue(f"{ref} missing source/datasheet evidence field; one of required: {fields}")

    if require_assembly_package and not has_any_field(component, policy_values(policy, "assembly_package_fields"), policy):
        fields = ", ".join(policy_values(policy, "assembly_package_fields"))
        result.issue(f"{ref} missing assembly package token/packaging field; one of required: {fields}")

    if require_pcba_availability and not any(
        field_value(component, field) is True for field in policy_values(policy, "pcba_availability_fields")
    ):
        fields = ", ".join(policy_values(policy, "pcba_availability_fields"))
        result.issue(f"{ref} missing true PCBA availability field; one of required: {fields}")


def architecture_prerequisite(
    spec: dict[str, Any], policy: dict[str, Any], result: CheckResult, spec_path: Path | None = None
) -> dict[str, Any]:
    if policy.get("require_architecture_gate_before_selection") is not True:
        return {"required": False}
    architecture_result = CheckResult()
    details = check_architecture(
        spec, architecture_result, force=True, before_sourcing=True, spec_path=spec_path
    )
    for issue in architecture_result.issues:
        result.issue(f"architecture prerequisite: {issue}")
    for warning in architecture_result.warnings:
        result.warning(f"architecture prerequisite: {warning}")
    return {"required": True, "ok": architecture_result.ok(), **details}


def declared_string_list(prefix: str, value: Any, result: CheckResult, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or any(not string_value(item) for item in value):
        result.issue(f"{prefix} must be a list of non-empty strings")
        return []
    values = [str(item).strip() for item in value]
    if not allow_empty and not values:
        result.issue(f"{prefix} must not be empty")
    if len(set(values)) != len(values):
        result.issue(f"{prefix} must not contain duplicates")
    return values


def check_architecture_traceability(
    spec: dict[str, Any], components: list[Any], policy: dict[str, Any], result: CheckResult
) -> dict[str, Any]:
    architecture = mapping_value(spec.get("architecture"))
    blocks = [block for block in list_value(architecture.get("blocks")) if isinstance(block, dict)]
    if not blocks:
        result.issue("architecture blocks are required for component traceability")
        return {"required_blocks": 0, "covered_blocks": 0, "required_constraints": 0, "covered_constraints": 0}

    trace_field = str(policy.get("architecture_trace_field", "")).strip()
    block_field = str(policy.get("architecture_trace_block_field", "")).strip()
    constraint_field = str(policy.get("architecture_trace_constraint_field", "")).strip()
    architecture_constraint_field = str(policy.get("architecture_selection_constraints_field", "")).strip()
    configured_fields = [trace_field, block_field, constraint_field, architecture_constraint_field]
    if any(not field for field in configured_fields):
        result.issue("part-selection policy must declare architecture trace and constraint field names")
        return {"required_blocks": 0, "covered_blocks": 0, "required_constraints": 0, "covered_constraints": 0}

    block_map = {str(block.get("id", "")).strip(): block for block in blocks if string_value(block.get("id"))}
    required_scopes = {normalized(value) for value in policy_values(policy, "required_architecture_block_scopes")}
    exempt_categories = {normalized(value) for value in policy_values(policy, "architecture_block_exempt_categories")}
    required_blocks = {
        block_id
        for block_id, block in block_map.items()
        if block.get("required") is True
        and normalized(block.get("scope")) in required_scopes
        and normalized(block.get("category")) not in exempt_categories
    }

    constraint_owners: dict[str, str] = {}
    required_constraints: set[str] = set()
    for block_id, block in block_map.items():
        constraints = block.get(architecture_constraint_field)
        if constraints is None:
            continue
        if not isinstance(constraints, list):
            result.issue(f"architecture block {block_id}.{architecture_constraint_field} must be a list")
            continue
        for index, constraint in enumerate(constraints):
            if not isinstance(constraint, dict):
                result.issue(f"architecture block {block_id}.{architecture_constraint_field}[{index}] must be a mapping")
                continue
            constraint_id = str(constraint.get("id", "")).strip()
            if not constraint_id:
                continue
            constraint_owners[constraint_id] = block_id
            if constraint.get("required") is True and block_id in required_blocks:
                required_constraints.add(constraint_id)

    covered_blocks: set[str] = set()
    covered_constraints: set[str] = set()
    for index, component in enumerate(components):
        if not isinstance(component, dict):
            continue
        nonblocking = is_nonblocking_component(component, policy)
        if nonblocking and not nonprocured_trace_coverage_allowed(component, policy):
            continue
        ref = component_ref(component, index)
        trace = component.get(trace_field)
        if not isinstance(trace, dict):
            if policy.get("require_each_selected_component_trace") is True or nonblocking:
                result.issue(f"{ref} missing {trace_field} mapping to architecture blocks and constraints")
            continue
        block_ids = declared_string_list(f"{ref}.{trace_field}.{block_field}", trace.get(block_field), result)
        constraint_ids = declared_string_list(
            f"{ref}.{trace_field}.{constraint_field}", trace.get(constraint_field), result
        )
        valid_blocks = {block_id for block_id in block_ids if block_id in block_map}
        for block_id in block_ids:
            if block_id not in block_map:
                result.issue(f"{ref}.{trace_field}.{block_field} references unknown architecture block: {block_id}")
        for constraint_id in constraint_ids:
            owner = constraint_owners.get(constraint_id)
            if owner is None:
                result.issue(
                    f"{ref}.{trace_field}.{constraint_field} references unknown architecture constraint: {constraint_id}"
                )
            elif owner not in valid_blocks:
                result.issue(
                    f"{ref}.{trace_field}.{constraint_field} constraint {constraint_id} belongs to unmapped block {owner}"
                )
            else:
                covered_constraints.add(constraint_id)
        covered_blocks.update(valid_blocks)

    if policy.get("require_required_block_coverage") is True:
        for block_id in sorted(required_blocks - covered_blocks):
            result.issue(f"required architecture block has no selected component coverage: {block_id}")
    if policy.get("require_required_constraint_coverage") is True:
        for constraint_id in sorted(required_constraints - covered_constraints):
            result.issue(f"required architecture sourcing constraint has no selected component coverage: {constraint_id}")
    return {
        "required_blocks": len(required_blocks),
        "covered_blocks": len(required_blocks & covered_blocks),
        "required_constraints": len(required_constraints),
        "covered_constraints": len(required_constraints & covered_constraints),
    }


def check_part_selection(
    spec: dict[str, Any],
    result: CheckResult,
    force: bool = False,
    spec_path: Path | None = None,
    as_of: str | None = None,
    before_generation: bool = False,
) -> dict[str, Any]:
    policy = load_policy(spec)
    enabled = is_production_candidate(spec, policy, force=force)
    details: dict[str, Any] = {
        "enabled": enabled,
        "assembly_enabled": assembly_enabled(spec),
        "production_candidate": enabled,
        "checked_components": 0,
        "skipped_components": 0,
    }
    if not enabled:
        result.warning("part selection gate not required for this spec stage")
        return details

    stage = normalized(get_path(spec, "project.stage"))
    production_stages = {normalized(value) for value in policy_values(policy, "production_stage_values")}
    if stage in production_stages and string_value(get_path(spec, "validation.part_selection.policy_file")):
        result.issue("production part-selection policy override is forbidden; use the bundled trusted policy")

    details["architecture_prerequisite"] = architecture_prerequisite(spec, policy, result, spec_path)
    details["part_lock"] = check_part_lock(
        spec,
        spec_path,
        result,
        force=True,
        as_of=as_of,
        before_generation=before_generation,
    )

    components = spec.get("components")
    if not isinstance(components, list) or not components:
        result.issue("components must be declared before supply-chain part selection")
        return details

    assembly_is_enabled = assembly_enabled(spec)
    require_assembly_package = assembly_is_enabled and bool_config(policy, "require_package_token_when_assembly_enabled", True)
    require_pcba_availability = assembly_is_enabled and bool_config(policy, "require_pcba_availability_when_assembly_enabled", False)

    for index, component in enumerate(components):
        if not isinstance(component, dict):
            result.issue(f"components[{index}] must be a mapping")
            continue
        if is_nonblocking_component(component, policy):
            details["skipped_components"] += 1
            continue
        details["checked_components"] += 1
        check_component(component, index, policy, result, require_assembly_package, require_pcba_availability)

    details["architecture_traceability"] = check_architecture_traceability(spec, components, policy, result)

    if result.ok():
        result.warning(
            "part selection gate passed with a fresh deterministic part lock; inventory is timestamped, not reserved, and web DFM remains a later gate"
        )
    return details


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Validate JLC/LCSC supply-chain-first component selection.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--require", action="store_true")
    parser.add_argument("--as-of")
    parser.add_argument("--before-generation", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])

    result = CheckResult()
    details: dict[str, Any] = {}
    try:
        details = check_part_selection(
            load_spec(args.spec),
            result,
            force=args.require,
            spec_path=args.spec,
            as_of=args.as_of,
            before_generation=args.before_generation,
        )
    except Exception as error:
        result.issue(str(error))

    if args.json_output:
        payload = {
            "check": "part_selection_check",
            "ok": result.ok(),
            "issues": result.issues,
            "warnings": result.warnings,
            "details": details,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if result.ok() else 1
    return print_result("part_selection_check", result, False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
