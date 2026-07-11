#!/usr/bin/env python3
"""Validate and bind the schematic-only stage before PCB generation."""

from __future__ import annotations

import copy
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from _kicad_sexpr import symbol_pin_numbers, symbol_unit_numbers
from _part_lock import durable_write
from _pcb_skill_checks import (
    CheckResult,
    actual_net_graph,
    check_generated_netlist,
    check_net_graph,
    check_schema,
    component_map,
    get_path,
    load_yaml,
    net_classes,
    net_names,
    pad_net,
    parse_pin,
    pin_id,
    sha256_file,
    string_value,
)
from _spec_freeze import load_policy as load_freeze_policy
from _spec_freeze import spec_digest


def mapping_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def string_list(value: Any) -> list[str]:
    return [str(item) for item in list_value(value) if string_value(item)]


def normalized(value: Any) -> str:
    return str(value).strip().lower().replace("_", "-") if value is not None else ""


def policy_path() -> Path:
    return Path(__file__).resolve().parents[1] / "assets" / "schematic-stage-policy.yaml"


def load_policy() -> dict[str, Any]:
    return load_yaml(policy_path())


def dotted_exists(data: dict[str, Any], path: str) -> bool:
    value = get_path(data, path)
    return value not in (None, "", [], {})


def production_required(spec: dict[str, Any], policy: dict[str, Any], forced: bool = False) -> bool:
    if forced:
        return True
    stage = normalized(get_path(spec, "project.stage"))
    stages = {normalized(item) for item in string_list(policy.get("production_stage_values"))}
    return stage in stages or any(dotted_exists(spec, path) for path in string_list(policy.get("production_signals")))


def stage_required(spec: dict[str, Any], policy: dict[str, Any], forced: bool = False) -> bool:
    if forced or production_required(spec, policy):
        return True
    field = str(policy.get("force_required_field", ""))
    if field and get_path(spec, field) is True:
        return True
    stage = normalized(get_path(spec, "project.stage"))
    return stage in {normalized(item) for item in string_list(policy.get("required_stage_values"))}


def project_root(spec: dict[str, Any], spec_path: Path, policy: dict[str, Any]) -> Path:
    field = str(get_path(policy, "paths.project_root_field"))
    configured = get_path(spec, field) if field else None
    if string_value(configured):
        raw = Path(str(configured))
        return raw.resolve() if raw.is_absolute() else (spec_path.resolve().parent / raw).resolve()
    return Path.cwd().resolve()


def resolve_from_root(value: Any, root: Path) -> Path:
    raw = Path(str(value))
    return raw.resolve() if raw.is_absolute() else (root / raw).resolve()


def display_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def configured_root(spec: dict[str, Any], root: Path, policy: dict[str, Any], key: str, result: CheckResult) -> Path:
    field = str(get_path(policy, f"paths.{key}"))
    value = get_path(spec, field) if field else None
    if not string_value(value):
        result.issue(f"Schematic stage policy path {key} points to missing Spec field: {field}")
        return root
    return resolve_from_root(value, root)


def artifact_paths(
    spec: dict[str, Any], spec_path: Path, policy: dict[str, Any], result: CheckResult
) -> dict[str, Path]:
    root = project_root(spec, spec_path, policy)
    output = configured_root(spec, root, policy, "output_dir_field", result)
    artifacts = configured_root(spec, root, policy, "artifacts_dir_field", result)
    project_name = str(get_path(spec, "project.name") or spec_path.stem)
    report_dir = artifacts / str(get_path(policy, "paths.manifest_directory")) / project_name
    return {
        "root": root,
        "output": output,
        "artifacts": artifacts,
        "schematic": output / f"{project_name}.kicad_sch",
        "manifest": report_dir / str(get_path(policy, "paths.manifest_filename")),
        "erc": report_dir / str(get_path(policy, "paths.erc_report_filename")),
        "netlist": report_dir / str(get_path(policy, "paths.generated_netlist_filename")),
        "net_graph": report_dir / str(get_path(policy, "paths.generated_net_graph_filename")),
        "review_pdf": report_dir / str(get_path(policy, "paths.review_pdf_filename")),
    }


def split_library_id(value: Any) -> tuple[str, str] | None:
    if not string_value(value) or ":" not in str(value):
        return None
    library, symbol = str(value).split(":", 1)
    return (library, symbol) if library and symbol else None


