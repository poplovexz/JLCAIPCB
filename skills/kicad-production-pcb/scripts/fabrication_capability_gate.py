#!/usr/bin/env python3
"""Fail closed on fabrication technologies that require manufacturer evidence."""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pcb_skill_checks import (  # noqa: E402
    CheckResult,
    get_path,
    load_spec,
    load_yaml,
    number_value,
    positive_number,
    print_result,
    resolve_spec_project_path,
    sha256_file,
    string_value,
)


POLICY_PATH = Path(__file__).resolve().parents[1] / "assets" / "fabrication-capability-policy.yaml"


def mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def sequence(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def strings(value: Any) -> list[str]:
    return [str(item) for item in sequence(value) if string_value(item)]


def normalized(value: Any) -> str:
    return "" if value is None else str(value).strip().lower()


def enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return normalized(value) in {"true", "yes", "1"}


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_policy() -> dict[str, Any]:
    return load_yaml(POLICY_PATH)


def require_fields(value: dict[str, Any], fields: list[str], label: str, result: CheckResult) -> None:
    for field in fields:
        if field not in value or value[field] in (None, "", [], {}):
            result.issue(f"{label}.{field} is required")


def validate_copper_layer_count(spec: dict[str, Any], policy: dict[str, Any], result: CheckResult) -> int | None:
    value = get_path(spec, "board.layers.copper")
    rules = mapping(policy.get("copper_layers"))
    minimum = rules.get("min_count")
    maximum = rules.get("max_count")
    if not isinstance(minimum, int) or isinstance(minimum, bool):
        result.issue("fabrication capability policy copper_layers.min_count must be an integer")
        return None
    if not isinstance(maximum, int) or isinstance(maximum, bool):
        result.issue("fabrication capability policy copper_layers.max_count must be an integer")
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        result.issue("board.layers.copper must be an integer")
        return None
    if value < minimum or value > maximum:
        result.issue(f"board.layers.copper must be from {minimum} through {maximum}")
    if enabled(rules.get("require_even")) and value % 2:
        result.issue("board.layers.copper must be even")
    return value


def expected_copper_layers(count: int, policy: dict[str, Any], result: CheckResult) -> list[str]:
    rules = mapping(policy.get("copper_layers"))
    front = rules.get("front_name")
    back = rules.get("back_name")
    template = rules.get("inner_name_template")
    if not all(string_value(item) for item in [front, back, template]):
        result.issue("fabrication capability policy copper layer names/template are incomplete")
        return []
    try:
        inner = [str(template).format(index=index) for index in range(1, count - 1)]
    except (KeyError, ValueError) as error:
        result.issue(f"fabrication capability policy inner_name_template is invalid: {error}")
        return []
    return [str(front), *inner, str(back)]


def trigger_rule_matches(spec: dict[str, Any], rule: Any) -> bool:
    if isinstance(rule, str):
        value = get_path(spec, rule)
        return value not in (None, False, "", [], {})
    if not isinstance(rule, dict) or not string_value(rule.get("path")):
        return False
    value = get_path(spec, str(rule["path"]))
    allowed = sequence(rule.get("equals_any"))
    if allowed:
        return normalized(value) in {normalized(item) for item in allowed}
    item_fields = strings(rule.get("items_with_any_fields"))
    if item_fields:
        return any(
            isinstance(item, dict) and any(item.get(field) not in (None, "", [], {}) for field in item_fields)
            for item in sequence(value)
        )
    if rule.get("nonempty") is True:
        return value not in (None, False, "", [], {})
    return enabled(value)


def special_via_types(spec: dict[str, Any], trigger: dict[str, Any]) -> list[str]:
    special = {normalized(item) for item in strings(trigger.get("special_via_types"))}
    default_type = normalized(trigger.get("default_via_type"))
    found: set[str] = set()
    for raw_rule in sequence(trigger.get("special_via_paths")):
        rule = mapping(raw_rule)
        if not string_value(rule.get("path")):
            continue
        value = get_path(spec, str(rule["path"]))
        container = normalized(rule.get("container")) or "sequence"
        if container == "mapping_values":
            items = list(mapping(value).values())
        elif container == "mapping":
            items = [value] if isinstance(value, dict) else []
        else:
            items = sequence(value)
        fixed_type = normalized(rule.get("fixed_type"))
        extractors = sequence(rule.get("extractors"))
        for item in items:
            candidates: list[str] = []
            if fixed_type:
                candidates.append(fixed_type)
            elif extractors and isinstance(item, dict):
                for raw_extractor in extractors:
                    extractor = mapping(raw_extractor)
                    extractor_path = str(extractor.get("path", ""))
                    extracted = get_path(item, extractor_path) if extractor_path else item
                    mode = normalized(extractor.get("mode"))
                    if mode == "scalar_list":
                        candidates.extend(normalized(value) for value in strings(extracted))
                    elif mode == "mapping_list":
                        for entry in sequence(extracted):
                            if not isinstance(entry, dict):
                                continue
                            for field in strings(extractor.get("type_fields")):
                                if string_value(entry.get(field)):
                                    candidates.append(normalized(entry[field]))
                                    break
                    elif mode == "boolean_feature" and enabled(extracted):
                        candidates.append(normalized(extractor.get("feature")))
                    elif mode == "scalar" and string_value(extracted):
                        candidates.append(normalized(extracted))
            elif isinstance(item, dict):
                via_type = ""
                for field in strings(rule.get("type_fields")):
                    if string_value(item.get(field)):
                        via_type = normalized(item[field])
                        break
                candidates.append(via_type or default_type)
            found.update(candidate for candidate in candidates if candidate in special)
    return sorted(found)


def determine_requirement(
    spec: dict[str, Any], policy: dict[str, Any], copper_count: int | None, force: bool = False
) -> dict[str, Any]:
    trigger = mapping(policy.get("trigger"))
    stage = normalized(get_path(spec, "project.stage"))
    lifecycle_in_scope = stage in {normalized(item) for item in strings(trigger.get("required_project_stages"))}
    reasons: list[str] = []
    if force:
        reasons.append("command line --require")
    for path in strings(trigger.get("explicit_validation_paths")):
        if enabled(get_path(spec, path)):
            reasons.append(f"{path} is true")
    multilayer_threshold = trigger.get("multilayer_min_copper_layers")
    if (
        copper_count is not None
        and isinstance(multilayer_threshold, int)
        and not isinstance(multilayer_threshold, bool)
        and copper_count >= multilayer_threshold
    ):
        reasons.append(f"board.layers.copper={copper_count} requires multilayer capability evidence")
    controlled = any(trigger_rule_matches(spec, rule) for rule in sequence(trigger.get("controlled_impedance_paths")))
    if controlled:
        reasons.append("controlled impedance is declared")
    via_types = special_via_types(spec, trigger)
    if via_types:
        reasons.append("special via processes are declared: " + ", ".join(via_types))

    feature_names = mapping(trigger.get("required_feature_names"))
    features: set[str] = set(via_types)
    if copper_count is not None and isinstance(multilayer_threshold, int) and copper_count >= multilayer_threshold:
        if string_value(feature_names.get("multilayer")):
            features.add(str(feature_names["multilayer"]))
    if controlled and string_value(feature_names.get("controlled_impedance")):
        features.add(str(feature_names["controlled_impedance"]))
    return {
        "required": bool(reasons),
        "reasons": reasons,
        "stage": stage or None,
        "lifecycle_in_scope": lifecycle_in_scope,
        "controlled_impedance": controlled,
        "special_via_types": via_types,
        "required_features": sorted(features),
    }


def validate_physical_stackup(
    spec: dict[str, Any], policy: dict[str, Any], copper_count: int, result: CheckResult
) -> dict[str, Any]:
    rules = mapping(policy.get("physical_stackup"))
    stackup_path = str(rules.get("spec_path", ""))
    stackup = get_path(spec, stackup_path) if stackup_path else None
    details: dict[str, Any] = {"spec_path": stackup_path}
    if not isinstance(stackup, dict):
        result.issue(f"{stackup_path or 'physical stackup'} must be a mapping when fabrication capability evidence is required")
        return details

    details["digest"] = canonical_sha256(stackup)
    require_fields(stackup, strings(rules.get("required_fields")), stackup_path, result)
    allowed_versions = sequence(rules.get("allowed_schema_versions"))
    if stackup.get("schema_version") not in allowed_versions:
        result.issue(f"{stackup_path}.schema_version is unsupported")

    thickness_field = str(rules.get("board_thickness_field", ""))
    tolerance_field = str(rules.get("total_thickness_tolerance_field", ""))
    scope_field = str(rules.get("total_thickness_scope_field", ""))
    board_thickness = stackup.get(thickness_field)
    tolerance = stackup.get(tolerance_field)
    if not positive_number(board_thickness):
        result.issue(f"{stackup_path}.{thickness_field} must be a positive number")
    if not number_value(tolerance) or float(tolerance) < 0:
        result.issue(f"{stackup_path}.{tolerance_field} must be a non-negative number")
    if stackup.get(scope_field) not in sequence(rules.get("allowed_total_thickness_scopes")):
        result.issue(f"{stackup_path}.{scope_field} is unsupported")

    epsilon = rules.get("numeric_epsilon_mm")
    if not number_value(epsilon) or float(epsilon) < 0:
        result.issue("fabrication capability policy physical_stackup.numeric_epsilon_mm must be non-negative")
        epsilon = 0.0
    for alias in strings(rules.get("optional_total_thickness_alias_fields")):
        if alias not in stackup:
            continue
        if not positive_number(stackup.get(alias)):
            result.issue(f"{stackup_path}.{alias} must be a positive number when present")
        elif positive_number(board_thickness) and enabled(rules.get("require_total_thickness_alias_match")):
            if abs(float(stackup[alias]) - float(board_thickness)) > float(epsilon):
                result.issue(f"{stackup_path}.{alias} conflicts with authoritative {stackup_path}.{thickness_field}")

    layers_field = str(rules.get("layers_field", ""))
    layers = stackup.get(layers_field)
    if not isinstance(layers, list) or not layers:
        result.issue(f"{stackup_path}.{layers_field} must be a non-empty list")
        return details

    allowed_types = {normalized(item) for item in strings(rules.get("allowed_entry_types"))}
    dielectric_types = {normalized(item) for item in strings(rules.get("allowed_dielectric_types"))}
    copper_type = normalized(mapping(policy.get("copper_layers")).get("entry_type"))
    dielectric_type = normalized(rules.get("dielectric_entry_type"))
    solder_mask_type = normalized(rules.get("solder_mask_entry_type"))
    if not copper_type or not dielectric_type or not solder_mask_type:
        result.issue("fabrication capability policy physical stackup entry types are incomplete")
    total_types = {normalized(item) for item in strings(rules.get("total_thickness_entry_types"))}
    required_copper = strings(rules.get("required_copper_fields"))
    required_dielectric = strings(rules.get("required_dielectric_fields"))
    required_solder_mask = strings(rules.get("required_solder_mask_fields"))
    solder_mask_kicad_types = mapping(rules.get("solder_mask_layer_kicad_types"))
    required_solder_masks = strings(rules.get("required_solder_mask_names"))
    core_sequence_types = {normalized(item) for item in strings(rules.get("core_sequence_entry_types"))}
    observed_types: list[str] = []
    observed_core_types: list[str] = []
    observed_copper: list[str] = []
    observed_solder_masks: list[str] = []
    thickness_sum = 0.0
    for index, raw_entry in enumerate(layers):
        label = f"{stackup_path}.{layers_field}[{index}]"
        if not isinstance(raw_entry, dict):
            result.issue(f"{label} must be a mapping")
            continue
        entry_type = normalized(raw_entry.get("type"))
        observed_types.append(entry_type)
        if entry_type in core_sequence_types:
            observed_core_types.append(entry_type)
        if entry_type not in allowed_types:
            result.issue(f"{label}.type is unsupported")
            continue
        if entry_type == copper_type:
            require_fields(raw_entry, required_copper, label, result)
            if string_value(raw_entry.get("name")):
                observed_copper.append(str(raw_entry["name"]))
        elif entry_type == dielectric_type:
            require_fields(raw_entry, required_dielectric, label, result)
            if normalized(raw_entry.get("dielectric_type")) not in dielectric_types:
                result.issue(f"{label}.dielectric_type is unsupported")
            if not string_value(raw_entry.get("material")):
                result.issue(f"{label}.material must be a non-empty string")
            if not positive_number(raw_entry.get("epsilon_r")):
                result.issue(f"{label}.epsilon_r must be a positive number")
            loss_tangent = raw_entry.get("loss_tangent")
            if not number_value(loss_tangent) or float(loss_tangent) <= 0:
                result.issue(f"{label}.loss_tangent must be a positive number")
        elif entry_type == solder_mask_type:
            require_fields(raw_entry, required_solder_mask, label, result)
            name = raw_entry.get("name")
            if not string_value(name) or str(name) not in solder_mask_kicad_types:
                result.issue(f"{label}.name is not a configured solder-mask layer")
            else:
                observed_solder_masks.append(str(name))
            if not string_value(raw_entry.get("material")):
                result.issue(f"{label}.material must be a non-empty string")
            if not positive_number(raw_entry.get("epsilon_r")):
                result.issue(f"{label}.epsilon_r must be a positive number")
            loss_tangent = raw_entry.get("loss_tangent")
            if not number_value(loss_tangent) or float(loss_tangent) <= 0:
                result.issue(f"{label}.loss_tangent must be a positive number")
        thickness = raw_entry.get("thickness_mm")
        if not positive_number(thickness):
            result.issue(f"{label}.thickness_mm must be a positive number")
        elif entry_type in total_types:
            thickness_sum += float(thickness)

    expected_copper = expected_copper_layers(copper_count, policy, result)
    if len(observed_copper) != copper_count or set(observed_copper) != set(expected_copper):
        result.issue(f"{stackup_path}.{layers_field} copper layer set does not match board.layers.copper")
    if enabled(rules.get("require_exact_copper_layer_order")) and observed_copper != expected_copper:
        result.issue(f"{stackup_path}.{layers_field} copper layer order does not match KiCad copper order")
    if enabled(rules.get("require_alternating_entries")):
        expected_types = [copper_type if index % 2 == 0 else dielectric_type for index in range(copper_count * 2 - 1)]
        if observed_core_types != expected_types:
            result.issue(f"{stackup_path}.{layers_field} must alternate copper and dielectric entries")
    if observed_solder_masks != required_solder_masks:
        result.issue(f"{stackup_path}.{layers_field} must include configured solder-mask layers in order")
    if (
        enabled(rules.get("require_total_thickness_match"))
        and positive_number(board_thickness)
        and number_value(tolerance)
        and abs(thickness_sum - float(board_thickness)) > float(tolerance) + float(epsilon)
    ):
        result.issue(
            f"{stackup_path}.{layers_field} thickness sum {thickness_sum:.12g}mm does not match "
            f"{thickness_field} {float(board_thickness):.12g}mm within {tolerance_field} {float(tolerance):.12g}mm"
        )
    details.update(
        {
            "copper_layers": observed_copper,
            "expected_copper_layers": expected_copper,
            "solder_mask_layers": observed_solder_masks,
            "entry_count": len(layers),
            "calculated_thickness_mm": thickness_sum,
            "board_thickness_mm": float(board_thickness) if positive_number(board_thickness) else None,
            "total_thickness_tolerance_mm": float(tolerance) if number_value(tolerance) else None,
            "total_thickness_scope": stackup.get(scope_field),
            "solder_mask_included_in_total_thickness": not enabled(rules.get("exclude_solder_mask_from_total_thickness")),
        }
    )
    return details


def parse_timestamp(value: Any, label: str, result: CheckResult) -> datetime | None:
    if not string_value(value):
        result.issue(f"{label} must be an RFC3339 timestamp")
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        result.issue(f"{label} must be an RFC3339 timestamp")
        return None
    if parsed.tzinfo is None:
        result.issue(f"{label} must include a timezone")
        return None
    return parsed.astimezone(timezone.utc)


def load_evidence(path: Path) -> dict[str, Any]:
    if path.suffix.lower() == ".json":
        value = json.loads(path.read_text(encoding="utf-8"))
    else:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"evidence file must contain a top-level mapping: {path}")
    return value


