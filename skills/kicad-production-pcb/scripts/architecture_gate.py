#!/usr/bin/env python3
"""Validate the block-level architecture before sourcing or KiCad generation."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import deque
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pcb_skill_checks import CheckResult, get_path, load_spec, load_yaml, print_result, string_value  # noqa: E402


def mapping_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def int_value(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def normalized(value: Any) -> str:
    return str(value).strip().lower().replace("_", "-")


def architecture_digest(architecture: dict[str, Any]) -> str:
    payload = json.dumps(architecture, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def architecture_confirmation_digest(architecture: dict[str, Any], policy: dict[str, Any]) -> str:
    excluded = policy_set(policy, "confirmation_digest_excluded_keys")
    payload = {
        key: value
        for key, value in architecture.items()
        if normalized(key) not in excluded
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def resolved_path(value: Any, root: Path | None = None) -> Path:
    path = Path(str(value))
    return path.resolve() if path.is_absolute() else ((root or Path.cwd()) / path).resolve()


def architecture_project_root(spec: dict[str, Any], policy: dict[str, Any], spec_path: Path | None = None) -> Path:
    configured = get_path(spec, "project.root_dir")
    if string_value(configured):
        base = spec_path.resolve().parent if spec_path is not None else Path.cwd()
        return resolved_path(configured, base)
    configured = get_path(spec, "validation.architecture.project_root") or policy.get("default_project_root")
    if not string_value(configured):
        raise ValueError("project.root_dir, validation.architecture.project_root, or policy default_project_root is required")
    return resolved_path(configured)


def project_artifacts_root(
    spec: dict[str, Any], policy: dict[str, Any], spec_path: Path | None = None
) -> Path:
    configured = get_path(spec, "project.artifacts_dir")
    if not string_value(configured):
        configured = policy.get("default_artifacts_dir")
    if not string_value(configured):
        raise ValueError("project.artifacts_dir or policy default_artifacts_dir must be declared")
    allowed_root = architecture_project_root(spec, policy, spec_path)
    artifacts = resolved_path(configured, allowed_root)
    if policy.get("require_artifacts_dir_under_project_root") is True:
        try:
            artifacts.relative_to(allowed_root)
        except ValueError as error:
            raise ValueError(
                f"project.artifacts_dir must stay under the architecture project root {allowed_root}: {artifacts}"
            ) from error
    return artifacts


def architecture_report_path(
    spec: dict[str, Any], policy: dict[str, Any], spec_path: Path | None = None
) -> Path:
    configured = get_path(spec, "architecture.outputs.report_path")
    if not string_value(configured):
        raise ValueError("architecture.outputs.report_path must be declared")
    project_root = architecture_project_root(spec, policy, spec_path)
    path = resolved_path(configured, project_root)
    if policy.get("require_report_under_artifacts_dir") is True:
        root = project_artifacts_root(spec, policy, spec_path)
        try:
            path.relative_to(root)
        except ValueError as error:
            raise ValueError(f"architecture report path must stay under project artifacts directory {root}: {path}") from error
    return path


def policy_path(spec: dict[str, Any]) -> Path:
    default = Path(__file__).resolve().parents[1] / "assets" / "architecture-policy.yaml"
    configured = get_path(spec, "validation.architecture.policy_file")
    if string_value(configured):
        builtin = load_yaml(default)
        protected = {normalized(value) for value in policy_values(builtin, "protected_policy_override_targets")}
        stage = normalized(get_path(spec, "project.stage"))
        stage_target = normalized(mapping_value(builtin.get("project_stage_target_map")).get(stage))
        targets = {
            normalized(get_path(spec, "architecture.current_target")),
            normalized(get_path(spec, "requirement_intake.decision.current_target")),
            stage_target,
        }
        if protected & targets:
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
    return [str(item) for item in list_value(policy.get(key)) if string_value(item)]


def policy_set(policy: dict[str, Any], key: str) -> set[str]:
    return {normalized(item) for item in policy_values(policy, key)}


def intake_report(spec: dict[str, Any]) -> dict[str, Any]:
    report = spec.get("requirement_intake")
    if isinstance(report, dict):
        return report
    if isinstance(spec.get("intake"), dict) and isinstance(spec.get("confirmation"), dict):
        return spec
    return {}


def architecture_config(spec: dict[str, Any]) -> dict[str, Any]:
    return mapping_value(mapping_value(spec.get("validation")).get("architecture"))


def architecture_required(spec: dict[str, Any], policy: dict[str, Any], force: bool = False) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    config = architecture_config(spec)
    if force:
        reasons.append("command requires architecture")
    if config.get("required") is True:
        reasons.append("validation.architecture.required is true")
    if isinstance(spec.get("architecture"), dict):
        reasons.append("architecture section is declared")

    stage = normalized(get_path(spec, "project.stage"))
    if stage and stage in policy_set(policy, "required_for_stages"):
        reasons.append(f"project stage {stage} requires architecture")

    for path in policy_values(policy, "required_signal_paths"):
        value = get_path(spec, path)
        if value is True or (isinstance(value, (dict, list)) and bool(value)):
            reasons.append(f"spec signal {path} requires architecture")

    report = intake_report(spec)
    confirmation = mapping_value(report.get("confirmation"))
    decision = mapping_value(report.get("decision"))
    if normalized(confirmation.get("status")) in policy_set(policy, "confirmed_intake_statuses"):
        target = normalized(decision.get("current_target"))
        if target in policy_set(policy, "confirmed_intake_targets_requiring_architecture"):
            reasons.append(f"confirmed intake target {target} requires architecture")

    return bool(reasons), reasons


def check_required_fields(prefix: str, item: dict[str, Any], fields: list[str], result: CheckResult) -> None:
    for field in fields:
        value = item.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            result.issue(f"{prefix}.{field} must be declared")


def check_string_list(prefix: str, value: Any, result: CheckResult, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or any(not string_value(item) for item in value):
        result.issue(f"{prefix} must be a list of non-empty strings")
        return []
    values = [str(item).strip() for item in value]
    if not allow_empty and not values:
        result.issue(f"{prefix} must not be empty")
    if len({normalized(item) for item in values}) != len(values):
        result.issue(f"{prefix} must not contain duplicates")
    return values


def check_allowed(prefix: str, value: Any, allowed: set[str], result: CheckResult) -> str:
    token = normalized(value)
    if token and token not in allowed:
        result.issue(f"{prefix} must be one of {', '.join(sorted(allowed))}; got {value}")
    return token


def unique_identifier(prefix: str, item: dict[str, Any], identifiers: set[str], result: CheckResult) -> str:
    identifier = str(item.get("id", "")).strip()
    if not identifier:
        return ""
    if identifier in identifiers:
        result.issue(f"{prefix}.id is duplicated: {identifier}")
    identifiers.add(identifier)
    return identifier


def find_forbidden_keys(value: Any, forbidden: set[str], prefix: str = "architecture") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{prefix}.{key}"
            if normalized(key) in forbidden:
                found.append(child_path)
            found.extend(find_forbidden_keys(child, forbidden, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(find_forbidden_keys(child, forbidden, f"{prefix}[{index}]"))
    return found


def stage_ranks(policy: dict[str, Any]) -> dict[str, int]:
    ranks = mapping_value(policy.get("stage_rank"))
    result: dict[str, int] = {}
    for key, value in ranks.items():
        rank = int_value(value)
        if rank is not None:
            result[normalized(key)] = rank
    return result


def check_required_before(
    prefix: str,
    value: Any,
    current_target: str,
    ranks: dict[str, int],
    result: CheckResult,
) -> str:
    required_before = normalized(value)
    if required_before not in ranks:
        result.issue(f"{prefix}.required_before must be one of {', '.join(sorted(ranks))}; got {value}")
        return ""
    if current_target not in ranks:
        return required_before
    return required_before


def unresolved_is_blocking(current_target: str, required_before: str, ranks: dict[str, int]) -> bool:
    return current_target in ranks and required_before in ranks and ranks[current_target] >= ranks[required_before]


def phase_validation_target(
    current_target: str,
    before_sourcing: bool,
    before_generation: bool,
    policy: dict[str, Any],
    ranks: dict[str, int],
) -> str:
    targets = [current_target]
    if before_sourcing:
        targets.append(normalized(policy.get("before_sourcing_target")))
    if before_generation:
        targets.append(normalized(policy.get("before_generation_target")))
    known_targets = [target for target in targets if target in ranks]
    return max(known_targets, key=lambda target: ranks[target]) if known_targets else current_target


def check_blocks(
    architecture: dict[str, Any], policy: dict[str, Any], result: CheckResult
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    blocks = architecture.get("blocks")
    if not isinstance(blocks, list):
        result.issue("architecture.blocks must be a list")
        return {}, {}
    minimum = int(policy.get("minimum_blocks", 0))
    if len(blocks) < minimum:
        result.issue(f"architecture.blocks must contain at least {minimum} blocks")

    identifiers: set[str] = set()
    block_map: dict[str, dict[str, Any]] = {}
    categories: dict[str, str] = {}
    allowed_categories = policy_set(policy, "block_categories")
    allowed_scopes = policy_set(policy, "block_scopes")
    required_fields = policy_values(policy, "required_block_fields")
    for index, block in enumerate(blocks):
        prefix = f"architecture.blocks[{index}]"
        if not isinstance(block, dict):
            result.issue(f"{prefix} must be a mapping")
            continue
        check_required_fields(prefix, block, required_fields, result)
        identifier = unique_identifier(prefix, block, identifiers, result)
        category = check_allowed(f"{prefix}.category", block.get("category"), allowed_categories, result)
        check_allowed(f"{prefix}.scope", block.get("scope"), allowed_scopes, result)
        check_string_list(f"{prefix}.power_domains", block.get("power_domains"), result)
        if not string_value(block.get("role")):
            result.issue(f"{prefix}.role must be a non-empty string")
        if not isinstance(block.get("required"), bool):
            result.issue(f"{prefix}.required must be true or false")
        if identifier:
            block_map[identifier] = block
            categories[identifier] = category
    return block_map, categories


def check_selection_constraints(
    block_map: dict[str, dict[str, Any]],
    categories: dict[str, str],
    validation_target: str,
    ranks: dict[str, int],
    phase: str,
    policy: dict[str, Any],
    result: CheckResult,
) -> dict[str, str]:
    field = str(policy.get("selection_constraints_field", "")).strip()
    if not field:
        result.issue("architecture policy selection_constraints_field must be declared")
        return {}

    require_for_phase = normalized(phase) in policy_set(policy, "require_selection_constraints_phases")
    required_scopes = policy_set(policy, "selection_constraint_required_scopes")
    exempt_categories = policy_set(policy, "selection_constraint_exempt_categories")
    required_fields = policy_values(policy, "required_selection_constraint_fields")
    allowed_kinds = policy_set(policy, "selection_constraint_kinds")
    identifiers: set[str] = set()
    owners: dict[str, str] = {}

    for block_id, block in block_map.items():
        prefix = f"architecture block {block_id}.{field}"
        values = block.get(field)
        scope = normalized(block.get("scope"))
        category = categories.get(block_id, "")
        block_requires_constraints = (
            block.get("required") is True
            and scope in required_scopes
            and category not in exempt_categories
        )
        if values is None:
            if require_for_phase and block_requires_constraints:
                result.issue(f"{prefix} must declare sourcing constraints before {phase}")
            continue
        if not isinstance(values, list):
            result.issue(f"{prefix} must be a list")
            continue
        if require_for_phase and block_requires_constraints and not values:
            result.issue(f"{prefix} must not be empty before {phase}")
        for index, constraint in enumerate(values):
            item_prefix = f"{prefix}[{index}]"
            if not isinstance(constraint, dict):
                result.issue(f"{item_prefix} must be a mapping")
                continue
            check_required_fields(item_prefix, constraint, required_fields, result)
            identifier = unique_identifier(item_prefix, constraint, identifiers, result)
            check_allowed(f"{item_prefix}.kind", constraint.get("kind"), allowed_kinds, result)
            if not string_value(constraint.get("statement")):
                result.issue(f"{item_prefix}.statement must be a non-empty capability requirement")
            else:
                statement = str(constraint.get("statement")).upper()
                unknown = next(
                    (
                        token
                        for token in policy_values(policy, "selection_constraint_unknown_tokens")
                        if token.upper() in statement
                    ),
                    None,
                )
                if unknown:
                    result.issue(f"{item_prefix}.statement contains unresolved token: {unknown}")
            if not isinstance(constraint.get("required"), bool):
                result.issue(f"{item_prefix}.required must be true or false")
            check_required_before(item_prefix, constraint.get("required_before"), validation_target, ranks, result)
            if identifier:
                owners[identifier] = block_id
    return owners


def check_edges(
    architecture: dict[str, Any],
    block_map: dict[str, dict[str, Any]],
    categories: dict[str, str],
    policy: dict[str, Any],
    result: CheckResult,
) -> list[dict[str, Any]]:
    edges = architecture.get("block_edges")
    if not isinstance(edges, list):
        result.issue("architecture.block_edges must be a list")
        return []
    identifiers: set[str] = set()
    valid: list[dict[str, Any]] = []
    edge_kinds = policy_set(policy, "edge_kinds")
    edge_directions = policy_set(policy, "edge_directions")
    required_fields = policy_values(policy, "required_edge_fields")
    adjacency: dict[str, set[str]] = {identifier: set() for identifier in block_map}
    participating: set[str] = set()

    for index, edge in enumerate(edges):
        prefix = f"architecture.block_edges[{index}]"
        if not isinstance(edge, dict):
            result.issue(f"{prefix} must be a mapping")
            continue
        check_required_fields(prefix, edge, required_fields, result)
        unique_identifier(prefix, edge, identifiers, result)
        source = str(edge.get("from", "")).strip()
        target = str(edge.get("to", "")).strip()
        if source not in block_map:
            result.issue(f"{prefix}.from references unknown block: {source}")
        if target not in block_map:
            result.issue(f"{prefix}.to references unknown block: {target}")
        if source and source == target:
            result.issue(f"{prefix} cannot connect a block to itself: {source}")
        check_allowed(f"{prefix}.kind", edge.get("kind"), edge_kinds, result)
        check_allowed(f"{prefix}.direction", edge.get("direction"), edge_directions, result)
        if source in block_map and target in block_map and source != target:
            adjacency[source].add(target)
            adjacency[target].add(source)
            participating.update([source, target])
            valid.append(edge)

    isolated_allowed = policy_set(policy, "isolated_allowed_block_categories")
    required_graph_blocks = {
        identifier
        for identifier, block in block_map.items()
        if block.get("required") is True and categories.get(identifier) not in isolated_allowed
    }
    for identifier in sorted(required_graph_blocks - participating):
        result.issue(f"architecture block is orphaned from the block graph: {identifier}")
    if required_graph_blocks:
        start = sorted(required_graph_blocks)[0]
        visited = {start}
        queue = deque([start])
        while queue:
            current = queue.popleft()
            for neighbor in adjacency.get(current, set()):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        disconnected = required_graph_blocks - visited
        if disconnected:
            result.issue("architecture required blocks form disconnected graph groups: " + ", ".join(sorted(disconnected)))
    return valid


def check_power_domains(
    architecture: dict[str, Any],
    block_map: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    current_target: str,
    ranks: dict[str, int],
    policy: dict[str, Any],
    result: CheckResult,
    unresolved: list[tuple[str, str]],
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    values = architecture.get("power_domains")
    if not isinstance(values, list):
        result.issue("architecture.power_domains must be a list")
        return {}, set()
    identifiers: set[str] = set()
    domains: dict[str, dict[str, Any]] = {}
    required_protection: set[str] = set()
    power_edges = {
        (str(edge.get("from", "")), str(edge.get("to", "")))
        for edge in edges
        if normalized(edge.get("kind")) == "power"
    }
    for index, domain in enumerate(values):
        prefix = f"architecture.power_domains[{index}]"
        if not isinstance(domain, dict):
            result.issue(f"{prefix} must be a mapping")
            continue
        check_required_fields(prefix, domain, policy_values(policy, "required_power_domain_fields"), result)
        identifier = unique_identifier(prefix, domain, identifiers, result)
        source = str(domain.get("source_block", "")).strip()
        consumers = check_string_list(f"{prefix}.consumer_blocks", domain.get("consumer_blocks"), result, allow_empty=False)
        if source not in block_map:
            result.issue(f"{prefix}.source_block references unknown block: {source}")
        for consumer in consumers:
            if consumer not in block_map:
                result.issue(f"{prefix}.consumer_blocks references unknown block: {consumer}")
            if consumer == source:
                result.issue(f"{prefix} cannot use its source block as a consumer: {consumer}")
            if source in block_map and consumer in block_map and (source, consumer) not in power_edges:
                result.issue(f"{prefix} missing source-to-consumer power edge: {source} -> {consumer}")
        if not string_value(domain.get("voltage_class")):
            result.issue(f"{prefix}.voltage_class must be a non-empty architecture class")
        current_class = check_allowed(
            f"{prefix}.current_class", domain.get("current_class"), policy_set(policy, "current_classes"), result
        )
        sharing = check_allowed(
            f"{prefix}.sharing", domain.get("sharing"), policy_set(policy, "power_sharing_values"), result
        )
        backfeed = check_allowed(
            f"{prefix}.backfeed_policy", domain.get("backfeed_policy"), policy_set(policy, "backfeed_policy_values"), result
        )
        protection = check_allowed(
            f"{prefix}.protection_intent", domain.get("protection_intent"), policy_set(policy, "protection_intent_values"), result
        )
        required_before = check_required_before(prefix, domain.get("required_before"), current_target, ranks, result)
        unresolved_values = {
            "current_class": (current_class, policy_set(policy, "unresolved_current_class_values")),
            "sharing": (sharing, policy_set(policy, "unresolved_power_sharing_values")),
            "backfeed_policy": (backfeed, policy_set(policy, "unresolved_backfeed_values")),
            "protection_intent": (protection, policy_set(policy, "unresolved_protection_values")),
        }
        for field, (value, unresolved_set) in unresolved_values.items():
            if value in unresolved_set and required_before:
                unresolved.append((f"{prefix}.{field}", required_before))
        if protection == "required" and identifier:
            required_protection.add(identifier)
        if identifier:
            domains[identifier] = domain

    declared_domains = set(domains)
    for block_id, block in block_map.items():
        for domain_id in list_value(block.get("power_domains")):
            if str(domain_id) not in declared_domains:
                result.issue(f"architecture block {block_id} references unknown power domain: {domain_id}")
    return domains, required_protection


def check_interfaces(
    architecture: dict[str, Any],
    block_map: dict[str, dict[str, Any]],
    domains: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    current_target: str,
    ranks: dict[str, int],
    policy: dict[str, Any],
    result: CheckResult,
    unresolved: list[tuple[str, str]],
) -> tuple[dict[str, dict[str, Any]], set[str], dict[str, str]]:
    values = architecture.get("interfaces")
    if not isinstance(values, list):
        result.issue("architecture.interfaces must be a list")
        return {}, set(), {}
    identifiers: set[str] = set()
    interfaces: dict[str, dict[str, Any]] = {}
    external: set[str] = set()
    speed_classes: dict[str, str] = {}
    edge_signatures: dict[tuple[str, str, str], set[str]] = {}
    for edge in edges:
        signature = (str(edge.get("from", "")), str(edge.get("to", "")), normalized(edge.get("kind")))
        edge_signatures.setdefault(signature, set()).add(normalized(edge.get("direction")))
    direction_map = {
        normalized(key): normalized(value)
        for key, value in mapping_value(policy.get("interface_edge_direction_map")).items()
    }
    for index, interface in enumerate(values):
        prefix = f"architecture.interfaces[{index}]"
        if not isinstance(interface, dict):
            result.issue(f"{prefix} must be a mapping")
            continue
        check_required_fields(prefix, interface, policy_values(policy, "required_interface_fields"), result)
        identifier = unique_identifier(prefix, interface, identifiers, result)
        source = str(interface.get("from", "")).strip()
        target = str(interface.get("to", "")).strip()
        for field, value in [("from", source), ("to", target)]:
            if value not in block_map:
                result.issue(f"{prefix}.{field} references unknown block: {value}")
        kind = check_allowed(f"{prefix}.kind", interface.get("kind"), policy_set(policy, "interface_kinds"), result)
        direction = check_allowed(
            f"{prefix}.direction", interface.get("direction"), policy_set(policy, "interface_directions"), result
        )
        if not isinstance(interface.get("external"), bool):
            result.issue(f"{prefix}.external must be true or false")
        if interface.get("external") is True and identifier:
            external.add(identifier)
        speed = check_allowed(
            f"{prefix}.speed_class", interface.get("speed_class"), policy_set(policy, "speed_classes"), result
        )
        if not string_value(interface.get("voltage_domain")):
            result.issue(f"{prefix}.voltage_domain must be a non-empty domain id or not_applicable")
        elif str(interface.get("voltage_domain")) not in domains and normalized(interface.get("voltage_domain")) != "not-applicable":
            result.issue(f"{prefix}.voltage_domain references unknown power domain: {interface.get('voltage_domain')}")
        risk_tags = check_string_list(f"{prefix}.risk_tags", interface.get("risk_tags"), result)
        allowed_tags = policy_set(policy, "risk_tags")
        for tag in risk_tags:
            if normalized(tag) not in allowed_tags:
                result.issue(f"{prefix}.risk_tags contains unsupported value: {tag}")
        required_before = check_required_before(prefix, interface.get("required_before"), current_target, ranks, result)
        if speed in policy_set(policy, "unresolved_speed_values") and required_before:
            unresolved.append((f"{prefix}.speed_class", required_before))
        signature = (source, target, kind)
        if source in block_map and target in block_map and kind and signature not in edge_signatures:
            result.issue(f"{prefix} has no matching block edge: {source} -> {target} ({kind})")
        elif signature in edge_signatures:
            expected_direction = direction_map.get(direction)
            if expected_direction and expected_direction not in edge_signatures[signature]:
                result.issue(
                    f"{prefix} direction {direction} does not match block edge directions: "
                    + ", ".join(sorted(edge_signatures[signature]))
                )
        if identifier:
            interfaces[identifier] = interface
            speed_classes[identifier] = speed
    return interfaces, external, speed_classes


def check_boundary_interfaces(
    block_map: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    interfaces: dict[str, dict[str, Any]],
    policy: dict[str, Any],
    result: CheckResult,
) -> None:
    external_scopes = policy_set(policy, "external_block_scopes")
    boundary_kinds = policy_set(policy, "boundary_interface_edge_kinds")
    interface_signatures: dict[tuple[str, str, str], list[tuple[str, dict[str, Any]]]] = {}
    for interface_id, interface in interfaces.items():
        signature = (
            str(interface.get("from", "")).strip(),
            str(interface.get("to", "")).strip(),
            normalized(interface.get("kind")),
        )
        interface_signatures.setdefault(signature, []).append((interface_id, interface))

    if policy.get("require_external_interface_for_boundary_edges") is True:
        for edge in edges:
            source = str(edge.get("from", "")).strip()
            target = str(edge.get("to", "")).strip()
            kind = normalized(edge.get("kind"))
            if source not in block_map or target not in block_map or kind not in boundary_kinds:
                continue
            source_external = normalized(block_map[source].get("scope")) in external_scopes
            target_external = normalized(block_map[target].get("scope")) in external_scopes
            if source_external == target_external:
                continue
            matching = interface_signatures.get((source, target, kind), [])
            if not matching:
                result.issue(f"boundary block edge has no matching external interface: {source} -> {target} ({kind})")
            elif not any(interface.get("external") is True for _, interface in matching):
                result.issue(f"boundary block edge interface must set external true: {source} -> {target} ({kind})")

    if policy.get("require_external_interfaces_to_cross_boundary") is True:
        for interface_id, interface in interfaces.items():
            if interface.get("external") is not True:
                continue
            source = str(interface.get("from", "")).strip()
            target = str(interface.get("to", "")).strip()
            if source not in block_map or target not in block_map:
                continue
            source_external = normalized(block_map[source].get("scope")) in external_scopes
            target_external = normalized(block_map[target].get("scope")) in external_scopes
            if source_external == target_external:
                result.issue(f"external interface must cross exactly one board boundary: {interface_id}")


def check_external_connectors(
    architecture: dict[str, Any],
    block_map: dict[str, dict[str, Any]],
    categories: dict[str, str],
    interfaces: dict[str, dict[str, Any]],
    external_interfaces: set[str],
    current_target: str,
    ranks: dict[str, int],
    policy: dict[str, Any],
    result: CheckResult,
    unresolved: list[tuple[str, str]],
) -> tuple[dict[str, dict[str, Any]], set[str], set[str]]:
    values = architecture.get("external_connectors")
    if not isinstance(values, list):
        result.issue("architecture.external_connectors must be a list")
        return {}, set(), set()
    identifiers: set[str] = set()
    connectors: dict[str, dict[str, Any]] = {}
    covered_interfaces: set[str] = set()
    interface_assignments: dict[str, list[str]] = {}
    required_protection: set[str] = set()
    requires_external_risk: set[str] = set()
    external_scopes = policy_set(policy, "external_block_scopes")
    for index, connector in enumerate(values):
        prefix = f"architecture.external_connectors[{index}]"
        if not isinstance(connector, dict):
            result.issue(f"{prefix} must be a mapping")
            continue
        check_required_fields(prefix, connector, policy_values(policy, "required_external_connector_fields"), result)
        identifier = unique_identifier(prefix, connector, identifiers, result)
        block_id = str(connector.get("block_id", "")).strip()
        if block_id not in block_map:
            result.issue(f"{prefix}.block_id references unknown block: {block_id}")
        elif categories.get(block_id) not in policy_set(policy, "connector_block_categories"):
            result.issue(f"{prefix}.block_id uses non-connector architecture category: {categories.get(block_id)}")
        interface_ids = check_string_list(
            f"{prefix}.interface_ids", connector.get("interface_ids"), result, allow_empty=False
        )
        for interface_id in interface_ids:
            if interface_id not in interfaces:
                result.issue(f"{prefix}.interface_ids references unknown interface: {interface_id}")
                continue
            covered_interfaces.add(interface_id)
            interface_assignments.setdefault(interface_id, []).append(identifier or prefix)
            interface = interfaces[interface_id]
            if policy.get("require_connector_interfaces_external") is True and interface.get("external") is not True:
                result.issue(f"{prefix}.interface_ids must reference an external interface: {interface_id}")
            if policy.get("require_connector_interface_endpoint_match") is True:
                endpoints = [
                    str(interface.get(field, "")).strip()
                    for field in ("from", "to")
                    if str(interface.get(field, "")).strip() in block_map
                    and normalized(block_map[str(interface.get(field, "")).strip()].get("scope")) not in external_scopes
                ]
                if block_id not in endpoints:
                    result.issue(
                        f"{prefix}.block_id must be the board-side endpoint of interface {interface_id}: "
                        + ", ".join(endpoints or ["<none>"])
                    )
        exposure = check_allowed(
            f"{prefix}.exposure", connector.get("exposure"), policy_set(policy, "connector_exposure_values"), result
        )
        hot_plug = check_allowed(
            f"{prefix}.hot_plug", connector.get("hot_plug"), policy_set(policy, "hot_plug_values"), result
        )
        protection = check_allowed(
            f"{prefix}.protection_intent",
            connector.get("protection_intent"),
            policy_set(policy, "protection_intent_values"),
            result,
        )
        if not string_value(connector.get("rationale")):
            result.issue(f"{prefix}.rationale must explain exposure and protection intent")
        required_before = check_required_before(prefix, connector.get("required_before"), current_target, ranks, result)
        if hot_plug in policy_set(policy, "unresolved_hot_plug_values") and required_before:
            unresolved.append((f"{prefix}.hot_plug", required_before))
        if protection in policy_set(policy, "unresolved_protection_values") and required_before:
            unresolved.append((f"{prefix}.protection_intent", required_before))
        if protection == "required" and identifier:
            required_protection.add(identifier)
        if exposure in policy_set(policy, "exposures_requiring_external_risk") and identifier:
            requires_external_risk.add(identifier)
        if identifier:
            connectors[identifier] = connector
    for interface_id in sorted(external_interfaces - covered_interfaces):
        result.issue(f"external architecture interface is not assigned to an exposed connector: {interface_id}")
    if policy.get("require_unique_connector_per_external_interface") is True:
        for interface_id, assigned in sorted(interface_assignments.items()):
            if interface_id in external_interfaces and len(assigned) > 1:
                result.issue(
                    f"external architecture interface is assigned to multiple connectors: {interface_id} -> "
                    + ", ".join(assigned)
                )
    return connectors, required_protection, requires_external_risk


def check_risk_paths(
    architecture: dict[str, Any],
    block_map: dict[str, dict[str, Any]],
    interfaces: dict[str, dict[str, Any]],
    domains: dict[str, dict[str, Any]],
    connectors: dict[str, dict[str, Any]],
    current_target: str,
    ranks: dict[str, int],
    policy: dict[str, Any],
    result: CheckResult,
) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]]]:
    values = architecture.get("risk_paths")
    if not isinstance(values, list):
        result.issue("architecture.risk_paths must be a list")
        return {}, {}
    identifiers: set[str] = set()
    risks: dict[str, dict[str, Any]] = {}
    kind_refs: dict[str, set[str]] = {}
    for index, risk in enumerate(values):
        prefix = f"architecture.risk_paths[{index}]"
        if not isinstance(risk, dict):
            result.issue(f"{prefix} must be a mapping")
            continue
        check_required_fields(prefix, risk, policy_values(policy, "required_risk_path_fields"), result)
        identifier = unique_identifier(prefix, risk, identifiers, result)
        kind = check_allowed(f"{prefix}.kind", risk.get("kind"), policy_set(policy, "risk_path_kinds"), result)
        references: set[str] = set()
        for field, known in [
            ("block_ids", block_map),
            ("interface_ids", interfaces),
            ("power_domain_ids", domains),
            ("connector_ids", connectors),
        ]:
            values_for_field = check_string_list(f"{prefix}.{field}", risk.get(field), result)
            for reference in values_for_field:
                if reference not in known:
                    result.issue(f"{prefix}.{field} references unknown id: {reference}")
                references.add(reference)
                kind_refs.setdefault(kind, set()).add(reference)
        if not references:
            result.issue(f"{prefix} must reference at least one block, interface, power domain, or connector")
        if not string_value(risk.get("reason")):
            result.issue(f"{prefix}.reason must explain why this path is risky")
        check_string_list(f"{prefix}.constraints", risk.get("constraints"), result, allow_empty=False)
        check_required_before(prefix, risk.get("required_before"), current_target, ranks, result)
        if identifier:
            risks[identifier] = risk
    return risks, kind_refs


def check_risk_coverage(
    domains: dict[str, dict[str, Any]],
    speed_classes: dict[str, str],
    external_interfaces: set[str],
    connector_risk_ids: set[str],
    kind_refs: dict[str, set[str]],
    policy: dict[str, Any],
    result: CheckResult,
) -> None:
    high_current = policy_set(policy, "high_current_classes")
    high_current_refs = kind_refs.get("high-current", set())
    for domain_id, domain in domains.items():
        if normalized(domain.get("current_class")) in high_current and domain_id not in high_current_refs:
            result.issue(f"high-current power domain is missing a high_current risk path: {domain_id}")

    speed_requirements = mapping_value(policy.get("speed_risk_kinds"))
    for interface_id, speed in speed_classes.items():
        required_kinds = {normalized(item) for item in list_value(speed_requirements.get(speed)) if string_value(item)}
        if required_kinds and not any(interface_id in kind_refs.get(kind, set()) for kind in required_kinds):
            result.issue(f"{speed} interface is missing a matching risk path: {interface_id}")

    external_kinds = policy_set(policy, "external_connector_risk_kinds")
    if policy.get("require_external_interface_risk_path") is True:
        for interface_id in sorted(external_interfaces):
            if not any(interface_id in kind_refs.get(kind, set()) for kind in external_kinds):
                result.issue(f"external interface is missing an external-cable risk path: {interface_id}")
    for connector_id in sorted(connector_risk_ids):
        if not any(connector_id in kind_refs.get(kind, set()) for kind in external_kinds):
            result.issue(f"exposed connector is missing an external-cable risk path: {connector_id}")


def check_protection_intents(
    architecture: dict[str, Any],
    boundaries: set[str],
    required_boundaries: set[str],
    current_target: str,
    ranks: dict[str, int],
    policy: dict[str, Any],
    result: CheckResult,
    unresolved: list[tuple[str, str]],
) -> dict[str, dict[str, Any]]:
    values = architecture.get("protection_intents")
    if not isinstance(values, list):
        result.issue("architecture.protection_intents must be a list")
        return {}
    identifiers: set[str] = set()
    protections: dict[str, dict[str, Any]] = {}
    covered: set[str] = set()
    for index, protection in enumerate(values):
        prefix = f"architecture.protection_intents[{index}]"
        if not isinstance(protection, dict):
            result.issue(f"{prefix} must be a mapping")
            continue
        check_required_fields(prefix, protection, policy_values(policy, "required_protection_intent_fields"), result)
        identifier = unique_identifier(prefix, protection, identifiers, result)
        boundary_id = str(protection.get("boundary_id", "")).strip()
        if boundary_id not in boundaries:
            result.issue(f"{prefix}.boundary_id references unknown interface, connector, or power domain: {boundary_id}")
        covered.add(boundary_id)
        threats = check_string_list(f"{prefix}.threats", protection.get("threats"), result)
        for threat in threats:
            if normalized(threat) not in policy_set(policy, "protection_threats"):
                result.issue(f"{prefix}.threats contains unsupported value: {threat}")
        disposition = check_allowed(
            f"{prefix}.disposition", protection.get("disposition"), policy_set(policy, "protection_intent_values"), result
        )
        strategies = check_string_list(f"{prefix}.strategy_classes", protection.get("strategy_classes"), result)
        for strategy in strategies:
            if normalized(strategy) not in policy_set(policy, "protection_strategy_classes"):
                result.issue(f"{prefix}.strategy_classes contains unsupported value: {strategy}")
        if disposition == "required" and (not threats or not strategies):
            result.issue(f"{prefix} requires at least one threat and strategy class when protection is required")
        if not string_value(protection.get("rationale")):
            result.issue(f"{prefix}.rationale must explain the protection disposition")
        required_before = check_required_before(prefix, protection.get("required_before"), current_target, ranks, result)
        if disposition in policy_set(policy, "unresolved_protection_values") and required_before:
            unresolved.append((f"{prefix}.disposition", required_before))
        if identifier:
            protections[identifier] = protection
    for boundary_id in sorted(required_boundaries - covered):
        result.issue(f"required protection intent has no protection strategy entry: {boundary_id}")
    return protections


def check_failure_states(
    architecture: dict[str, Any],
    block_map: dict[str, dict[str, Any]],
    current_target: str,
    ranks: dict[str, int],
    policy: dict[str, Any],
    result: CheckResult,
) -> dict[str, dict[str, Any]]:
    values = architecture.get("failure_states")
    if not isinstance(values, list):
        result.issue("architecture.failure_states must be a list")
        return {}
    minimum = int(policy.get("minimum_failure_states", 0))
    if len(values) < minimum:
        result.issue(f"architecture.failure_states must contain at least {minimum} item(s)")
    identifiers: set[str] = set()
    states: dict[str, dict[str, Any]] = {}
    for index, state in enumerate(values):
        prefix = f"architecture.failure_states[{index}]"
        if not isinstance(state, dict):
            result.issue(f"{prefix} must be a mapping")
            continue
        check_required_fields(prefix, state, policy_values(policy, "required_failure_state_fields"), result)
        identifier = unique_identifier(prefix, state, identifiers, result)
        for field in ["trigger", "safe_state"]:
            if not string_value(state.get(field)):
                result.issue(f"{prefix}.{field} must be a non-empty string")
        affected = check_string_list(f"{prefix}.affected_blocks", state.get("affected_blocks"), result, allow_empty=False)
        for block_id in affected:
            if block_id not in block_map:
                result.issue(f"{prefix}.affected_blocks references unknown block: {block_id}")
        check_required_before(prefix, state.get("required_before"), current_target, ranks, result)
        if identifier:
            states[identifier] = state
    return states


def check_test_and_debug(
    architecture: dict[str, Any],
    block_map: dict[str, dict[str, Any]],
    categories: dict[str, str],
    current_target: str,
    ranks: dict[str, int],
    policy: dict[str, Any],
    result: CheckResult,
    unresolved: list[tuple[str, str]],
) -> dict[str, dict[str, Any]]:
    values = architecture.get("test_and_debug")
    if not isinstance(values, list):
        result.issue("architecture.test_and_debug must be a list")
        return {}
    minimum = int(policy.get("minimum_test_debug_items", 0))
    if len(values) < minimum:
        result.issue(f"architecture.test_and_debug must contain at least {minimum} item(s)")
    identifiers: set[str] = set()
    items: dict[str, dict[str, Any]] = {}
    controller_blocks = {
        block_id for block_id, category in categories.items() if category in policy_set(policy, "controller_block_categories")
    }
    covered_controllers: set[str] = set()
    for index, item in enumerate(values):
        prefix = f"architecture.test_and_debug[{index}]"
        if not isinstance(item, dict):
            result.issue(f"{prefix} must be a mapping")
            continue
        check_required_fields(prefix, item, policy_values(policy, "required_test_debug_fields"), result)
        identifier = unique_identifier(prefix, item, identifiers, result)
        kind = check_allowed(f"{prefix}.kind", item.get("kind"), policy_set(policy, "test_debug_kinds"), result)
        targets = check_string_list(f"{prefix}.target_blocks", item.get("target_blocks"), result)
        for block_id in targets:
            if block_id not in block_map:
                result.issue(f"{prefix}.target_blocks references unknown block: {block_id}")
        disposition = check_allowed(
            f"{prefix}.disposition", item.get("disposition"), policy_set(policy, "test_debug_dispositions"), result
        )
        if not string_value(item.get("rationale")):
            result.issue(f"{prefix}.rationale must explain the test/debug disposition")
        required_before = check_required_before(prefix, item.get("required_before"), current_target, ranks, result)
        if disposition in policy_set(policy, "unresolved_test_debug_values") and required_before:
            unresolved.append((f"{prefix}.disposition", required_before))
        if kind in policy_set(policy, "required_controller_test_kinds") and disposition == "required":
            covered_controllers.update(controller_blocks & set(targets))
        if identifier:
            items[identifier] = item
    for block_id in sorted(controller_blocks - covered_controllers):
        result.issue(f"controller block has no required programming/debug/recovery architecture path: {block_id}")
    return items


def check_requirement_coverage(
    architecture: dict[str, Any],
    report: dict[str, Any],
    block_map: dict[str, dict[str, Any]],
    policy: dict[str, Any],
    result: CheckResult,
) -> None:
    values = architecture.get("requirement_coverage")
    if not isinstance(values, list):
        result.issue("architecture.requirement_coverage must be a list")
        return
    minimum = int(policy.get("minimum_requirement_coverage", 1))
    if len(values) < minimum:
        result.issue(f"architecture.requirement_coverage must contain at least {minimum} item(s)")
    covered_paths: set[str] = set()
    for index, coverage in enumerate(values):
        prefix = f"architecture.requirement_coverage[{index}]"
        if not isinstance(coverage, dict):
            result.issue(f"{prefix} must be a mapping")
            continue
        check_required_fields(prefix, coverage, policy_values(policy, "required_requirement_coverage_fields"), result)
        path = str(coverage.get("requirement_path", "")).strip()
        covered_paths.add(path)
        if report and get_path(report, path) is None:
            result.issue(f"{prefix}.requirement_path does not exist in the intake report: {path}")
        block_ids = check_string_list(f"{prefix}.block_ids", coverage.get("block_ids"), result, allow_empty=False)
        for block_id in block_ids:
            if block_id not in block_map:
                result.issue(f"{prefix}.block_ids references unknown block: {block_id}")
        if not string_value(coverage.get("rationale")):
            result.issue(f"{prefix}.rationale must explain the architecture coverage")
    if report:
        required_paths = set(policy_values(policy, "required_intake_coverage_paths"))
        required_paths.update(
            path
            for path in policy_values(policy, "conditional_intake_coverage_paths")
            if get_path(report, path) is not None
        )
        for path in sorted(required_paths - covered_paths):
            result.issue(f"architecture.requirement_coverage missing confirmed intake path: {path}")


def check_hazard_coverage(
    architecture: dict[str, Any],
    report: dict[str, Any],
    control_ids: set[str],
    current_target: str,
    ranks: dict[str, int],
    policy: dict[str, Any],
    result: CheckResult,
) -> None:
    values = architecture.get("hazard_coverage")
    if not isinstance(values, list):
        result.issue("architecture.hazard_coverage must be a list")
        return
    covered: set[str] = set()
    for index, item in enumerate(values):
        prefix = f"architecture.hazard_coverage[{index}]"
        if not isinstance(item, dict):
            result.issue(f"{prefix} must be a mapping")
            continue
        check_required_fields(prefix, item, policy_values(policy, "required_hazard_coverage_fields"), result)
        hazard = str(item.get("hazard", "")).strip()
        covered.add(normalized(hazard))
        controls = check_string_list(f"{prefix}.control_ids", item.get("control_ids"), result, allow_empty=False)
        for control_id in controls:
            if control_id not in control_ids:
                result.issue(f"{prefix}.control_ids references unknown risk/protection/failure control: {control_id}")
        if not string_value(item.get("rationale")):
            result.issue(f"{prefix}.rationale must explain hazard coverage")
        check_required_before(prefix, item.get("required_before"), current_target, ranks, result)
    hazards = list_value(get_path(report, "safety_screening.hazards")) if report else []
    for hazard in hazards:
        if string_value(hazard) and normalized(hazard) not in covered:
            result.issue(f"architecture.hazard_coverage missing intake hazard: {hazard}")


def beginner_mode(spec: dict[str, Any], report: dict[str, Any], policy: dict[str, Any]) -> bool:
    experience = normalized(get_path(spec, "user_profile.experience"))
    input_style = normalized(get_path(spec, "user_profile.input_style"))
    if experience in policy_set(policy, "beginner_experience_values"):
        return True
    if input_style in policy_set(policy, "beginner_input_styles"):
        return True
    return normalized(get_path(report, "intake.input_style")) in policy_set(policy, "beginner_input_styles")


def check_open_decisions(
    architecture: dict[str, Any],
    current_target: str,
    ranks: dict[str, int],
    beginner: bool,
    policy: dict[str, Any],
    result: CheckResult,
) -> set[str]:
    values = architecture.get("open_decisions")
    if not isinstance(values, list):
        result.issue("architecture.open_decisions must be a list")
        return set()
    identifiers: set[str] = set()
    future_block_stages: set[str] = set()
    unresolved_statuses = policy_set(policy, "unresolved_open_decision_statuses")
    for index, item in enumerate(values):
        prefix = f"architecture.open_decisions[{index}]"
        if not isinstance(item, dict):
            result.issue(f"{prefix} must be a mapping")
            continue
        check_required_fields(prefix, item, policy_values(policy, "required_open_decision_fields"), result)
        unique_identifier(prefix, item, identifiers, result)
        kind = check_allowed(f"{prefix}.kind", item.get("kind"), policy_set(policy, "open_decision_kinds"), result)
        owner = check_allowed(f"{prefix}.owner", item.get("owner"), policy_set(policy, "open_decision_owners"), result)
        status = check_allowed(f"{prefix}.status", item.get("status"), policy_set(policy, "open_decision_statuses"), result)
        blocks = normalized(item.get("blocks"))
        if blocks not in ranks:
            result.issue(f"{prefix}.blocks must be one of {', '.join(sorted(ranks))}; got {item.get('blocks')}")
        if not string_value(item.get("description")) or not string_value(item.get("next_action")):
            result.issue(f"{prefix}.description and next_action must be non-empty strings")
        if beginner and kind == "technical" and owner in policy_set(policy, "forbidden_beginner_technical_owner_values"):
            result.issue(f"{prefix} assigns a professional technical decision to a beginner user")
        if status in unresolved_statuses and blocks in ranks:
            future_block_stages.add(blocks)
            if unresolved_is_blocking(current_target, blocks, ranks):
                result.issue(f"{prefix} remains unresolved but blocks current architecture target {current_target}")
    return future_block_stages


def check_deferred_unknowns(
    unresolved: list[tuple[str, str]],
    current_target: str,
    ranks: dict[str, int],
    open_block_stages: set[str],
    result: CheckResult,
) -> None:
    for label, required_before in unresolved:
        if unresolved_is_blocking(current_target, required_before, ranks):
            result.issue(f"{label} is unresolved but is required before current target {current_target}")
        elif required_before not in open_block_stages:
            result.issue(f"{label} is deferred until {required_before} but has no matching open_decision blocker")


def check_source_revision(
    architecture: dict[str, Any], report: dict[str, Any], policy: dict[str, Any], result: CheckResult
) -> None:
    revision = int_value(architecture.get("revision"))
    source_revision = int_value(architecture.get("source_intake_revision"))
    if revision is None or revision < 1:
        result.issue("architecture.revision must be a positive integer")
    if source_revision is None or source_revision < 0:
        result.issue("architecture.source_intake_revision must be a non-negative integer")
    if not report:
        return
    confirmation = mapping_value(report.get("confirmation"))
    if normalized(confirmation.get("status")) not in policy_set(policy, "confirmed_intake_statuses"):
        result.issue("architecture cannot advance from an unconfirmed intake")
        return
    if policy.get("require_intake_user_confirmation") is True:
        confirmed_by = normalized(confirmation.get("confirmed_by"))
        if confirmed_by not in policy_set(policy, "intake_confirmation_user_values"):
            result.issue("architecture requires intake confirmation explicitly attributed to the user")
    confirmed_revision = int_value(confirmation.get("confirmed_revision"))
    if confirmed_revision is None or source_revision != confirmed_revision:
        result.issue(
            "architecture.source_intake_revision must equal requirement_intake.confirmation.confirmed_revision"
        )


def check_practical_choice_confirmation(
    architecture: dict[str, Any], policy: dict[str, Any], result: CheckResult
) -> None:
    confirmation = architecture.get("practical_choice_confirmation")
    if not isinstance(confirmation, dict):
        result.issue("architecture.practical_choice_confirmation must record explicit user confirmation")
        return
    prefix = "architecture.practical_choice_confirmation"
    check_required_fields(prefix, confirmation, policy_values(policy, "required_practical_confirmation_fields"), result)
    check_allowed(
        f"{prefix}.status",
        confirmation.get("status"),
        policy_set(policy, "practical_confirmation_statuses"),
        result,
    )
    check_allowed(
        f"{prefix}.confirmed_by",
        confirmation.get("confirmed_by"),
        policy_set(policy, "practical_confirmation_user_values"),
        result,
    )
    architecture_revision = int_value(architecture.get("revision"))
    confirmed_architecture_revision = int_value(confirmation.get("architecture_revision"))
    if confirmed_architecture_revision is None or confirmed_architecture_revision != architecture_revision:
        result.issue(f"{prefix}.architecture_revision must match architecture.revision")
    source_revision = int_value(architecture.get("source_intake_revision"))
    confirmed_source_revision = int_value(confirmation.get("source_intake_revision"))
    if confirmed_source_revision is None or confirmed_source_revision != source_revision:
        result.issue(f"{prefix}.source_intake_revision must match architecture.source_intake_revision")
    expected_digest = architecture_confirmation_digest(architecture, policy)
    if normalized(confirmation.get("architecture_sha256")) != expected_digest:
        result.issue(f"{prefix}.architecture_sha256 is stale; expected {expected_digest}")
    if not string_value(confirmation.get("user_response_summary")):
        result.issue(f"{prefix}.user_response_summary must record what the user confirmed")


def check_minimum_target(
    spec: dict[str, Any],
    report: dict[str, Any],
    current_target: str,
    ranks: dict[str, int],
    policy: dict[str, Any],
    result: CheckResult,
) -> None:
    required_targets: list[tuple[str, str]] = []
    stage_map = {
        normalized(key): normalized(value)
        for key, value in mapping_value(policy.get("project_stage_target_map")).items()
    }
    project_stage = normalized(get_path(spec, "project.stage"))
    if project_stage in stage_map:
        required_targets.append((f"project.stage {project_stage}", stage_map[project_stage]))
    intake_target = normalized(get_path(report, "decision.current_target")) if report else ""
    if intake_target in ranks:
        required_targets.append(("confirmed intake decision", intake_target))
    for source, required_target in required_targets:
        if current_target in ranks and required_target in ranks and ranks[current_target] < ranks[required_target]:
            result.issue(
                f"architecture.current_target {current_target} is below {required_target} required by {source}"
            )


def check_report_artifact(
    spec: dict[str, Any],
    architecture: dict[str, Any],
    outputs: dict[str, Any],
    policy: dict[str, Any],
    before_sourcing: bool,
    before_generation: bool,
    result: CheckResult,
    spec_path: Path | None = None,
) -> None:
    required = (before_sourcing and policy.get("require_report_before_sourcing") is True) or (
        before_generation and policy.get("require_report_before_generation") is True
    )
    if not required:
        return
    try:
        path = architecture_report_path(spec, policy, spec_path)
    except ValueError as error:
        result.issue(str(error))
        return
    if not path.is_file() or path.stat().st_size <= 0:
        result.issue(f"architecture report is missing or empty: {path}")
        return
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        result.issue(f"cannot read architecture report {path}: {error}")
        return
    markers = [
        f"- Architecture revision: {architecture.get('revision')}",
        f"- Source intake revision: {architecture.get('source_intake_revision')}",
        f"- State: {architecture.get('state')}",
        f"- Confirmation SHA256: {architecture_confirmation_digest(architecture, policy)}",
        f"- Architecture SHA256: {architecture_digest(architecture)}",
    ]
    for marker in markers:
        if marker not in content:
            result.issue(f"architecture report is stale; missing marker: {marker}")


def check_architecture(
    spec: dict[str, Any],
    result: CheckResult,
    force: bool = False,
    before_sourcing: bool = False,
    before_generation: bool = False,
    spec_path: Path | None = None,
) -> dict[str, Any]:
    policy = load_policy(spec)
    enabled, reasons = architecture_required(spec, policy, force=force)
    details: dict[str, Any] = {
        "enabled": enabled,
        "policy": str(policy_path(spec)),
        "reasons": reasons,
        "before_sourcing": before_sourcing,
        "before_generation": before_generation,
    }
    if string_value(get_path(spec, "validation.architecture.policy_file")):
        protected = policy_set(policy, "protected_policy_override_targets")
        stage = normalized(get_path(spec, "project.stage"))
        stage_target = normalized(mapping_value(policy.get("project_stage_target_map")).get(stage))
        declared_targets = {
            normalized(get_path(spec, "architecture.current_target")),
            normalized(get_path(spec, "requirement_intake.decision.current_target")),
            stage_target,
        }
        if protected & declared_targets:
            result.issue("production architecture policy override is forbidden; use the bundled trusted policy")
    if not enabled:
        result.warning("architecture gate not required for this legacy/simple spec")
        return details

    if architecture_config(spec).get("enabled") is False:
        result.issue("validation.architecture.enabled cannot disable architecture for a required workflow state")

    architecture = spec.get("architecture")
    if not isinstance(architecture, dict):
        result.issue("architecture section is required before sourcing or KiCad generation")
        return details
    check_required_fields("architecture", architecture, policy_values(policy, "required_architecture_fields"), result)
    for path in find_forbidden_keys(architecture, policy_set(policy, "forbidden_architecture_keys")):
        result.issue(f"block-level architecture contains detailed-design key: {path}")

    report = intake_report(spec)
    check_source_revision(architecture, report, policy, result)
    current_target = normalized(architecture.get("current_target"))
    allowed_targets = policy_set(policy, "allowed_current_targets")
    check_allowed("architecture.current_target", architecture.get("current_target"), allowed_targets, result)
    ranks = stage_ranks(policy)
    check_minimum_target(spec, report, current_target, ranks, policy, result)
    validation_target = phase_validation_target(current_target, before_sourcing, before_generation, policy, ranks)
    phase = "before-generation" if before_generation else "before-sourcing" if before_sourcing else "architecture"
    state = check_allowed("architecture.state", architecture.get("state"), policy_set(policy, "architecture_states"), result)
    if not string_value(architecture.get("summary")):
        result.issue("architecture.summary must be a non-empty plain-language summary")
    if not isinstance(architecture.get("practical_choices_confirmed"), bool):
        result.issue("architecture.practical_choices_confirmed must be true or false")
    check_allowed(
        "architecture.technical_decision_owner",
        architecture.get("technical_decision_owner"),
        policy_set(policy, "technical_decision_owners"),
        result,
    )
    outputs = mapping_value(architecture.get("outputs"))
    if not outputs:
        result.issue("architecture.outputs must be a mapping")
    check_required_fields("architecture.outputs", outputs, policy_values(policy, "required_output_fields"), result)

    if before_sourcing and state not in policy_set(policy, "ready_states_before_sourcing"):
        result.issue(f"architecture.state {state or '<missing>'} is not ready for component sourcing")
    if before_generation and state not in policy_set(policy, "ready_states_before_generation"):
        result.issue(f"architecture.state {state or '<missing>'} is not ready for KiCad generation")
    if (before_sourcing or before_generation) and architecture.get("practical_choices_confirmed") is not True:
        result.issue("architecture practical choices must be confirmed before sourcing or KiCad generation")
    if before_sourcing or before_generation:
        check_practical_choice_confirmation(architecture, policy, result)

    unresolved: list[tuple[str, str]] = []
    blocks, categories = check_blocks(architecture, policy, result)
    selection_constraints = check_selection_constraints(
        blocks, categories, validation_target, ranks, phase, policy, result
    )
    edges = check_edges(architecture, blocks, categories, policy, result)
    domains, domain_protection = check_power_domains(
        architecture, blocks, edges, validation_target, ranks, policy, result, unresolved
    )
    interfaces, external_interfaces, speed_classes = check_interfaces(
        architecture, blocks, domains, edges, validation_target, ranks, policy, result, unresolved
    )
    check_boundary_interfaces(blocks, edges, interfaces, policy, result)
    connectors, connector_protection, connector_risk_ids = check_external_connectors(
        architecture,
        blocks,
        categories,
        interfaces,
        external_interfaces,
        validation_target,
        ranks,
        policy,
        result,
        unresolved,
    )
    risks, risk_refs = check_risk_paths(
        architecture, blocks, interfaces, domains, connectors, validation_target, ranks, policy, result
    )
    check_risk_coverage(domains, speed_classes, external_interfaces, connector_risk_ids, risk_refs, policy, result)
    boundaries = set(domains) | set(interfaces) | set(connectors)
    protections = check_protection_intents(
        architecture,
        boundaries,
        domain_protection | connector_protection,
        validation_target,
        ranks,
        policy,
        result,
        unresolved,
    )
    failures = check_failure_states(architecture, blocks, validation_target, ranks, policy, result)
    test_items = check_test_and_debug(
        architecture, blocks, categories, validation_target, ranks, policy, result, unresolved
    )
    check_requirement_coverage(architecture, report, blocks, policy, result)
    check_hazard_coverage(
        architecture,
        report,
        set(risks) | set(protections) | set(failures),
        validation_target,
        ranks,
        policy,
        result,
    )
    open_stages = check_open_decisions(
        architecture, validation_target, ranks, beginner_mode(spec, report, policy), policy, result
    )
    check_deferred_unknowns(unresolved, validation_target, ranks, open_stages, result)
    if result.ok():
        check_report_artifact(
            spec,
            architecture,
            outputs,
            policy,
            before_sourcing,
            before_generation,
            result,
            spec_path,
        )

    details.update(
        {
            "state": state,
            "current_target": current_target,
            "validation_target": validation_target,
            "blocks": len(blocks),
            "selection_constraints": len(selection_constraints),
            "edges": len(edges),
            "power_domains": len(domains),
            "interfaces": len(interfaces),
            "external_connectors": len(connectors),
            "risk_paths": len(risks),
            "protection_intents": len(protections),
            "failure_states": len(failures),
            "test_and_debug": len(test_items),
            "deferred_unknowns": len(unresolved),
        }
    )
    return details


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Validate block-level PCB architecture before sourcing or generation.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--require", action="store_true")
    parser.add_argument("--before-sourcing", action="store_true")
    parser.add_argument("--before-generation", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])

    result = CheckResult()
    details: dict[str, Any] = {}
    try:
        details = check_architecture(
            load_spec(args.spec),
            result,
            force=args.require,
            before_sourcing=args.before_sourcing,
            before_generation=args.before_generation,
            spec_path=args.spec,
        )
    except Exception as error:
        result.issue(str(error))

    if args.json_output:
        print(
            json.dumps(
                {
                    "check": "architecture_gate",
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
            "architecture_gate state: "
            f"enabled={details.get('enabled')} "
            f"state={details.get('state') or '<none>'} "
            f"target={details.get('current_target') or '<none>'} "
            f"blocks={details.get('blocks', 0)}"
        )
    return print_result("architecture_gate", result, False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