def library_entry_path(entry: Any) -> Any:
    return entry.get("path") if isinstance(entry, dict) else entry


def symbol_library_path(spec: dict[str, Any], spec_path: Path, policy: dict[str, Any], library: str) -> Path:
    root = project_root(spec, spec_path, policy)
    libraries = mapping_value(get_path(spec, "kicad.symbol_libraries"))
    if library in libraries and string_value(library_entry_path(libraries[library])):
        return resolve_from_root(library_entry_path(libraries[library]), root)
    symbol_root = get_path(spec, "kicad.symbol_root")
    if not string_value(symbol_root):
        raise ValueError(f"No symbol library mapping or symbol_root for: {library}")
    return resolve_from_root(symbol_root, root) / f"{library}.kicad_sym"


def no_connect_map(spec: dict[str, Any], policy: dict[str, Any], result: CheckResult) -> dict[str, set[str]]:
    config = mapping_value(policy.get("pin_contract"))
    path = str(config.get("no_connects_path", ""))
    ref_field = str(config.get("no_connect_ref_field", ""))
    pins_field = str(config.get("no_connect_pins_field", ""))
    entries = get_path(spec, path)
    if entries in (None, []):
        return {}
    if not isinstance(entries, list):
        result.issue(f"{path} must be a list")
        return {}
    components = component_map(spec)
    mapped: dict[str, set[str]] = {}
    for index, entry in enumerate(entries):
        label = f"{path}[{index}]"
        if not isinstance(entry, dict):
            result.issue(f"{label} must be a mapping")
            continue
        ref = entry.get(ref_field)
        pins = entry.get(pins_field)
        if not string_value(ref) or str(ref) not in components:
            result.issue(f"{label}.{ref_field} references an unknown component: {ref}")
            continue
        if not isinstance(pins, list) or not pins:
            result.issue(f"{label}.{pins_field} must be a non-empty list")
            continue
        target = mapped.setdefault(str(ref), set())
        for pin in pins:
            if not string_value(pin):
                result.issue(f"{label}.{pins_field} contains an empty pin")
            elif str(pin) in target:
                result.issue(f"{label} duplicates no-connect pin {ref}.{pin}")
            else:
                target.add(str(pin))
    return mapped


def check_pin_contract(
    spec: dict[str, Any], spec_path: Path, policy: dict[str, Any], result: CheckResult, required: bool
) -> None:
    config = mapping_value(policy.get("pin_contract"))
    no_connects = no_connect_map(spec, policy, result)
    supported_units = config.get("supported_symbol_unit_count")
    cache: dict[str, tuple[set[str], list[int]]] = {}
    for component in list_value(spec.get("components")):
        if not isinstance(component, dict) or not string_value(component.get("ref")):
            continue
        ref = str(component["ref"])
        parsed = split_library_id(component.get("symbol"))
        if parsed is None:
            continue
        library, symbol = parsed
        library_id = str(component["symbol"])
        try:
            if library_id not in cache:
                path = symbol_library_path(spec, spec_path, policy, library)
                cache[library_id] = (
                    set(symbol_pin_numbers(path, symbol)),
                    symbol_unit_numbers(path, symbol),
                )
            symbol_pins, units = cache[library_id]
        except (OSError, ValueError) as error:
            result.issue(f"Cannot inspect symbol pins for {ref} ({library_id}): {error}")
            continue
        connected = {str(pin) for pin in mapping_value(component.get("pads"))}
        disconnected = no_connects.get(ref, set())
        unknown_connected = sorted(connected - symbol_pins)
        unknown_disconnected = sorted(disconnected - symbol_pins)
        overlap = sorted(connected & disconnected)
        missing = sorted(symbol_pins - connected - disconnected)
        if unknown_connected and config.get("fail_on_unknown_declared_pin") is True:
            result.issue(f"{ref} connects pins absent from {library_id}: {', '.join(unknown_connected)}")
        if unknown_disconnected:
            result.issue(f"{ref} marks absent pins as no-connect for {library_id}: {', '.join(unknown_disconnected)}")
        if overlap and config.get("fail_on_connected_and_no_connect_overlap") is True:
            result.issue(f"{ref} pins are both connected and no-connect: {', '.join(overlap)}")
        if required and config.get("require_complete_coverage_when_required") is True and missing:
            result.issue(f"{ref} symbol pins lack connected/no-connect disposition: {', '.join(missing)}")
        if isinstance(supported_units, int) and len(units) > supported_units:
            message = f"{ref} uses {len(units)} symbol units {units}; the generator supports {supported_units}"
            if required:
                result.issue(message)
            else:
                result.warning(message)