def validate_url(value: Any, allowed_schemes: list[str], label: str, result: CheckResult) -> None:
    if not string_value(value):
        result.issue(f"{label} must be a non-empty URL")
        return
    parsed = urlparse(str(value))
    if normalized(parsed.scheme) not in {normalized(item) for item in allowed_schemes} or not parsed.netloc:
        result.issue(f"{label} must use an allowed source URL scheme and include a host")


def validate_manufacturer_source(
    manufacturer_id: Any,
    source_url: Any,
    rules: dict[str, Any],
    label: str,
    result: CheckResult,
) -> None:
    if not string_value(manufacturer_id):
        return
    registry = mapping(rules.get("manufacturer_source_registry"))
    registry_entry = next(
        (mapping(value) for key, value in registry.items() if normalized(key) == normalized(manufacturer_id)),
        {},
    )
    if not registry_entry:
        result.issue(f"{label} manufacturer_id is not present in the trusted manufacturer source registry")
        return
    patterns = strings(registry_entry.get("allowed_host_regexes"))
    if not patterns:
        result.issue(f"fabrication capability policy source registry for {manufacturer_id} has no allowed host regex")
        return
    hostname = urlparse(str(source_url)).hostname if string_value(source_url) else None
    if not hostname:
        return
    matched = False
    for pattern in patterns:
        try:
            if re.fullmatch(pattern, hostname, flags=re.IGNORECASE):
                matched = True
                break
        except re.error as error:
            result.issue(f"fabrication capability policy source registry regex is invalid for {manufacturer_id}: {error}")
            return
    if not matched:
        result.issue(f"{label} host is not trusted for manufacturer_id {manufacturer_id}")


