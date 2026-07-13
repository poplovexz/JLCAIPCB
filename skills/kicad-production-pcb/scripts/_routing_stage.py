#!/usr/bin/env python3
"""Routing contract, actual-board inspection, and SHA-bound evidence helpers."""

from __future__ import annotations

import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from _layout_stage import board_snapshot as layout_snapshot, check_evidence as check_layout_evidence
from _package_binding_stage import artifacts_root, ensure_artifact, mapping, project_root, resolve, sequence, strings
from _pcb_skill_checks import CheckResult, configured_copper_layer_names, fabrication_capability_policy, get_path, sha256_file, string_value


SKILL_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = SKILL_ROOT / "assets" / "routing-stage-policy.yaml"


def load_policy() -> dict[str, Any]:
    data = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("routing stage policy must be a mapping")
    return data


def normalized(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def spec_copper_layer_names(spec: dict[str, Any], result: CheckResult) -> list[str]:
    return configured_copper_layer_names(spec, result)


def via_type_definitions(policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(name): mapping(value) for name, value in mapping(get_path(policy, "via_types.definitions")).items()}


def canonical_policy_key(value: Any, choices: Any) -> str | None:
    wanted = normalized(value)
    return next((str(choice) for choice in choices if normalized(choice) == wanted), None)


def default_via_type(policy: dict[str, Any]) -> str | None:
    return canonical_policy_key(get_path(policy, "via_types.default_type"), via_type_definitions(policy))


def backdrill_mode_sides(policy: dict[str, Any]) -> dict[str, list[str]]:
    return {
        str(mode): strings(sides)
        for mode, sides in mapping(get_path(policy, "backdrill.mode_sides")).items()
    }


def default_backdrill_mode(policy: dict[str, Any]) -> str | None:
    return canonical_policy_key(get_path(policy, "backdrill.default_mode"), backdrill_mode_sides(policy))


def validate_via_span(
    via_type: str,
    layers: list[str],
    copper_layers: list[str],
    policy: dict[str, Any],
    result: CheckResult,
    label: str,
) -> None:
    definitions = via_type_definitions(policy)
    definition = definitions.get(via_type)
    if definition is None:
        result.issue(f"{label}.via_type is unsupported")
        return
    if len(layers) != 2 or len(set(layers)) != 2 or any(layer not in copper_layers for layer in layers):
        result.issue(f"{label}.layers must identify two distinct configured copper layers")
        return
    positions = [copper_layers.index(layer) for layer in layers]
    if positions != sorted(positions):
        result.issue(f"{label}.layers must be ordered from top to bottom")
        return
    if definition.get("full_span_required") is True and positions != [0, len(copper_layers) - 1]:
        result.issue(f"{label} does not span the full copper stack required by via_type {via_type}")
    expected_outer_count = definition.get("outer_endpoint_count")
    if isinstance(expected_outer_count, int) and not isinstance(expected_outer_count, bool):
        actual_outer_count = sum(position in {0, len(copper_layers) - 1} for position in positions)
        if actual_outer_count != expected_outer_count:
            result.issue(f"{label} has invalid outer-layer endpoints for via_type {via_type}")
    if definition.get("adjacent_layers_required") is True and positions[1] - positions[0] != 1:
        result.issue(f"{label} must join adjacent copper layers for via_type {via_type}")


def stage_required(spec: dict[str, Any], force: bool = False) -> bool:
    policy = load_policy()
    if force or isinstance(spec.get("routing"), dict) or get_path(spec, "validation.routing_stage.required") is True:
        return True
    return normalized(get_path(spec, "project.stage")) in {normalized(v) for v in strings(policy.get("required_project_stages"))}


def board_path(spec: dict[str, Any], root: Path) -> Path:
    return resolve(root, get_path(spec, "project.output_dir")) / f"{get_path(spec, 'project.name')}.kicad_pcb"


def electrical_net_names(spec: dict[str, Any]) -> set[str]:
    pad_counts: dict[str, int] = {}
    for component in sequence(spec.get("components")):
        for net in mapping(component.get("pads") if isinstance(component, dict) else {}).values():
            if string_value(net):
                pad_counts[str(net)] = pad_counts.get(str(net), 0) + 1
    return {net for net, count in pad_counts.items() if count > 1}


def require_fields(data: dict[str, Any], fields: list[str], result: CheckResult, label: str) -> None:
    for field in fields:
        if data.get(field) in (None, "", [], {}):
            result.issue(f"{label}.{field} is required")


def check_contract(spec: dict[str, Any], spec_path: Path, result: CheckResult, force: bool = False) -> dict[str, Any]:
    required = stage_required(spec, force)
    root = project_root(spec, spec_path)
    details = {"required": required, "root": str(root), "board": str(board_path(spec, root))}
    if not required:
        result.warning("Routing stage is not required for this legacy/draft spec")
        return details
    policy = load_policy()
    if default_backdrill_mode(policy) is None:
        result.issue("routing stage policy backdrill.default_mode must identify a configured mode")
    routing = mapping(spec.get("routing"))
    require_fields(routing, strings(policy.get("required_routing_fields")), result, "routing")
    if routing.get("schema_version") not in sequence(policy.get("routing_schema_versions")):
        result.issue("routing.schema_version is unsupported")
    if normalized(routing.get("state")) not in {normalized(v) for v in strings(policy.get("ready_states"))}:
        result.issue("routing.state is not ready")
    if not isinstance(routing.get("revision"), int) or isinstance(routing.get("revision"), bool) or int(routing.get("revision", 0)) < 1:
        result.issue("routing.revision must be a positive integer")
    if normalized(routing.get("strategy")) not in {normalized(v) for v in strings(policy.get("strategies"))}:
        result.issue("routing.strategy is unsupported")

    known_nets = {str(item.get("name")) for item in sequence(spec.get("nets")) if isinstance(item, dict) and string_value(item.get("name"))}
    required_nets = electrical_net_names(spec)
    batch_nets: list[str] = []
    ids: set[str] = set()
    orders: set[int] = set()
    freerouting_enabled = get_path(spec, "routing.freerouting.enabled") is True
    for index, raw in enumerate(sequence(routing.get("batches"))):
        batch = mapping(raw)
        label = f"routing.batches[{index}]"
        require_fields(batch, strings(policy.get("required_batch_fields")), result, label)
        identifier = str(batch.get("id", ""))
        if identifier in ids:
            result.issue(f"routing.batches duplicates id {identifier}")
        ids.add(identifier)
        order = batch.get("order")
        if not isinstance(order, int) or isinstance(order, bool) or order < 1 or order in orders:
            result.issue(f"{label}.order must be a unique positive integer")
        else:
            orders.add(order)
        method = normalized(batch.get("method"))
        if method not in {normalized(v) for v in strings(policy.get("batch_methods"))}:
            result.issue(f"{label}.method is unsupported")
        if method == "freerouting" and not freerouting_enabled:
            result.issue(f"{label} uses Freerouting but routing.freerouting.enabled is not true")
        if normalized(batch.get("state")) not in {normalized(v) for v in strings(policy.get("batch_states"))}:
            result.issue(f"{label}.state is unsupported")
        for net in strings(batch.get("nets")):
            if net not in known_nets:
                result.issue(f"{label} references unknown net {net}")
            batch_nets.append(net)
    if sorted(batch_nets) != sorted(required_nets):
        result.issue("routing.batches must cover every multi-pin electrical net exactly once")

    constraints = mapping(routing.get("net_constraints"))
    if set(constraints) != required_nets:
        result.issue("routing.net_constraints must exactly cover every multi-pin electrical net")
    copper_layers = spec_copper_layer_names(spec, result)
    allowed_board_layers = set(copper_layers)
    definitions = via_type_definitions(policy)
    policy_default_via_type = default_via_type(policy)
    if not definitions or policy_default_via_type is None:
        result.issue("routing stage policy requires via type definitions and a valid default_type")
    policy_backdrill_sides = set(strings(get_path(policy, "backdrill.sides")))
    for net, raw in constraints.items():
        item = mapping(raw)
        label = f"routing.net_constraints.{net}"
        require_fields(item, strings(policy.get("required_net_constraint_fields")), result, label)
        layers = set(strings(item.get("allowed_layers")))
        if not layers or not layers <= allowed_board_layers:
            result.issue(f"{label}.allowed_layers contains unsupported copper layers")
        if normalized(item.get("connection_mode")) not in {normalized(value) for value in strings(policy.get("connection_modes"))}:
            result.issue(f"{label}.connection_mode is unsupported")
        topology = normalized(item.get("topology"))
        if topology not in {normalized(value) for value in strings(policy.get("topologies"))}:
            result.issue(f"{label}.topology is unsupported")
        if normalized(item.get("connection_mode")) == "zone" and not strings(item.get("zone_ids")):
            result.issue(f"{label}.zone_ids is required for zone-only routing")
        if normalized(item.get("connection_mode")) == "zone" and topology != "plane":
            result.issue(f"{label}.topology must be plane for zone-only routing")
        if topology == "star":
            anchor = mapping(item.get("star_anchor"))
            if not string_value(anchor.get("ref")) or not string_value(anchor.get("pad")):
                result.issue(f"{label}.star_anchor requires ref and pad")
        for field in ["min_width_mm", "max_vias"]:
            value = item.get(field)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or float(value) < 0:
                result.issue(f"{label}.{field} must be non-negative numeric")
        if "max_length_mm" in item and (not isinstance(item["max_length_mm"], (int, float)) or float(item["max_length_mm"]) <= 0):
            result.issue(f"{label}.max_length_mm must be positive numeric")
        if "preferred_width_mm" in item:
            preferred = item["preferred_width_mm"]
            neckdown_length = item.get("max_neckdown_length_mm")
            if not isinstance(preferred, (int, float)) or isinstance(preferred, bool) or float(preferred) < float(item.get("min_width_mm", 0)):
                result.issue(f"{label}.preferred_width_mm must be numeric and at least min_width_mm")
            if not isinstance(neckdown_length, (int, float)) or isinstance(neckdown_length, bool) or float(neckdown_length) < 0:
                result.issue(f"{label}.max_neckdown_length_mm must be non-negative numeric")
        for field in ["min_via_diameter_mm", "min_via_drill_mm"]:
            if field in item and (not isinstance(item[field], (int, float)) or isinstance(item[field], bool) or float(item[field]) <= 0):
                result.issue(f"{label}.{field} must be positive numeric")
        raw_allowed_via_types = strings(item.get("allowed_via_types")) if "allowed_via_types" in item else ([policy_default_via_type] if policy_default_via_type else [])
        allowed_via_types: list[str] = []
        if not raw_allowed_via_types:
            result.issue(f"{label}.allowed_via_types must not be empty")
        for raw_via_type in raw_allowed_via_types:
            via_type = canonical_policy_key(raw_via_type, definitions)
            if via_type is None:
                result.issue(f"{label}.allowed_via_types contains unsupported via type {raw_via_type}")
            else:
                allowed_via_types.append(via_type)
        if len(allowed_via_types) != len(set(allowed_via_types)):
            result.issue(f"{label}.allowed_via_types contains duplicates")

        backdrill = mapping(item.get("backdrill"))
        if "backdrill" in item and not isinstance(item.get("backdrill"), dict):
            result.issue(f"{label}.backdrill must be a mapping")
        for field in ["allowed", "required"]:
            if field in backdrill and not isinstance(backdrill[field], bool):
                result.issue(f"{label}.backdrill.{field} must be boolean")
        if backdrill.get("required") is True and backdrill.get("allowed") is not True:
            result.issue(f"{label}.backdrill.required needs backdrill.allowed true")
        allowed_backdrill_sides = strings(backdrill.get("allowed_sides")) if "allowed_sides" in backdrill else sorted(policy_backdrill_sides)
        if "allowed_sides" in backdrill and (not allowed_backdrill_sides or not set(allowed_backdrill_sides) <= policy_backdrill_sides):
            result.issue(f"{label}.backdrill.allowed_sides contains unsupported sides")
        if backdrill.get("allowed") is True and not any(definitions.get(via_type, {}).get("backdrill_allowed") is True for via_type in allowed_via_types):
            result.issue(f"{label}.backdrill is allowed but none of allowed_via_types supports it")

        pair_mappings = item.get("via_type_by_layer_pair", [])
        if "via_type_by_layer_pair" in item and not isinstance(pair_mappings, list):
            result.issue(f"{label}.via_type_by_layer_pair must be a list")
            pair_mappings = []
        seen_layer_pairs: set[tuple[str, str]] = set()
        for pair_index, raw_pair_mapping in enumerate(sequence(pair_mappings)):
            pair_mapping = mapping(raw_pair_mapping)
            pair_label = f"{label}.via_type_by_layer_pair[{pair_index}]"
            pair_layers = strings(pair_mapping.get("layers"))
            pair_via_type = canonical_policy_key(pair_mapping.get("via_type"), definitions)
            if pair_via_type is None:
                result.issue(f"{pair_label}.via_type is unsupported")
                continue
            validate_via_span(pair_via_type, pair_layers, copper_layers, policy, result, pair_label)
            if pair_via_type not in allowed_via_types:
                result.issue(f"{pair_label}.via_type is not listed in allowed_via_types")
            if len(pair_layers) == 2:
                pair_key = (pair_layers[0], pair_layers[1])
                if pair_key in seen_layer_pairs:
                    result.issue(f"{label}.via_type_by_layer_pair duplicates layers {pair_layers}")
                seen_layer_pairs.add(pair_key)
    pair_ids: set[str] = set()
    for index, raw in enumerate(sequence(routing.get("differential_pairs"))):
        pair = mapping(raw)
        label = f"routing.differential_pairs[{index}]"
        require_fields(pair, strings(policy.get("required_differential_pair_fields")), result, label)
        identifier = str(pair.get("id", ""))
        if identifier in pair_ids:
            result.issue(f"routing.differential_pairs duplicates id {identifier}")
        pair_ids.add(identifier)
        pair_nets = strings(pair.get("nets"))
        if len(pair_nets) != 2 or len(set(pair_nets)) != 2 or any(net not in constraints for net in pair_nets):
            result.issue(f"{label}.nets must identify two distinct constrained nets")
        for field in ["target_gap_mm", "gap_tolerance_mm", "max_skew_mm"]:
            if not isinstance(pair.get(field), (int, float)) or isinstance(pair.get(field), bool) or float(pair.get(field, -1)) < 0:
                result.issue(f"{label}.{field} must be non-negative numeric")
        ratio = pair.get("min_coupled_ratio")
        if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or not 0 < float(ratio) <= 1:
            result.issue(f"{label}.min_coupled_ratio must be greater than 0 and at most 1")
    return details


def route_snapshot(path: Path, result: CheckResult) -> dict[str, Any]:
    try:
        import pcbnew
    except Exception as error:
        result.issue(f"pcbnew is required for routing inspection: {error}")
        return {}
    if not path.is_file():
        result.issue(f"routed PCB is missing: {path}")
        return {}
    board = pcbnew.LoadBoard(str(path))
    policy = load_policy()
    definitions = via_type_definitions(policy)
    via_enum_to_type: dict[int, str] = {}
    for via_type, definition in definitions.items():
        raw_enum_name = definition.get("pcbnew_enum")
        enum_name = raw_enum_name.strip() if isinstance(raw_enum_name, str) else ""
        enum_value = getattr(pcbnew, enum_name, None) if enum_name else None
        if enum_value is None:
            result.issue(f"routing stage policy via type {via_type} has an unavailable pcbnew_enum")
            continue
        if int(enum_value) in via_enum_to_type:
            result.issue(f"routing stage policy duplicates pcbnew via enum {enum_name}")
            continue
        via_enum_to_type[int(enum_value)] = via_type

    mode_enums = mapping(get_path(policy, "backdrill.mode_enums"))
    configured_mode_sides = backdrill_mode_sides(policy)
    backdrill_enum_to_mode: dict[int, str] = {}
    for mode, enum_name_value in mode_enums.items():
        enum_name = enum_name_value.strip() if isinstance(enum_name_value, str) else ""
        enum_value = getattr(pcbnew, enum_name, None) if enum_name else None
        if enum_value is None:
            result.issue(f"routing stage policy backdrill mode {mode} has an unavailable pcbnew enum")
            continue
        if int(enum_value) in backdrill_enum_to_mode:
            result.issue(f"routing stage policy duplicates pcbnew backdrill enum {enum_name}")
            continue
        backdrill_enum_to_mode[int(enum_value)] = str(mode)
    if set(mode_enums) != set(configured_mode_sides):
        result.issue("routing stage policy backdrill mode_enums and mode_sides must have identical keys")
    if default_backdrill_mode(policy) is None:
        result.issue("routing stage policy backdrill.default_mode must identify a configured mode")
    configured_sides = set(strings(get_path(policy, "backdrill.sides")))
    if any(not set(sides) <= configured_sides for sides in configured_mode_sides.values()):
        result.issue("routing stage policy backdrill mode_sides references an unsupported side")

    records: list[dict[str, Any]] = []
    metrics: dict[str, dict[str, Any]] = {}
    for item_index, item in enumerate(board.GetTracks()):
        net = str(item.GetNetname())
        metric = metrics.setdefault(net, {"length_mm": 0.0, "vias": 0, "segments": 0, "layers": set(), "via_types": set(), "backdrill_modes": set(), "min_width_mm": math.inf, "min_via_diameter_mm": math.inf, "min_via_drill_mm": math.inf})
        if isinstance(item, pcbnew.PCB_VIA):
            position = item.GetPosition()
            layers = [board.GetLayerName(item.TopLayer()), board.GetLayerName(item.BottomLayer())]
            raw_via_enum = int(item.GetViaType())
            via_type = via_enum_to_type.get(raw_via_enum)
            if via_type is None:
                result.issue(f"routing via {item_index} on net {net} uses a pcbnew via enum absent from routing-stage-policy")
            raw_backdrill_enum = int(item.GetBackdrillMode())
            backdrill_mode = backdrill_enum_to_mode.get(raw_backdrill_enum)
            if backdrill_mode is None:
                result.issue(f"routing via {item_index} on net {net} uses a pcbnew backdrill enum absent from routing-stage-policy")
            backdrill: dict[str, Any] = {"mode": backdrill_mode}
            for side in configured_mode_sides.get(backdrill_mode or "", []):
                method_suffix = side[:1].upper() + side[1:]
                layer_getter = getattr(item, f"Get{method_suffix}BackdrillLayer", None)
                size_getter = getattr(item, f"Get{method_suffix}BackdrillSize", None)
                if not callable(layer_getter) or not callable(size_getter):
                    result.issue(f"routing-stage-policy backdrill side {side} has no matching pcbnew API")
                    continue
                stop_layer_id = layer_getter()
                drill_size = size_getter()
                stop_layer = board.GetLayerName(stop_layer_id) if int(stop_layer_id) >= 0 else None
                drill_mm = float(pcbnew.ToMM(drill_size)) if drill_size is not None else None
                backdrill[side] = {"stop_layer": stop_layer, "drill_mm": drill_mm}
                if stop_layer is None or drill_mm is None or drill_mm <= 0:
                    result.issue(f"routing via {item_index} on net {net} has incomplete {side} backdrill details")
            record = {
                "type": "via",
                "net": net,
                "at": [pcbnew.ToMM(position.x), pcbnew.ToMM(position.y)],
                "size_mm": pcbnew.ToMM(item.GetWidth(item.TopLayer())),
                "drill_mm": pcbnew.ToMM(item.GetDrillValue()),
                "layers": layers,
                "via_type": via_type,
                "backdrill": backdrill,
            }
            if via_type is None:
                record["via_type_enum"] = raw_via_enum
            if backdrill_mode is None:
                backdrill["mode_enum"] = raw_backdrill_enum
            metric["vias"] += 1
            metric["min_via_diameter_mm"] = min(metric["min_via_diameter_mm"], float(record["size_mm"]))
            metric["min_via_drill_mm"] = min(metric["min_via_drill_mm"], float(record["drill_mm"]))
            metric["layers"].update(layers)
            if via_type is not None:
                metric["via_types"].add(via_type)
            if backdrill_mode is not None:
                metric["backdrill_modes"].add(backdrill_mode)
        else:
            start, end = item.GetStart(), item.GetEnd()
            layer = board.GetLayerName(item.GetLayer())
            width = float(pcbnew.ToMM(item.GetWidth()))
            length = float(pcbnew.ToMM(item.GetLength()))
            record = {"type": "segment", "net": net, "start": [pcbnew.ToMM(start.x), pcbnew.ToMM(start.y)], "end": [pcbnew.ToMM(end.x), pcbnew.ToMM(end.y)], "width_mm": width, "layer": layer}
            if isinstance(item, pcbnew.PCB_ARC):
                mid = item.GetMid()
                record.update({"type": "arc", "mid": [pcbnew.ToMM(mid.x), pcbnew.ToMM(mid.y)]})
            metric["segments"] += 1
            metric["length_mm"] += length
            metric["min_width_mm"] = min(metric["min_width_mm"], width)
            metric["layers"].add(layer)
        records.append(record)
    normalized_metrics = {}
    for net, item in metrics.items():
        normalized_metrics[net] = {**item, "length_mm": round(item["length_mm"], 6), "min_width_mm": None if math.isinf(item["min_width_mm"]) else round(item["min_width_mm"], 6), "min_via_diameter_mm": None if math.isinf(item["min_via_diameter_mm"]) else round(item["min_via_diameter_mm"], 6), "min_via_drill_mm": None if math.isinf(item["min_via_drill_mm"]) else round(item["min_via_drill_mm"], 6), "layers": sorted(item["layers"]), "via_types": sorted(item["via_types"]), "backdrill_modes": sorted(item["backdrill_modes"])}
    records.sort(key=lambda item: json.dumps(item, sort_keys=True))
    pads: dict[str, dict[str, dict[str, Any]]] = {}
    for footprint in board.GetFootprints():
        ref = str(footprint.GetReference())
        pads[ref] = {}
        for pad in footprint.Pads():
            position = pad.GetPosition()
            pads[ref][str(pad.GetNumber())] = {"x": round(float(pcbnew.ToMM(position.x)), 6), "y": round(float(pcbnew.ToMM(position.y)), 6), "net": str(pad.GetNetname())}
    return {"tracks": records, "pads": pads, "metrics_by_net": dict(sorted(normalized_metrics.items())), "segment_count": sum(v["segments"] for v in normalized_metrics.values()), "via_count": sum(v["vias"] for v in normalized_metrics.values()), "total_length_mm": round(sum(v["length_mm"] for v in normalized_metrics.values()), 6)}


def check_route_constraints(spec: dict[str, Any], snapshot: dict[str, Any], result: CheckResult) -> None:
    constraints = mapping(get_path(spec, "routing.net_constraints"))
    metrics = mapping(snapshot.get("metrics_by_net"))
    policy = load_policy()
    definitions = via_type_definitions(policy)
    policy_default_via_type = default_via_type(policy)
    copper_layers = spec_copper_layer_names(spec, result)
    mode_sides = backdrill_mode_sides(policy)
    policy_backdrill_sides = set(strings(get_path(policy, "backdrill.sides")))
    backdrill_validation = mapping(get_path(policy, "backdrill.validation"))
    track_records = [item for item in sequence(snapshot.get("tracks")) if isinstance(item, dict)]
    for net, raw in constraints.items():
        rule = mapping(raw)
        actual = mapping(metrics.get(net))
        mode = normalized(rule.get("connection_mode"))
        if mode == "zone":
            zones = {str(item.get("id")): item for item in sequence(get_path(spec, "board.copper_zones")) if isinstance(item, dict)}
            if any(identifier not in zones or zones[identifier].get("net") != net for identifier in strings(rule.get("zone_ids"))):
                result.issue(f"routing net {net} zone_ids do not resolve to copper zones on that net")
            continue
        if not actual or int(actual.get("segments", 0)) == 0:
            result.issue(f"routing net {net} has no routed copper segment")
            continue
        if float(actual.get("min_width_mm") or 0) + 1e-9 < float(rule.get("min_width_mm", 0)):
            result.issue(f"routing net {net} is narrower than min_width_mm")
        if "preferred_width_mm" in rule:
            preferred = float(rule["preferred_width_mm"])
            neckdown_length = sum(
                math.dist(item["start"], item["end"])
                for item in sequence(snapshot.get("tracks"))
                if isinstance(item, dict)
                and item.get("net") == net
                and item.get("type") == "segment"
                and float(item.get("width_mm", 0)) + 1e-9 < preferred
            )
            if neckdown_length > float(rule.get("max_neckdown_length_mm", 0)) + 1e-9:
                result.issue(f"routing net {net} exceeds max_neckdown_length_mm")
        if int(actual.get("vias", 0)) > int(rule.get("max_vias", 0)):
            result.issue(f"routing net {net} exceeds max_vias")
        for field in ["min_via_diameter_mm", "min_via_drill_mm"]:
            if int(actual.get("vias", 0)) and field in rule and float(actual.get(field) or 0) + 1e-9 < float(rule[field]):
                result.issue(f"routing net {net} is below {field}")
        via_records = [item for item in track_records if item.get("net") == net and item.get("type") == "via"]
        if len(via_records) != int(actual.get("vias", 0)):
            result.issue(f"routing net {net} via detail records are incomplete")
        raw_allowed_types = strings(rule.get("allowed_via_types")) if "allowed_via_types" in rule else ([policy_default_via_type] if policy_default_via_type else [])
        allowed_types = {
            canonical
            for raw_type in raw_allowed_types
            if (canonical := canonical_policy_key(raw_type, definitions)) is not None
        }
        backdrill_rule = mapping(rule.get("backdrill"))
        backdrill_allowed = backdrill_rule.get("allowed") is True
        backdrill_required = backdrill_rule.get("required") is True
        allowed_backdrill_sides = set(strings(backdrill_rule.get("allowed_sides"))) if "allowed_sides" in backdrill_rule else policy_backdrill_sides
        for via_index, via in enumerate(via_records):
            via_label = f"routing net {net} via[{via_index}]"
            via_type = canonical_policy_key(via.get("via_type"), definitions)
            if via_type is None:
                result.issue(f"{via_label} has unsupported or missing via_type")
            else:
                if via_type not in allowed_types:
                    result.issue(f"{via_label} uses via_type {via_type} outside allowed_via_types")
                validate_via_span(via_type, strings(via.get("layers")), copper_layers, policy, result, via_label)
            backdrill = mapping(via.get("backdrill"))
            mode = canonical_policy_key(backdrill.get("mode"), mode_sides)
            if mode is None:
                result.issue(f"{via_label} has unsupported or missing backdrill.mode")
                active_sides: set[str] = set()
            else:
                active_sides = set(mode_sides[mode])
            detail_sides = set(backdrill) - {"mode", "mode_enum"}
            if detail_sides != active_sides:
                result.issue(f"{via_label} backdrill details do not match its configured mode")
            if active_sides:
                if via_type is None or definitions.get(via_type, {}).get("backdrill_allowed") is not True:
                    result.issue(f"{via_label} via_type does not allow backdrill")
                if not backdrill_allowed:
                    result.issue(f"{via_label} is backdrilled but the net constraint does not allow backdrill")
                if not active_sides <= allowed_backdrill_sides:
                    result.issue(f"{via_label} uses a backdrill side outside allowed_sides")
                via_layers = strings(via.get("layers"))
                via_positions = (
                    [copper_layers.index(layer) for layer in via_layers]
                    if len(via_layers) == 2 and all(layer in copper_layers for layer in via_layers)
                    else []
                )
                stop_positions: dict[str, int] = {}
                for side in active_sides:
                    detail = mapping(backdrill.get(side))
                    stop_layer = detail.get("stop_layer")
                    if stop_layer not in copper_layers:
                        result.issue(f"{via_label} {side} backdrill stop_layer is not a configured copper layer")
                    else:
                        stop_position = copper_layers.index(str(stop_layer))
                        stop_positions[side] = stop_position
                        if (
                            backdrill_validation.get("require_stop_between_via_endpoints") is True
                            and len(via_positions) == 2
                            and not via_positions[0] < stop_position < via_positions[1]
                        ):
                            result.issue(
                                f"{via_label} {side} backdrill stop_layer must be between the via endpoints"
                            )
                    drill_mm = detail.get("drill_mm")
                    if not isinstance(drill_mm, (int, float)) or isinstance(drill_mm, bool) or float(drill_mm) <= 0:
                        result.issue(f"{via_label} {side} backdrill drill_mm must be positive numeric")
                    elif (
                        backdrill_validation.get("require_drill_larger_than_via_drill") is True
                        and isinstance(via.get("drill_mm"), (int, float))
                        and not isinstance(via.get("drill_mm"), bool)
                        and float(drill_mm) <= float(via["drill_mm"])
                    ):
                        result.issue(f"{via_label} {side} backdrill drill_mm must exceed the via drill")
                if (
                    backdrill_validation.get("require_ordered_dual_stops") is True
                    and {"top", "bottom"} <= set(stop_positions)
                    and stop_positions["top"] >= stop_positions["bottom"]
                ):
                    result.issue(f"{via_label} top/bottom backdrill stop layers overlap or cross")
            if backdrill_required and not active_sides:
                result.issue(f"{via_label} is missing required backdrill")
        if backdrill_required and not via_records:
            result.issue(f"routing net {net} requires backdrill but has no vias")
        if not set(strings(actual.get("layers"))) <= set(strings(rule.get("allowed_layers"))):
            result.issue(f"routing net {net} uses a forbidden copper layer")
        if "max_length_mm" in rule and float(actual.get("length_mm", 0)) > float(rule["max_length_mm"]):
            result.issue(f"routing net {net} exceeds max_length_mm")
        check_route_topology(net, rule, snapshot, policy, result)
    for pair in sequence(get_path(spec, "routing.differential_pairs")):
        if not isinstance(pair, dict):
            continue
        nets = strings(pair.get("nets"))
        if len(nets) != 2 or any(net not in metrics for net in nets):
            result.issue(f"differential pair {pair.get('id')} does not resolve to two routed nets")
            continue
        skew = abs(float(metrics[nets[0]]["length_mm"]) - float(metrics[nets[1]]["length_mm"]))
        if skew > float(pair.get("max_skew_mm", 0)):
            result.issue(f"differential pair {pair.get('id')} skew {skew:.3f}mm exceeds max_skew_mm")
        check_differential_pair_geometry(pair, snapshot, policy, result)
    check_impedance_route_geometry(spec, snapshot, result)


def check_impedance_route_geometry(
    spec: dict[str, Any], snapshot: dict[str, Any], result: CheckResult
) -> None:
    fabrication_policy = fabrication_capability_policy(result)
    rules = mapping(fabrication_policy.get("impedance_evidence"))
    binding = mapping(rules.get("routing_binding"))
    epsilon = binding.get("numeric_epsilon")
    if not isinstance(epsilon, (int, float)) or isinstance(epsilon, bool) or float(epsilon) < 0:
        result.issue("fabrication capability policy impedance routing binding numeric_epsilon must be non-negative")
        return
    geometry_layer_field = str(rules.get("geometry_layer_field", ""))
    geometry_width_field = str(rules.get("geometry_trace_width_field", ""))
    tracks = [
        item
        for item in sequence(snapshot.get("tracks"))
        if isinstance(item, dict) and item.get("type") in {"segment", "arc"}
    ]
    for source in sequence(rules.get("target_sources")):
        source_rule = mapping(source)
        source_path = str(source_rule.get("path", ""))
        target_field = str(source_rule.get("target_field", ""))
        nets_field = str(source_rule.get("nets_field", ""))
        net_field = str(source_rule.get("net_field", ""))
        geometry_field = str(source_rule.get("geometry_field", ""))
        for index, raw_target in enumerate(sequence(get_path(spec, source_path))):
            target = mapping(raw_target)
            if target.get(target_field) is None:
                continue
            nets = strings(target.get(nets_field)) if nets_field else []
            if not nets and net_field and string_value(target.get(net_field)):
                nets = [str(target[net_field])]
            geometry = mapping(target.get(geometry_field))
            expected_layer = geometry.get(geometry_layer_field)
            expected_width = geometry.get(geometry_width_field)
            label = f"{source_path}[{index}]"
            for net in nets:
                net_tracks = [item for item in tracks if item.get("net") == net]
                for item in net_tracks:
                    if item.get("layer") != expected_layer:
                        result.issue(f"{label} actual routing for {net} leaves impedance geometry layer {expected_layer}")
                    width = item.get("width_mm")
                    if (
                        not isinstance(width, (int, float))
                        or isinstance(width, bool)
                        or not isinstance(expected_width, (int, float))
                        or isinstance(expected_width, bool)
                        or abs(float(width) - float(expected_width)) > float(epsilon)
                    ):
                        result.issue(f"{label} actual routing width for {net} does not match impedance geometry")


def point_segment_distance(point: tuple[float, float], start: list[float], end: list[float]) -> float:
    px, py = point
    ax, ay = float(start[0]), float(start[1])
    bx, by = float(end[0]), float(end[1])
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return math.hypot(px - ax, py - ay)
    ratio = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return math.hypot(px - (ax + ratio * dx), py - (ay + ratio * dy))


def route_degrees(net: str, snapshot: dict[str, Any], tolerance: float) -> dict[tuple[int, int], int]:
    degrees: dict[tuple[int, int], int] = {}
    scale = 1.0 / tolerance
    for item in sequence(snapshot.get("tracks")):
        if not isinstance(item, dict) or item.get("net") != net or item.get("type") not in {"segment", "arc"}:
            continue
        for point in [item.get("start"), item.get("end")]:
            if not isinstance(point, list) or len(point) != 2:
                continue
            key = (round(float(point[0]) * scale), round(float(point[1]) * scale))
            degrees[key] = degrees.get(key, 0) + 1
    return degrees


def check_route_topology(net: str, rule: dict[str, Any], snapshot: dict[str, Any], policy: dict[str, Any], result: CheckResult) -> None:
    topology = normalized(rule.get("topology"))
    if topology in {"plane", "mixed"}:
        return
    tolerance = float(policy["topology_coordinate_tolerance_mm"])
    degrees = route_degrees(net, snapshot, tolerance)
    branch_points = {point: degree for point, degree in degrees.items() if degree > 2}
    if topology in {"point-to-point", "daisy-chain"} and branch_points:
        result.issue(f"routing net {net} topology {topology} contains an undeclared branch point")
    if topology == "star":
        anchor = mapping(rule.get("star_anchor"))
        pad = mapping(mapping(snapshot.get("pads")).get(str(anchor.get("ref")))).get(str(anchor.get("pad")))
        if not isinstance(pad, dict):
            result.issue(f"routing net {net} star_anchor is absent from the actual PCB")
            return
        scale = 1.0 / tolerance
        key = (round(float(pad["x"]) * scale), round(float(pad["y"]) * scale))
        if degrees.get(key, 0) < 3:
            result.issue(f"routing net {net} star topology does not branch at star_anchor")


def check_differential_pair_geometry(pair: dict[str, Any], snapshot: dict[str, Any], policy: dict[str, Any], result: CheckResult) -> None:
    pair_nets = strings(pair.get("nets"))
    if len(pair_nets) != 2:
        return
    first, second = pair_nets
    records = [item for item in sequence(snapshot.get("tracks")) if isinstance(item, dict)]
    if any(item.get("type") == "arc" and item.get("net") in {first, second} for item in records):
        result.issue(f"differential pair {pair.get('id')} contains arc routing unsupported by the geometry gate")
        return
    first_segments = [item for item in records if item.get("type") == "segment" and item.get("net") == first]
    second_segments = [item for item in records if item.get("type") == "segment" and item.get("net") == second]
    step = float(policy["differential_pair_sample_step_mm"])
    target = float(pair.get("target_gap_mm", 0))
    tolerance = float(pair.get("gap_tolerance_mm", 0))
    total_samples = 0
    coupled_samples = 0
    for segment in first_segments:
        start, end = segment["start"], segment["end"]
        length = math.dist(start, end)
        sample_count = max(2, int(math.ceil(length / step)) + 1)
        peers = [item for item in second_segments if item.get("layer") == segment.get("layer")]
        for index in range(sample_count):
            ratio = index / (sample_count - 1)
            point = (float(start[0]) + (float(end[0]) - float(start[0])) * ratio, float(start[1]) + (float(end[1]) - float(start[1])) * ratio)
            total_samples += 1
            if not peers:
                continue
            edge_gap = min(point_segment_distance(point, peer["start"], peer["end"]) - (float(segment["width_mm"]) + float(peer["width_mm"])) / 2 for peer in peers)
            if target - tolerance <= edge_gap <= target + tolerance:
                coupled_samples += 1
    ratio = coupled_samples / total_samples if total_samples else 0.0
    if ratio + 1e-9 < float(pair.get("min_coupled_ratio", 0)):
        result.issue(f"differential pair {pair.get('id')} coupled ratio {ratio:.3f} is below min_coupled_ratio")


def check_route_lock(spec: dict[str, Any], spec_path: Path, result: CheckResult) -> None:
    lock = mapping(get_path(spec, "routing.route_lock"))
    locked_freerouting = any(isinstance(item, dict) and normalized(item.get("method")) == "freerouting" and normalized(item.get("state")) == "locked" for item in sequence(get_path(spec, "routing.batches")))
    if not lock:
        if locked_freerouting:
            result.issue("locked Freerouting batch requires routing.route_lock")
        return
    artifact = mapping(lock.get("artifact"))
    root = project_root(spec, spec_path)
    path = resolve(root, artifact.get("path"))
    ensure_artifact(path, artifacts_root(spec, root), result, "route lock artifact")
    if lock.get("schema_version") != 1 or lock.get("routing_revision") != get_path(spec, "routing.revision"):
        result.issue("routing.route_lock schema/revision is stale")
    if not path.is_file() or artifact.get("sha256") != sha256_file(path):
        result.issue("routing.route_lock artifact is missing or has a stale hash")


def run_strict_drc(path: Path, output: Path, result: CheckResult) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    command = ["kicad-cli", "pcb", "drc", "--severity-all", "--exit-code-violations", "--refill-zones", "--save-board", "--output", str(output), str(path)]
    completed = subprocess.run(command, text=True, capture_output=True)
    details = {"command": command, "exit_code": completed.returncode, "report": str(output), "stdout": completed.stdout.strip(), "stderr": completed.stderr.strip()}
    if completed.returncode:
        result.issue(f"strict candidate DRC failed with exit {completed.returncode}: {output}")
    return details


def invariant_snapshot(path: Path, result: CheckResult) -> dict[str, Any]:
    snapshot = layout_snapshot(path, result)
    snapshot.pop("track_count", None)
    return snapshot


def evidence_path(spec: dict[str, Any], root: Path) -> Path:
    configured = get_path(spec, "validation.routing_stage.evidence_file")
    if string_value(configured):
        return resolve(root, configured)
    policy = load_policy()
    return artifacts_root(spec, root) / str(policy["default_evidence_subdir"]) / str(get_path(spec, "project.name")) / str(policy["default_evidence_filename"])


def run_after_generation(spec: dict[str, Any], spec_path: Path, result: CheckResult, force: bool = False) -> dict[str, Any]:
    details = check_contract(spec, spec_path, result, force)
    if not details.get("required"):
        return details
    if any(isinstance(item, dict) and normalized(item.get("state")) == "planned" for item in sequence(get_path(spec, "routing.batches"))):
        result.issue("routed generation cannot be accepted while routing batches remain planned")
    check_route_lock(spec, spec_path, result)
    layout_result = CheckResult()
    check_layout_evidence(spec, spec_path, layout_result, True)
    result.issues.extend(layout_result.issues)
    result.warnings.extend(layout_result.warnings)
    path = Path(details["board"])
    snapshot = route_snapshot(path, result)
    check_route_constraints(spec, snapshot, result)
    drc = run_strict_drc(path, artifacts_root(spec, Path(details["root"])) / "routing-stage" / str(get_path(spec, "project.name")) / "strict-drc.rpt", result)
    details["strict_drc"] = drc
    if result.ok():
        root = Path(details["root"])
        target = evidence_path(spec, root)
        ensure_artifact(target, artifacts_root(spec, root), result, "routing stage evidence")
        payload = {"schema_version": load_policy()["manifest_schema_version"], "status": "passed", "project_name": get_path(spec, "project.name"), "generated_at": datetime.now(timezone.utc).isoformat(), "spec_sha256": sha256_file(spec_path), "policy_sha256": sha256_file(POLICY_PATH), "executor_sha256": sha256_file(Path(__file__).resolve()), "board_sha256": sha256_file(path), "routing_fingerprint": snapshot, "strict_drc": {"report": drc["report"], "sha256": sha256_file(Path(drc["report"])), "exit_code": drc["exit_code"]}}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        details["evidence"] = str(target)
    return details


def check_stage_evidence(spec: dict[str, Any], spec_path: Path, result: CheckResult, force: bool = False) -> dict[str, Any]:
    details = check_contract(spec, spec_path, result, force)
    if not details.get("required"):
        return details
    target = evidence_path(spec, Path(details["root"]))
    if not target.is_file():
        result.issue(f"routing stage evidence is missing: {target}")
        return details
    evidence = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    path = Path(details["board"])
    snapshot = route_snapshot(path, result)
    check_route_constraints(spec, snapshot, result)
    if evidence.get("status") != "passed" or evidence.get("spec_sha256") != sha256_file(spec_path):
        result.issue("routing stage evidence does not bind the current Spec")
    if evidence.get("policy_sha256") != sha256_file(POLICY_PATH) or evidence.get("executor_sha256") != sha256_file(Path(__file__).resolve()):
        result.issue("routing stage policy or executor changed after validation")
    if evidence.get("board_sha256") != sha256_file(path) or mapping(evidence.get("routing_fingerprint")) != snapshot:
        result.issue("actual PCB routing changed after routing-stage acceptance")
    drc = mapping(evidence.get("strict_drc"))
    drc_path = Path(str(drc.get("report", "")))
    if drc.get("exit_code") != 0 or not drc_path.is_file() or drc.get("sha256") != sha256_file(drc_path):
        result.issue("routing stage strict DRC evidence is missing or stale")
    details["evidence"] = str(target)
    return details