def expected_rules(spec: dict[str, Any]) -> dict[str, Any]:
    configured = spec.get("expected_net_graph")
    if not isinstance(configured, dict):
        configured = get_path(spec, "validation.expected_net_graph")
    if not isinstance(configured, dict):
        return {}
    nets = configured.get("nets", configured)
    return nets if isinstance(nets, dict) else {}


def rule_pins(rule: dict[str, Any], fields: list[str]) -> set[str]:
    pins: set[str] = set()
    for field in fields:
        value = rule.get(field)
        if isinstance(value, list):
            pins.update(str(item) for item in value if string_value(item))
        elif string_value(value):
            pins.add(str(value))
    return pins


def check_pin_net(spec: dict[str, Any], raw_pin: Any, expected_net: str, result: CheckResult, label: str) -> None:
    parsed = parse_pin(raw_pin)
    if parsed is None:
        result.issue(f"{label} contains invalid pin: {raw_pin}")
        return
    actual = pad_net(spec, parsed[0], parsed[1])
    if actual != expected_net:
        result.issue(f"{label} pin {pin_id(parsed[0], parsed[1])} maps to {actual}, expected {expected_net}")


def check_net_roles(spec: dict[str, Any], policy: dict[str, Any], result: CheckResult, required: bool) -> None:
    config = mapping_value(policy.get("net_roles"))
    classes = net_classes(spec)
    rules = expected_rules(spec)
    power_classes = {normalized(item) for item in string_list(config.get("power_classes"))}
    power_class_patterns = [re.compile(str(item), re.IGNORECASE) for item in string_list(config.get("power_class_patterns"))]
    allowed_roles = {normalized(item) for item in string_list(config.get("allowed_roles"))}
    supply_role = normalized(config.get("supply_role"))
    return_role = normalized(config.get("return_role"))
    source_fields = string_list(config.get("source_fields"))
    sink_field = str(config.get("sink_field", ""))
    return_field = str(config.get("return_field", ""))
    for net, net_class in classes.items():
        normalized_class = normalized(net_class)
        if normalized_class not in power_classes and not any(pattern.fullmatch(normalized_class) for pattern in power_class_patterns):
            continue
        rule = rules.get(net)
        if not isinstance(rule, dict):
            if required and config.get("require_for_power_classes_when_required") is True:
                result.issue(f"Power-class net {net} requires an expected_net_graph rule with electrical role")
            continue
        role = normalized(rule.get("role"))
        if role not in allowed_roles:
            result.issue(f"expected_net_graph.{net}.role must be one of {', '.join(sorted(allowed_roles))}")
            continue
        expected = rule_pins(rule, ["required_pins", "pins"])
        if role == supply_role:
            sources = rule_pins(rule, source_fields)
            sinks = rule_pins(rule, [sink_field])
            if not sources:
                result.issue(f"Supply net {net} must declare source pin(s)")
            if not sinks:
                result.issue(f"Supply net {net} must declare sink_pins")
            if sources & sinks:
                result.issue(f"Supply net {net} pins cannot be both source and sink: {', '.join(sorted(sources & sinks))}")
            if expected and sources | sinks != expected:
                missing_roles = sorted(expected - sources - sinks)
                extra_roles = sorted((sources | sinks) - expected)
                if missing_roles:
                    result.issue(f"Supply net {net} has pins without source/sink role: {', '.join(missing_roles)}")
                if extra_roles:
                    result.issue(f"Supply net {net} role pins are outside expected pins: {', '.join(extra_roles)}")
            for pin in sorted(sources | sinks):
                if expected and pin not in expected:
                    result.issue(f"Supply role pin is absent from expected pins for {net}: {pin}")
                check_pin_net(spec, pin, net, result, f"expected_net_graph.{net}")
        elif role == return_role:
            returns = rule_pins(rule, [return_field])
            if len(returns) < 2:
                result.issue(f"Return net {net} must declare at least two return_pins")
            if expected and returns != expected:
                missing_returns = sorted(expected - returns)
                extra_returns = sorted(returns - expected)
                if missing_returns:
                    result.issue(f"Return net {net} has pins without return role: {', '.join(missing_returns)}")
                if extra_returns:
                    result.issue(f"Return net {net} role pins are outside expected pins: {', '.join(extra_returns)}")
            for pin in sorted(returns):
                if expected and pin not in expected:
                    result.issue(f"Return role pin is absent from expected pins for {net}: {pin}")
                check_pin_net(spec, pin, net, result, f"expected_net_graph.{net}")