def validate_evidence_descriptor(
    spec: dict[str, Any],
    spec_path: Path,
    descriptor: Any,
    rules: dict[str, Any],
    label: str,
    result: CheckResult,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    audit: dict[str, Any] = {}
    if not isinstance(descriptor, dict):
        result.issue(f"{label} must be a mapping")
        return {}, audit
    require_fields(descriptor, strings(rules.get("descriptor_required_fields")), label, result)
    path_value = descriptor.get("path")
    if not string_value(path_value):
        return {}, audit
    path = resolve_spec_project_path(spec_path, spec, Path(str(path_value)))
    audit["path"] = str(path)
    allowed_extensions = {normalized(item) for item in strings(rules.get("allowed_file_extensions"))}
    if allowed_extensions and normalized(path.suffix) not in allowed_extensions:
        result.issue(f"{label}.path has an unsupported evidence file extension")
    if not path.is_file():
        result.issue(f"{label}.path does not exist: {path}")
        return {}, audit

    expected_sha = descriptor.get("sha256")
    actual_sha = sha256_file(path)
    audit.update({"sha256": actual_sha, "size_bytes": path.stat().st_size})
    if not isinstance(expected_sha, str) or re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha) is None:
        result.issue(f"{label}.sha256 must be a SHA256 hex digest")
    elif actual_sha != expected_sha.lower():
        result.issue(f"{label}.sha256 does not match evidence file: {path}")

    captured = parse_timestamp(descriptor.get("captured_at"), f"{label}.captured_at", result)
    max_age = descriptor.get("max_age_hours")
    if not positive_number(max_age):
        result.issue(f"{label}.max_age_hours must be a positive number")
    trusted_max_age = rules.get("maximum_descriptor_age_hours")
    if not positive_number(trusted_max_age):
        result.issue(f"fabrication capability policy {label} maximum_descriptor_age_hours must be positive")
    elif positive_number(max_age) and float(max_age) > float(trusted_max_age):
        result.issue(f"{label}.max_age_hours exceeds the trusted policy maximum")
    if captured is not None:
        skew_minutes = rules.get("max_future_clock_skew_minutes")
        if not number_value(skew_minutes) or float(skew_minutes) < 0:
            result.issue(f"fabrication capability policy {label} max_future_clock_skew_minutes must be non-negative")
        else:
            if captured - now > timedelta(minutes=float(skew_minutes)):
                result.issue(f"{label}.captured_at is in the future")
            if positive_number(max_age) and now - captured > timedelta(hours=float(max_age)):
                result.issue(f"{label} is stale")
        audit["captured_at"] = captured.isoformat()
    validate_url(descriptor.get("source_url"), strings(rules.get("allowed_source_url_schemes")), f"{label}.source_url", result)
    audit["source_url"] = descriptor.get("source_url")

    try:
        evidence = load_evidence(path)
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as error:
        result.issue(f"{label}.path could not be read: {error}")
        return {}, audit
    require_fields(evidence, strings(rules.get("evidence_required_fields")), f"{label} file", result)
    if evidence.get("schema_version") not in sequence(rules.get("allowed_schema_versions")):
        result.issue(f"{label} file schema_version is unsupported")
    if evidence.get("captured_at") != descriptor.get("captured_at"):
        result.issue(f"{label} captured_at does not match evidence file")
    if evidence.get("source_url") != descriptor.get("source_url"):
        result.issue(f"{label} source_url does not match evidence file")
    audit["schema_version"] = evidence.get("schema_version")
    return evidence, audit