def check_power_flags(spec: dict[str, Any], policy: dict[str, Any], result: CheckResult, required: bool) -> None:
    config = mapping_value(policy.get("power_flags"))
    path = str(config.get("path", ""))
    flags = get_path(spec, path)
    if flags in (None, []):
        return
    if not isinstance(flags, list):
        result.issue(f"{path} must be a list")
        return
    net_field = str(config.get("net_field", ""))
    source_field = str(config.get("source_pins_field", ""))
    rationale_field = str(config.get("rationale_field", ""))
    declared_nets = net_names(spec)
    for index, flag in enumerate(flags):
        label = f"{path}[{index}]"
        if not isinstance(flag, dict):
            result.issue(f"{label} must be a mapping")
            continue
        net = flag.get(net_field)
        if not string_value(net) or str(net) not in declared_nets:
            result.issue(f"{label}.{net_field} references an unknown net: {net}")
            continue
        for field in string_list(config.get("required_fields")):
            if flag.get(field) in (None, "", [], {}):
                result.issue(f"{label}.{field} is required")
        if required and config.get("require_source_binding_when_required") is True:
            sources = flag.get(source_field)
            if not isinstance(sources, list) or not sources:
                result.issue(f"{label}.{source_field} must bind the flag to physical source pin(s)")
            else:
                for pin in sources:
                    check_pin_net(spec, pin, str(net), result, label)
            if not string_value(flag.get(rationale_field)):
                result.issue(f"{label}.{rationale_field} must justify the ERC power source assertion")


def resolved_pin_net(spec: dict[str, Any], raw_pin: Any, result: CheckResult, label: str) -> tuple[str, str] | None:
    parsed = parse_pin(raw_pin)
    if parsed is None:
        result.issue(f"{label} contains invalid pin: {raw_pin}")
        return None
    identifier = pin_id(parsed[0], parsed[1])
    net = pad_net(spec, parsed[0], parsed[1])
    if net is None:
        result.issue(f"{label} references an unconnected or unknown pin: {identifier}")
        return None
    return identifier, net


def check_path_assertions(
    spec: dict[str, Any], policy: dict[str, Any], result: CheckResult, production: bool
) -> None:
    config = mapping_value(policy.get("path_assertions"))
    path = str(config.get("path", ""))
    assertions = get_path(spec, path)
    if assertions in (None, []):
        assertions = []
    if not isinstance(assertions, list):
        result.issue(f"{path} must be a list")
        return
    allowed = {normalized(item) for item in string_list(config.get("allowed_kinds"))}
    coverage_field = str(config.get("coverage_field", ""))
    boundary_field = str(config.get("boundary_field", ""))
    covered: set[str] = set()
    coverage_boundaries: dict[str, set[str]] = {}
    ids: set[str] = set()
    for index, assertion in enumerate(assertions):
        label = f"{path}[{index}]"
        if not isinstance(assertion, dict):
            result.issue(f"{label} must be a mapping")
            continue
        assertion_id = assertion.get("id")
        if not string_value(assertion_id) or str(assertion_id) in ids:
            result.issue(f"{label}.id must be non-empty and unique")
        else:
            ids.add(str(assertion_id))
        covers = assertion.get(coverage_field, [])
        boundaries = set(string_list(assertion.get(boundary_field)))
        if isinstance(covers, list):
            for covered_id in (str(item) for item in covers if string_value(item)):
                covered.add(covered_id)
                coverage_boundaries.setdefault(covered_id, set()).update(boundaries)
        if production and (not isinstance(covers, list) or not covers):
            result.issue(f"{label}.{coverage_field} must identify covered production intent(s)")
        if production and not boundaries:
            result.issue(f"{label}.{boundary_field} must identify the protected architecture boundary")
        kind = normalized(assertion.get("kind"))
        if kind not in allowed:
            result.issue(f"{label}.kind must be one of {', '.join(sorted(allowed))}")
            continue
        if kind == "series":
            series = mapping_value(config.get("series"))
            source = resolved_pin_net(spec, assertion.get(str(series.get("source_pin_field"))), result, label)
            sink = resolved_pin_net(spec, assertion.get(str(series.get("sink_pin_field"))), result, label)
            through = assertion.get(str(series.get("through_field")))
            if not isinstance(through, list) or not through:
                result.issue(f"{label} series assertion must declare at least one through component")
                continue
            if source is None or sink is None:
                continue
            current_net = source[1]
            for item_index, item in enumerate(through):
                item_label = f"{label}.through[{item_index}]"
                if not isinstance(item, dict):
                    result.issue(f"{item_label} must be a mapping")
                    continue
                ref = str(item.get(str(series.get("ref_field")), ""))
                input_pin = resolved_pin_net(
                    spec, {"ref": ref, "pad": item.get(str(series.get("input_pin_field")))}, result, item_label
                )
                output_pin = resolved_pin_net(
                    spec, {"ref": ref, "pad": item.get(str(series.get("output_pin_field")))}, result, item_label
                )
                if input_pin is None or output_pin is None:
                    continue
                if input_pin[1] != current_net:
                    result.issue(f"{item_label} input net {input_pin[1]} does not continue {current_net}")
                if output_pin[1] == input_pin[1]:
                    result.issue(f"{item_label} does not create a series boundary; both pins use {input_pin[1]}")
                current_net = output_pin[1]
            if sink[1] != current_net:
                result.issue(f"{label} sink net {sink[1]} does not follow final series net {current_net}")
        elif kind == "shunt":
            shunt = mapping_value(config.get("shunt"))
            ref = str(assertion.get(str(shunt.get("ref_field")), ""))
            line = resolved_pin_net(
                spec, {"ref": ref, "pad": assertion.get(str(shunt.get("line_pin_field")))}, result, label
            )
            return_pin = resolved_pin_net(
                spec, {"ref": ref, "pad": assertion.get(str(shunt.get("return_pin_field")))}, result, label
            )
            if line is not None and return_pin is not None:
                if line[1] == return_pin[1]:
                    result.issue(f"{label} shunt line and return pins use the same net {line[1]}")
                expected_line = assertion.get(str(shunt.get("line_net_field")))
                expected_return = assertion.get(str(shunt.get("return_net_field")))
                if string_value(expected_line) and line[1] != str(expected_line):
                    result.issue(f"{label} line net is {line[1]}, expected {expected_line}")
                if string_value(expected_return) and return_pin[1] != str(expected_return):
                    result.issue(f"{label} return net is {return_pin[1]}, expected {expected_return}")

    if production and config.get("require_protection_coverage_for_production") is True:
        required_disposition = normalized(config.get("required_protection_disposition"))
        for index, intent in enumerate(list_value(get_path(spec, "architecture.protection_intents"))):
            if not isinstance(intent, dict) or normalized(intent.get("disposition")) != required_disposition:
                continue
            intent_id = intent.get("id")
            if string_value(intent_id) and str(intent_id) not in covered:
                result.issue(
                    f"architecture.protection_intents[{index}] is not covered by a schematic path assertion: {intent_id}"
                )
                continue
            boundary_id = intent.get("boundary_id")
            if string_value(intent_id) and string_value(boundary_id) and str(boundary_id) not in coverage_boundaries.get(str(intent_id), set()):
                result.issue(
                    f"architecture.protection_intents[{index}] boundary {boundary_id} does not match its schematic path assertion"
                )