def validate_process_dispositions(
    spec: dict[str, Any],
    capability_config: dict[str, Any],
    special_types: list[str],
    rules: dict[str, Any],
    result: CheckResult,
) -> dict[str, Any]:
    field = str(rules.get("process_dispositions_field", ""))
    dispositions = mapping(capability_config.get(field))
    declared_outputs = set(strings(get_path(spec, "manufacturing.required_outputs")))
    modes = mapping(rules.get("process_disposition_modes"))
    audit: dict[str, Any] = {}
    for feature in special_types:
        item = mapping(dispositions.get(feature))
        label = f"{rules.get('spec_path')}.{field}.{feature}"
        if not item:
            result.issue(f"{label} is required for the declared special process")
            continue
        mode = normalized(item.get("mode"))
        mode_rules = mapping(modes.get(mode))
        if not mode_rules:
            result.issue(f"{label}.mode is unsupported")
            continue
        require_fields(item, ["mode", *strings(mode_rules.get("required_fields"))], label, result)
        output_field = str(mode_rules.get("output_field", ""))
        outputs = strings(item.get(output_field))
        if not outputs:
            result.issue(f"{label}.{output_field} must name at least one fabrication deliverable")
        missing = sorted(set(outputs) - declared_outputs)
        if missing:
            result.issue(f"{label}.{output_field} is absent from manufacturing.required_outputs: {', '.join(missing)}")
        patterns_field = str(rules.get("artifact_patterns_field", ""))
        patterns = sequence(item.get(patterns_field))
        if not patterns:
            result.issue(f"{label}.{patterns_field} must declare at least one output artifact pattern")
        pattern_audit: list[dict[str, Any]] = []
        for index, raw_pattern in enumerate(patterns):
            pattern = mapping(raw_pattern)
            pattern_label = f"{label}.{patterns_field}[{index}]"
            require_fields(
                pattern,
                strings(rules.get("artifact_pattern_required_fields")),
                pattern_label,
                result,
            )
            if string_value(pattern.get("role")) and str(pattern["role"]) not in outputs:
                result.issue(f"{pattern_label}.role must be listed in {output_field}")
            content_regex = pattern.get("content_regex")
            if string_value(content_regex):
                try:
                    re.compile(str(content_regex))
                except re.error as error:
                    result.issue(f"{pattern_label}.content_regex is invalid: {error}")
            pattern_audit.append({
                "role": pattern.get("role"),
                "path_glob": pattern.get("path_glob"),
                "content_regex": pattern.get("content_regex"),
            })
        audit[feature] = {"mode": mode, "outputs": outputs, "artifact_patterns": pattern_audit}
    return audit