def check_pin_type_override_evidence(
    spec: dict[str, Any], spec_path: Path, policy: dict[str, Any], result: CheckResult, production: bool
) -> None:
    config = mapping_value(policy.get("pin_type_overrides"))
    overrides = get_path(spec, str(config.get("override_path", "")))
    if not isinstance(overrides, dict) or not overrides:
        return
    if not production or config.get("require_evidence_for_production") is not True:
        return
    evidence = get_path(spec, str(config.get("evidence_path", "")))
    evidence = evidence if isinstance(evidence, dict) else {}
    root = project_root(spec, spec_path, policy)
    artifacts = configured_root(spec, root, policy, "artifacts_dir_field", result)
    for symbol in overrides:
        record = evidence.get(symbol)
        if not isinstance(record, dict):
            result.issue(f"Pin-type override lacks production evidence: {symbol}")
            continue
        for field in string_list(config.get("required_evidence_fields")):
            if not string_value(record.get(field)):
                result.issue(f"Pin-type override evidence for {symbol} requires {field}")
        digest = record.get("sha256")
        if string_value(digest) and re.fullmatch(r"[0-9a-fA-F]{64}", str(digest)) is None:
            result.issue(f"Pin-type override evidence for {symbol} has invalid sha256")
        source_field = str(config.get("source_field", ""))
        digest_field = str(config.get("sha256_field", ""))
        if config.get("evidence_must_be_local_file") is True and string_value(record.get(source_field)):
            source_path = resolve_from_root(record.get(source_field), root)
            if not source_path.is_file():
                result.issue(f"Pin-type override evidence source is missing for {symbol}: {source_path}")
                continue
            if config.get("evidence_must_be_under_artifacts") is True:
                try:
                    source_path.relative_to(artifacts)
                except ValueError:
                    result.issue(f"Pin-type override evidence must stay under project.artifacts_dir for {symbol}")
            if record.get(digest_field) != sha256_file(source_path):
                result.issue(f"Pin-type override evidence sha256 is stale for {symbol}")


def check_contract(
    spec: dict[str, Any], spec_path: Path, policy: dict[str, Any], result: CheckResult, force: bool = False
) -> dict[str, Any]:
    required = stage_required(spec, policy, forced=force)
    production = production_required(spec, policy, forced=False)
    details = {"required": required, "production": production}
    schematic = spec.get("schematic")
    if not isinstance(schematic, dict):
        if required:
            result.issue("schematic section is required before schematic generation")
        else:
            result.warning("schematic stage contract is not required for this legacy/draft spec")
        return details
    connectivity = mapping_value(policy.get("connectivity"))
    section = get_path(spec, str(connectivity.get("section_path", "")))
    section = section if isinstance(section, dict) else {}
    mode_field = str(connectivity.get("mode_field", ""))
    mode = normalized(section.get(mode_field))
    allowed = {normalized(item) for item in string_list(connectivity.get("allowed_modes"))}
    if required and connectivity.get("require_explicit_mode_when_required") is True and not mode:
        result.issue(f"{connectivity.get('section_path')}.{mode_field} must be explicit for schematic stage")
    elif mode and mode not in allowed:
        result.issue(f"Schematic connectivity mode must be one of {', '.join(sorted(allowed))}")
    scope_field = str(connectivity.get("label_scope_field", ""))
    scope = normalized(section.get(scope_field))
    allowed_scopes = {normalized(item) for item in string_list(connectivity.get("allowed_label_scopes"))}
    required_scope = normalized(connectivity.get("required_label_scope"))
    if scope and scope not in allowed_scopes:
        result.issue(f"Schematic label scope must be one of {', '.join(sorted(allowed_scopes))}")
    if required and required_scope and scope != required_scope:
        result.issue(f"Schematic stage requires {connectivity.get('section_path')}.{scope_field}: {required_scope}")
    shape_field = str(connectivity.get("default_label_shape_field", ""))
    default_shape = normalized(section.get(shape_field))
    required_shape = normalized(connectivity.get("required_default_label_shape"))
    allowed_shapes = {normalized(item) for item in string_list(connectivity.get("allowed_label_shapes"))}
    if default_shape and default_shape not in allowed_shapes:
        result.issue(f"Schematic default label shape must be one of {', '.join(sorted(allowed_shapes))}")
    if required and required_shape and default_shape != required_shape:
        result.issue(f"Schematic stage requires {connectivity.get('section_path')}.{shape_field}: {required_shape}")
    net_shape_field = str(connectivity.get("net_label_shape_field", ""))
    for index, net in enumerate(list_value(spec.get("nets"))):
        if isinstance(net, dict) and net_shape_field in net:
            shape = normalized(net.get(net_shape_field))
            if shape not in allowed_shapes:
                result.issue(f"nets[{index}].{net_shape_field} must be one of {', '.join(sorted(allowed_shapes))}")
    check_pin_contract(spec, spec_path, policy, result, required)
    check_net_roles(spec, policy, result, required)
    check_power_flags(spec, policy, result, required)
    check_path_assertions(spec, policy, result, production)
    check_pin_type_override_evidence(spec, spec_path, policy, result, production)
    return details


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run_strict_erc(spec: dict[str, Any], paths: dict[str, Path], result: CheckResult) -> str:
    completed = subprocess.run(["kicad-cli", "version"], text=True, capture_output=True)
    version = completed.stdout.strip()
    if completed.returncode:
        result.issue(completed.stderr.strip() or "Cannot execute kicad-cli version")
        return version
    match = re.search(r"(\d+)\.", version)
    required = get_path(spec, "project.kicad_major_required")
    if match is None or not isinstance(required, int) or int(match.group(1)) != required:
        result.issue(f"Schematic stage requires kicad-cli major {required}, found {version or '<unknown>'}")
        return version
    paths["erc"].parent.mkdir(parents=True, exist_ok=True)
    paths["erc"].unlink(missing_ok=True)
    command = [
        "kicad-cli",
        "sch",
        "erc",
        "--severity-all",
        "--exit-code-violations",
        "--output",
        str(paths["erc"]),
        str(paths["schematic"]),
    ]
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode:
        output = (completed.stdout + completed.stderr).strip()
        result.issue(f"Strict schematic ERC failed: {' | '.join(output.splitlines()[-8:]) or 'unknown failure'}")
    if not paths["erc"].is_file():
        result.issue(f"Strict schematic ERC report is missing: {paths['erc']}")
    else:
        text = paths["erc"].read_text(encoding="utf-8")
        normalized_report = re.sub(
            r"^(ERC report )\([^,\n]+,",
            r"\1(timestamp-normalized,",
            text,
            flags=re.MULTILINE,
        )
        if normalized_report != text:
            paths["erc"].write_text(normalized_report, encoding="utf-8")
    return version