def validate_process_output_artifacts(
    spec: dict[str, Any],
    spec_path: Path,
    capability_config: dict[str, Any],
    special_types: list[str],
    rules: dict[str, Any],
    result: CheckResult,
) -> dict[str, Any]:
    disposition_field = str(rules.get("process_dispositions_field", ""))
    pattern_field = str(rules.get("artifact_patterns_field", ""))
    dispositions = mapping(capability_config.get(disposition_field))
    root = resolve_spec_project_path(spec_path, spec, Path("."))
    audit: dict[str, Any] = {}
    for feature in special_types:
        disposition = mapping(dispositions.get(feature))
        feature_records: list[dict[str, Any]] = []
        for index, raw_pattern in enumerate(sequence(disposition.get(pattern_field))):
            pattern = mapping(raw_pattern)
            label = f"{rules.get('spec_path')}.{disposition_field}.{feature}.{pattern_field}[{index}]"
            raw_glob = pattern.get("path_glob")
            if not string_value(raw_glob):
                continue
            candidate_pattern = Path(str(raw_glob))
            absolute_pattern = candidate_pattern if candidate_pattern.is_absolute() else root / candidate_pattern
            matches = [Path(path).resolve() for path in sorted(glob.glob(str(absolute_pattern)))]
            if not matches:
                result.issue(f"{label}.path_glob matched no fabrication output")
                continue
            content_pattern = pattern.get("content_regex")
            compiled = re.compile(str(content_pattern)) if string_value(content_pattern) else None
            for path in matches:
                try:
                    path.relative_to(root.resolve())
                except ValueError:
                    result.issue(f"{label}.path_glob matched a file outside project.root_dir: {path}")
                    continue
                if not path.is_file() or path.stat().st_size <= 0:
                    result.issue(f"{label}.path_glob matched a missing or empty output: {path}")
                    continue
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                except OSError as error:
                    result.issue(f"{label} output could not be read: {path}: {error}")
                    continue
                if compiled is not None and compiled.search(content) is None:
                    result.issue(f"{label}.content_regex did not match fabrication output: {path}")
                feature_records.append({
                    "role": pattern.get("role"),
                    "path": str(path),
                    "sha256": sha256_file(path),
                    "size_bytes": path.stat().st_size,
                })
        audit[feature] = feature_records
    return audit


def validate_capability_evidence(
    spec: dict[str, Any],
    spec_path: Path,
    policy: dict[str, Any],
    copper_count: int,
    stackup_digest: str | None,
    required_features: list[str],
    special_types: list[str],
    result: CheckResult,
    now: datetime,
) -> dict[str, Any]:
    rules = mapping(policy.get("capability_evidence"))
    config_path = str(rules.get("spec_path", ""))
    config = get_path(spec, config_path) if config_path else None
    audit: dict[str, Any] = {"spec_path": config_path}
    if not isinstance(config, dict):
        result.issue(f"{config_path or 'fabrication capability config'} must be a mapping")
        return audit

    manufacturer_field = str(rules.get("manufacturer_id_field", ""))
    manufacturer_id = config.get(manufacturer_field)
    if not string_value(manufacturer_id):
        result.issue(f"{config_path}.{manufacturer_field} must identify the manufacturer")
    requested_field = str(rules.get("requested_features_field", ""))
    requested = set(strings(config.get(requested_field)))
    missing_requested = sorted(set(required_features) - requested)
    if missing_requested:
        result.issue(f"{config_path}.{requested_field} does not cover required features: {', '.join(missing_requested)}")
    audit["requested_features"] = sorted(requested)
    audit["process_dispositions"] = validate_process_dispositions(spec, config, special_types, rules, result)

    evidence_field = str(rules.get("evidence_field", ""))
    descriptor = config.get(evidence_field)
    validate_manufacturer_source(
        manufacturer_id,
        mapping(descriptor).get("source_url"),
        rules,
        f"{config_path}.{evidence_field}.source_url",
        result,
    )
    evidence, evidence_audit = validate_evidence_descriptor(
        spec,
        spec_path,
        descriptor,
        rules,
        f"{config_path}.{evidence_field}",
        result,
        now,
    )
    audit["evidence"] = evidence_audit
    if not evidence:
        return audit
    if evidence.get("manufacturer_id") != manufacturer_id:
        result.issue(f"{config_path}.{evidence_field} manufacturer_id does not match evidence file")
    capabilities = mapping(evidence.get("capabilities"))
    require_fields(capabilities, strings(rules.get("capabilities_required_fields")), "capability evidence capabilities", result)
    count_field = str(rules.get("supported_copper_layer_counts_field", ""))
    counts = sequence(capabilities.get(count_field))
    if any(not isinstance(item, int) or isinstance(item, bool) for item in counts):
        result.issue(f"capability evidence capabilities.{count_field} must contain integers")
    if copper_count not in counts:
        result.issue(f"capability evidence does not support {copper_count} copper layers")
    digest_field = str(rules.get("supported_stackup_digests_field", ""))
    digests = strings(capabilities.get(digest_field))
    if stackup_digest and stackup_digest not in digests:
        result.issue("capability evidence does not match the current physical stackup digest")
    feature_field = str(rules.get("supported_process_features_field", ""))
    supported_features = set(strings(capabilities.get(feature_field)))
    missing_supported = sorted(requested - supported_features)
    if missing_supported:
        result.issue(f"capability evidence does not support requested features: {', '.join(missing_supported)}")
    audit.update(
        {
            "manufacturer_id": manufacturer_id,
            "supported_copper_layer_counts": counts,
            "supported_stackup_digest": bool(stackup_digest and stackup_digest in digests),
            "supported_process_features": sorted(supported_features),
        }
    )
    return audit


def spec_impedance_targets(spec: dict[str, Any], rules: dict[str, Any], result: CheckResult) -> dict[str, dict[str, Any]]:
    targets: dict[str, dict[str, Any]] = {}
    layer_field = str(rules.get("geometry_layer_field", ""))
    reference_layers_field = str(rules.get("geometry_reference_layers_field", ""))
    for source in sequence(rules.get("target_sources")):
        source_rule = mapping(source)
        source_path = str(source_rule.get("path", ""))
        for index, raw_item in enumerate(sequence(get_path(spec, source_path))):
            if not isinstance(raw_item, dict):
                continue
            target_field = str(source_rule.get("target_field", ""))
            if raw_item.get(target_field) is None:
                continue
            identifier = next((str(raw_item[field]) for field in strings(source_rule.get("id_fields")) if string_value(raw_item.get(field))), "")
            label = f"{source_path}[{index}]"
            if not identifier:
                result.issue(f"{label} requires an impedance target id/name")
                continue
            if identifier in targets:
                result.issue(f"duplicate impedance target id/name: {identifier}")
                continue
            nets_field = str(source_rule.get("nets_field", ""))
            net_field = str(source_rule.get("net_field", ""))
            nets = strings(raw_item.get(nets_field)) if nets_field else []
            if not nets and net_field and string_value(raw_item.get(net_field)):
                nets = [str(raw_item[net_field])]
            tolerance_field = str(source_rule.get("tolerance_field", ""))
            geometry_field = str(source_rule.get("geometry_field", ""))
            target = raw_item.get(target_field)
            tolerance = raw_item.get(tolerance_field)
            geometry = raw_item.get(geometry_field)
            if not nets:
                result.issue(f"{label} must name impedance-controlled net(s)")
            if not positive_number(target):
                result.issue(f"{label}.{target_field} must be a positive number")
            if not positive_number(tolerance):
                result.issue(f"{label}.{tolerance_field} must be a positive number")
            if not isinstance(geometry, dict):
                result.issue(f"{label}.{geometry_field} must be a mapping")
                geometry = {}
            require_fields(
                geometry,
                strings(source_rule.get("required_geometry_fields")),
                f"{label}.{geometry_field}",
                result,
            )
            for field in strings(source_rule.get("positive_geometry_fields")):
                if not positive_number(geometry.get(field)):
                    result.issue(f"{label}.{geometry_field}.{field} must be a positive number")
            if not string_value(geometry.get(layer_field)):
                result.issue(f"{label}.{geometry_field}.{layer_field} must name a copper layer")
            if not strings(geometry.get(reference_layers_field)):
                result.issue(f"{label}.{geometry_field}.{reference_layers_field} must name at least one reference layer")
            targets[identifier] = {
                "id": identifier,
                "nets": nets,
                "target_ohm": float(target) if positive_number(target) else None,
                "tolerance_ohm": float(tolerance) if positive_number(tolerance) else None,
                "geometry": geometry,
            }
    return targets