def export_review_pdf(paths: dict[str, Path], result: CheckResult) -> None:
    paths["review_pdf"].unlink(missing_ok=True)
    command = [
        "kicad-cli",
        "sch",
        "export",
        "pdf",
        "--output",
        str(paths["review_pdf"]),
        str(paths["schematic"]),
    ]
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode:
        output = (completed.stdout + completed.stderr).strip()
        result.issue(f"Schematic review PDF export failed: {' | '.join(output.splitlines()[-8:]) or 'unknown failure'}")
    elif not paths["review_pdf"].is_file() or paths["review_pdf"].stat().st_size == 0:
        result.issue(f"Schematic review PDF is missing or empty: {paths['review_pdf']}")


def freeze_binding(spec: dict[str, Any]) -> dict[str, Any]:
    freeze = mapping_value(spec.get("spec_freeze"))
    return {
        "revision": freeze.get("revision"),
        "spec_sha256": freeze.get("spec_sha256"),
        "manifest_path": get_path(freeze, "manifest.path"),
        "manifest_sha256": get_path(freeze, "manifest.sha256"),
    }


def executor_dependencies(policy: dict[str, Any]) -> list[dict[str, str]]:
    skill_root = Path(__file__).resolve().parents[1]
    records = []
    for configured in string_list(policy.get("executor_dependencies")):
        path = (skill_root / configured).resolve()
        try:
            path.relative_to(skill_root)
        except ValueError as error:
            raise ValueError(f"Schematic stage executor dependency leaves the skill root: {configured}") from error
        if not path.is_file():
            raise ValueError(f"Schematic stage executor dependency is missing: {path}")
        records.append({"path": display_path(path, skill_root), "sha256": sha256_file(path)})
    return records


def manifest_data(
    spec: dict[str, Any],
    spec_path: Path,
    policy: dict[str, Any],
    paths: dict[str, Path],
    generator: Path,
    kicad_version: str,
) -> dict[str, Any]:
    root = paths["root"]
    files = {}
    for key in ["schematic", "netlist", "net_graph", "erc", "review_pdf"]:
        path = paths[key]
        files[key] = {"path": display_path(path, root), "sha256": sha256_file(path)}
    return {
        "schema_version": policy.get("manifest_schema_version"),
        "status": "passed",
        "project_name": get_path(spec, "project.name"),
        "generated_at": utc_timestamp(),
        "spec": {
            "path": display_path(spec_path, root),
            "sha256": spec_digest(spec, load_freeze_policy()),
        },
        "spec_freeze": freeze_binding(spec),
        "policy": {"path": display_path(policy_path(), Path(__file__).resolve().parents[1]), "sha256": sha256_file(policy_path())},
        "executor_sha256": sha256_file(Path(__file__).resolve()),
        "executor_dependencies": executor_dependencies(policy),
        "generator": {"path": display_path(generator, root), "sha256": sha256_file(generator)},
        "kicad_cli_version": kicad_version,
        "files": files,
    }