def validate_impedance_routing_binding(
    spec: dict[str, Any],
    targets: dict[str, dict[str, Any]],
    rules: dict[str, Any],
    result: CheckResult,
) -> None:
    binding = mapping(rules.get("routing_binding"))
    constraints_path = str(binding.get("net_constraints_path", ""))
    constraints = mapping(get_path(spec, constraints_path))
    allowed_layers_field = str(binding.get("allowed_layers_field", ""))
    preferred_width_field = str(binding.get("preferred_width_field", ""))
    neckdown_field = str(binding.get("max_neckdown_length_field", ""))
    layer_field = str(rules.get("geometry_layer_field", ""))
    width_field = str(rules.get("geometry_trace_width_field", ""))
    gap_field = str(rules.get("geometry_gap_field", ""))
    epsilon = binding.get("numeric_epsilon")
    if not number_value(epsilon) or float(epsilon) < 0:
        result.issue("fabrication capability policy impedance routing binding numeric_epsilon must be non-negative")
        epsilon = 0.0

    for identifier, target in targets.items():
        geometry = mapping(target.get("geometry"))
        geometry_layer = geometry.get(layer_field)
        geometry_width = geometry.get(width_field)
        for net in strings(target.get("nets")):
            constraint = mapping(constraints.get(net))
            label = f"{constraints_path}.{net}"
            if not constraint:
                result.issue(f"impedance target {identifier} has no routing constraint for net {net}")
                continue
            allowed_layers = strings(constraint.get(allowed_layers_field))
            if geometry_layer not in allowed_layers:
                result.issue(f"{label}.{allowed_layers_field} does not include impedance geometry layer {geometry_layer}")
            if binding.get("require_single_geometry_layer") is True and allowed_layers != [geometry_layer]:
                result.issue(f"{label}.{allowed_layers_field} must contain only the impedance geometry layer")
            preferred_width = constraint.get(preferred_width_field)
            if (
                not number_value(preferred_width)
                or not number_value(geometry_width)
                or abs(float(preferred_width) - float(geometry_width)) > float(epsilon)
            ):
                result.issue(f"{label}.{preferred_width_field} does not match impedance trace geometry")
            neckdown = constraint.get(neckdown_field)
            if binding.get("require_zero_neckdown") is True and (
                not number_value(neckdown) or abs(float(neckdown)) > float(epsilon)
            ):
                result.issue(f"{label}.{neckdown_field} must be zero for the bound impedance geometry")

        if geometry.get(gap_field) is None:
            continue
        pairs_path = str(binding.get("differential_pairs_path", ""))
        pair_nets_field = str(binding.get("differential_pair_nets_field", ""))
        pair_gap_field = str(binding.get("differential_pair_gap_field", ""))
        expected_nets = strings(target.get("nets"))
        matches: list[dict[str, Any]] = []
        for raw_pair in sequence(get_path(spec, pairs_path)):
            pair = mapping(raw_pair)
            pair_nets = strings(pair.get(pair_nets_field))
            if binding.get("require_exact_differential_net_order") is True:
                matched = pair_nets == expected_nets
            else:
                matched = set(pair_nets) == set(expected_nets)
            if matched:
                matches.append(pair)
        if len(matches) != 1:
            result.issue(f"impedance target {identifier} must match exactly one routing differential pair")
            continue
        routing_gap = matches[0].get(pair_gap_field)
        if (
            not number_value(routing_gap)
            or not number_value(geometry.get(gap_field))
            or abs(float(routing_gap) - float(geometry[gap_field])) > float(epsilon)
        ):
            result.issue(f"impedance target {identifier} routing differential gap does not match geometry")


def validate_impedance_evidence(
    spec: dict[str, Any],
    spec_path: Path,
    policy: dict[str, Any],
    stackup_digest: str | None,
    stackup_layers: list[str],
    result: CheckResult,
    now: datetime,
) -> dict[str, Any]:
    rules = mapping(policy.get("impedance_evidence"))
    descriptor_path = str(rules.get("spec_path", ""))
    descriptor = get_path(spec, descriptor_path) if descriptor_path else None
    audit: dict[str, Any] = {"spec_path": descriptor_path}
    targets = spec_impedance_targets(spec, rules, result)
    if not targets:
        result.issue("controlled impedance requires at least one complete impedance target")
    validate_impedance_routing_binding(spec, targets, rules, result)
    for identifier, target in targets.items():
        geometry = mapping(target.get("geometry"))
        layer_field = str(rules.get("geometry_layer_field", ""))
        reference_layers_field = str(rules.get("geometry_reference_layers_field", ""))
        used_layers = [geometry.get(layer_field), *strings(geometry.get(reference_layers_field))]
        invalid = [str(layer) for layer in used_layers if layer not in stackup_layers]
        if invalid:
            result.issue(f"impedance target {identifier} geometry references layers absent from physical stackup: {', '.join(invalid)}")

    evidence, evidence_audit = validate_evidence_descriptor(
        spec, spec_path, descriptor, rules, descriptor_path, result, now
    )
    audit["evidence"] = evidence_audit
    audit["target_ids"] = sorted(targets)
    if not evidence:
        return audit
    digest_field = str(rules.get("stackup_digest_field", ""))
    if not stackup_digest or evidence.get(digest_field) != stackup_digest:
        result.issue("impedance evidence stackup digest does not match the current physical stackup")
    targets_field = str(rules.get("targets_field", ""))
    evidence_targets: dict[str, dict[str, Any]] = {}
    for index, raw_item in enumerate(sequence(evidence.get(targets_field))):
        label = f"impedance evidence {targets_field}[{index}]"
        if not isinstance(raw_item, dict):
            result.issue(f"{label} must be a mapping")
            continue
        require_fields(raw_item, strings(rules.get("required_target_fields")), label, result)
        identifier = raw_item.get("id")
        if not string_value(identifier):
            result.issue(f"{label}.id must be a non-empty string")
            continue
        if str(identifier) in evidence_targets:
            result.issue(f"impedance evidence duplicates target id {identifier}")
        evidence_targets[str(identifier)] = raw_item

    if enabled(rules.get("require_exact_target_coverage")) and set(evidence_targets) != set(targets):
        result.issue("impedance evidence targets do not exactly cover the current Spec impedance targets")
    allowed_statuses = {normalized(item) for item in strings(rules.get("allowed_result_statuses"))}
    for identifier, expected in targets.items():
        actual = evidence_targets.get(identifier)
        if not isinstance(actual, dict):
            continue
        actual_nets = strings(actual.get("nets"))
        expected_nets = strings(expected.get("nets"))
        if enabled(rules.get("require_exact_net_order")):
            nets_match = actual_nets == expected_nets
        else:
            nets_match = set(actual_nets) == set(expected_nets)
        if not nets_match:
            result.issue(f"impedance evidence target {identifier} nets do not match the Spec")
        for actual_field, expected_field in [("target_ohm", "target_ohm"), ("tolerance_ohm", "tolerance_ohm")]:
            if not number_value(actual.get(actual_field)) or float(actual[actual_field]) != float(expected[expected_field]):
                result.issue(f"impedance evidence target {identifier} {actual_field} does not match the Spec")
        if mapping(actual.get("geometry")) != mapping(expected.get("geometry")):
            result.issue(f"impedance evidence target {identifier} geometry does not match the Spec")
        model = mapping(actual.get("model"))
        require_fields(model, strings(rules.get("required_model_fields")), f"impedance evidence target {identifier}.model", result)
        result_data = mapping(actual.get("result"))
        require_fields(result_data, strings(rules.get("required_result_fields")), f"impedance evidence target {identifier}.result", result)
        if normalized(result_data.get("status")) not in allowed_statuses:
            result.issue(f"impedance evidence target {identifier} result.status is not accepted")
        calculated = result_data.get("calculated_ohm")
        if not positive_number(calculated):
            result.issue(f"impedance evidence target {identifier} result.calculated_ohm must be positive")
        elif expected.get("target_ohm") is not None and expected.get("tolerance_ohm") is not None:
            if abs(float(calculated) - float(expected["target_ohm"])) > float(expected["tolerance_ohm"]):
                result.issue(f"impedance evidence target {identifier} result is outside the Spec tolerance")
    audit["evidence_target_ids"] = sorted(evidence_targets)
    return audit


def check_fabrication_capability(
    spec: dict[str, Any],
    spec_path: Path,
    result: CheckResult,
    force: bool = False,
    check_outputs: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    policy = load_policy()
    checked_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    copper_count = validate_copper_layer_count(spec, policy, result)
    requirement = determine_requirement(spec, policy, copper_count, force=force)
    details: dict[str, Any] = {
        "policy": str(POLICY_PATH),
        "policy_sha256": sha256_file(POLICY_PATH),
        "checked_at": checked_at.isoformat(),
        "copper_layer_count": copper_count,
        **requirement,
    }
    if not requirement["required"]:
        details["status"] = "not_required"
        return details
    if copper_count is None:
        details["status"] = "failed"
        return details

    stackup = validate_physical_stackup(spec, policy, copper_count, result)
    details["physical_stackup"] = stackup
    stackup_digest = stackup.get("digest") if isinstance(stackup.get("digest"), str) else None
    details["capability_evidence"] = validate_capability_evidence(
        spec,
        spec_path,
        policy,
        copper_count,
        stackup_digest,
        strings(requirement.get("required_features")),
        strings(requirement.get("special_via_types")),
        result,
        checked_at,
    )
    if check_outputs:
        capability_rules = mapping(policy.get("capability_evidence"))
        capability_config = mapping(get_path(spec, str(capability_rules.get("spec_path", ""))))
        details["process_output_artifacts"] = validate_process_output_artifacts(
            spec,
            spec_path,
            capability_config,
            strings(requirement.get("special_via_types")),
            capability_rules,
            result,
        )
    if requirement.get("controlled_impedance"):
        details["impedance_evidence"] = validate_impedance_evidence(
            spec,
            spec_path,
            policy,
            stackup_digest,
            strings(stackup.get("expected_copper_layers")),
            result,
            checked_at,
        )
    details["status"] = "passed" if result.ok() else "failed"
    return details


def default_report_path(spec: dict[str, Any], spec_path: Path, policy: dict[str, Any]) -> Path:
    output = mapping(policy.get("output"))
    configured = get_path(spec, str(output.get("report_path_spec_field", "")))
    if string_value(configured):
        return resolve_spec_project_path(spec_path, spec, Path(str(configured)))
    artifacts = get_path(spec, "project.artifacts_dir")
    project_name = get_path(spec, "project.name")
    if not string_value(artifacts) or not string_value(project_name):
        raise ValueError("project.artifacts_dir and project.name are required to write fabrication capability report")
    root = resolve_spec_project_path(spec_path, spec, Path(str(artifacts)))
    return root / str(output.get("default_artifact_subdir")) / str(project_name) / str(output.get("default_filename"))


def result_payload(result: CheckResult, details: dict[str, Any], report_path: Path | None = None) -> dict[str, Any]:
    payload = {
        "check": "fabrication_capability_gate",
        "ok": result.ok(),
        "issues": list(result.issues),
        "warnings": list(result.warnings),
        "details": details,
    }
    if report_path is not None:
        payload["report_path"] = str(report_path)
    return payload


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Validate physical stackup and manufacturer capability evidence before generation.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--before-generation", action="store_true")
    parser.add_argument("--require", action="store_true")
    parser.add_argument("--check-outputs", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--report-output", type=Path)
    args = parser.parse_args(argv[1:])

    result = CheckResult()
    details: dict[str, Any] = {}
    report_path: Path | None = None
    spec: dict[str, Any] = {}
    try:
        spec = load_spec(args.spec)
        details = check_fabrication_capability(
            spec,
            args.spec,
            result,
            force=args.require,
            check_outputs=args.check_outputs,
        )
        output = mapping(load_policy().get("output"))
        if details.get("required") and enabled(output.get("write_report_when_required")):
            report_path = args.report_output.resolve() if args.report_output else default_report_path(spec, args.spec, load_policy())
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(result_payload(result, details, report_path), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except Exception as error:
        result.issue(str(error))

    payload = result_payload(result, details, report_path)
    if report_path is not None and report_path.parent.is_dir():
        try:
            report_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError as error:
            result.issue(f"could not write fabrication capability report: {error}")
            payload = result_payload(result, details, report_path)
    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if result.ok() else 1
    if report_path is not None:
        print(f"fabrication_capability_gate report: {report_path}")
    return print_result("fabrication_capability_gate", result, False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