def run_after_generation(
    spec: dict[str, Any], spec_path: Path, generator: Path, result: CheckResult, force: bool = False
) -> dict[str, Any]:
    policy = load_policy()
    details = check_contract(spec, spec_path, policy, result, force=force)
    paths = artifact_paths(spec, spec_path, policy, result)
    if not paths["schematic"].is_file():
        result.issue(f"Generated schematic is missing: {paths['schematic']}")
    if not generator.is_file():
        result.issue(f"Schematic generator is missing: {generator}")
    check_schema(spec, result)
    check_net_graph(spec, result, exact=True)
    if result.ok():
        check_generated_netlist(spec, result, strict_names=True)
    kicad_version = run_strict_erc(spec, paths, result) if result.ok() else ""
    if result.ok():
        export_review_pdf(paths, result)
    if result.ok():
        data = manifest_data(spec, spec_path, policy, paths, generator.resolve(), kicad_version)
        durable_write(paths["manifest"], yaml.safe_dump(data, sort_keys=False, allow_unicode=True).encode("utf-8"))
        details["manifest"] = str(paths["manifest"])
    return details


def check_evidence(
    spec: dict[str, Any], spec_path: Path, result: CheckResult, force: bool = False
) -> dict[str, Any]:
    policy = load_policy()
    details = check_contract(spec, spec_path, policy, result, force=force)
    required = bool(details.get("required") or force)
    paths = artifact_paths(spec, spec_path, policy, result)
    target = paths["manifest"]
    details["manifest"] = str(target)
    if not target.is_file():
        if required:
            result.issue(f"Schematic stage evidence is missing: {target}")
        else:
            result.warning("Schematic stage evidence is not required for this legacy/draft spec")
        return details
    manifest = load_yaml(target)
    if manifest.get("schema_version") != policy.get("manifest_schema_version"):
        result.issue("Schematic stage manifest schema_version is stale")
    if manifest.get("status") != "passed":
        result.issue("Schematic stage manifest status must be passed")
    if manifest.get("project_name") != get_path(spec, "project.name"):
        result.issue("Schematic stage manifest project_name is stale")
    current_spec_digest = spec_digest(spec, load_freeze_policy())
    if get_path(manifest, "spec.sha256") != current_spec_digest:
        result.issue("Schematic stage manifest does not bind the current Spec")
    if mapping_value(manifest.get("spec_freeze")) != freeze_binding(spec):
        result.issue("Schematic stage manifest does not bind the current Spec Freeze")
    if get_path(manifest, "policy.sha256") != sha256_file(policy_path()):
        result.issue("Schematic stage policy changed after ERC")
    if manifest.get("executor_sha256") != sha256_file(Path(__file__).resolve()):
        result.issue("Schematic stage executor changed after ERC")
    if list_value(manifest.get("executor_dependencies")) != executor_dependencies(policy):
        result.issue("Schematic stage executor dependencies changed after ERC")
    root = paths["root"]
    generator_meta = mapping_value(manifest.get("generator"))
    generator_path = resolve_from_root(generator_meta.get("path"), root)
    if not generator_path.is_file() or generator_meta.get("sha256") != sha256_file(generator_path):
        result.issue("Schematic generator changed after strict ERC")
    files = mapping_value(manifest.get("files"))
    for key in ["schematic", "netlist", "net_graph", "erc", "review_pdf"]:
        record = mapping_value(files.get(key))
        expected_path = paths[key]
        if record.get("path") != display_path(expected_path, root):
            result.issue(f"Schematic stage {key} path is stale")
        if not expected_path.is_file() or record.get("sha256") != sha256_file(expected_path):
            result.issue(f"Schematic stage {key} changed after strict ERC")
    return details
