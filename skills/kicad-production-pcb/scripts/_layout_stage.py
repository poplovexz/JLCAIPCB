#!/usr/bin/env python3
"""Validate the frozen layout contract and actual KiCad placement before routing."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from _package_binding_stage import artifacts_root, ensure_artifact, mapping, project_root, resolve, sequence, strings
from _pcb_skill_checks import CheckResult, get_path, sha256_file, string_value


SKILL_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = SKILL_ROOT / "assets" / "layout-stage-policy.yaml"


def load_policy() -> dict[str, Any]:
    with POLICY_PATH.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError("layout stage policy must be a mapping")
    return data


def require_fields(data: dict[str, Any], fields: list[str], result: CheckResult, label: str) -> None:
    for field in fields:
        if data.get(field) in (None, "", [], {}):
            result.issue(f"{label}.{field} is required")


def normalized(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def stage_required(spec: dict[str, Any], policy: dict[str, Any], force: bool = False) -> bool:
    if force or get_path(spec, "validation.layout_stage.required") is True or isinstance(spec.get("layout"), dict):
        return True
    return normalized(get_path(spec, "project.stage")) in {normalized(item) for item in strings(policy.get("required_project_stages"))}


def polygon(value: Any, result: CheckResult, label: str) -> list[dict[str, float]]:
    points: list[dict[str, float]] = []
    if not isinstance(value, list) or len(value) < 3:
        result.issue(f"{label} must contain at least three x/y points")
        return points
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            result.issue(f"{label}[{index}] must be a mapping")
            continue
        x, y = raw.get("x"), raw.get("y")
        if not isinstance(x, (int, float)) or isinstance(x, bool) or not isinstance(y, (int, float)) or isinstance(y, bool):
            result.issue(f"{label}[{index}] requires numeric x/y")
            continue
        points.append({"x": float(x), "y": float(y)})
    return points


def outline_loops(spec: dict[str, Any], result: CheckResult | None = None) -> list[list[dict[str, float]]]:
    configured = get_path(spec, "board.outline.loops")
    if isinstance(configured, list) and configured:
        return [polygon(loop, result or CheckResult(), f"board.outline.loops[{index}]") for index, loop in enumerate(configured)]
    origin = mapping(get_path(spec, "board.origin_mm"))
    size = mapping(get_path(spec, "board.size_mm"))
    try:
        x0, y0 = float(origin["x"]), float(origin["y"])
        x1, y1 = x0 + float(size["width"]), y0 + float(size["height"])
    except (KeyError, TypeError, ValueError):
        if result is not None:
            result.issue("board outline requires board.outline.loops or valid origin_mm/size_mm")
        return []
    return [[{"x": x0, "y": y0}, {"x": x1, "y": y0}, {"x": x1, "y": y1}, {"x": x0, "y": y1}]]


def item_ids(items: Any) -> set[str]:
    return {str(item.get("id")) for item in sequence(items) if isinstance(item, dict) and string_value(item.get("id"))}


def board_items(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in sequence(spec.get("components")) + sequence(spec.get("mechanical_footprints")) if isinstance(item, dict)]


def check_unique_ids(items: Any, result: CheckResult, label: str) -> set[str]:
    found: set[str] = set()
    for index, item in enumerate(sequence(items)):
        if not isinstance(item, dict) or not string_value(item.get("id")):
            result.issue(f"{label}[{index}].id is required")
            continue
        identifier = str(item["id"])
        if identifier in found:
            result.issue(f"{label} duplicates id {identifier}")
        found.add(identifier)
    return found


def check_contract(spec: dict[str, Any], spec_path: Path, result: CheckResult, force: bool = False) -> dict[str, Any]:
    policy = load_policy()
    required = stage_required(spec, policy, force)
    details: dict[str, Any] = {"required": required}
    if not required:
        result.warning("Layout stage is not required for this legacy/draft spec")
        return details
    layout = mapping(spec.get("layout"))
    require_fields(layout, strings(policy.get("required_layout_fields")), result, "layout")
    if layout.get("schema_version") not in sequence(policy.get("layout_schema_versions")):
        result.issue("layout.schema_version is unsupported")
    if normalized(layout.get("state")) not in {normalized(item) for item in strings(policy.get("ready_states"))}:
        result.issue("layout.state is not ready")
    if not isinstance(layout.get("revision"), int) or isinstance(layout.get("revision"), bool) or int(layout.get("revision", 0)) < 1:
        result.issue("layout.revision must be a positive integer")
    coordinate = mapping(layout.get("coordinate_system"))
    require_fields(coordinate, strings(policy.get("required_coordinate_system_fields")), result, "layout.coordinate_system")
    if coordinate.get("units") not in strings(policy.get("coordinate_units")):
        result.issue("layout.coordinate_system.units is unsupported")
    outline_loops(spec, result)

    refs = {
        str(component.get("ref")): component
        for component in board_items(spec)
        if isinstance(component, dict) and string_value(component.get("ref"))
    }
    allowed_sides = set(strings(policy.get("component_sides")))
    for ref, component in refs.items():
        position = mapping(component.get("position_mm"))
        for field in ["x", "y", "rotation"]:
            if not isinstance(position.get(field), (int, float)) or isinstance(position.get(field), bool):
                result.issue(f"{ref}.position_mm.{field} must be numeric for layout")
        side = str(position.get("side", component.get("side", ""))).lower()
        if side not in allowed_sides:
            result.issue(f"{ref}.position_mm.side must be one of {', '.join(sorted(allowed_sides))}")

    batches = sequence(layout.get("placement_batches"))
    batch_refs: list[str] = []
    orders: set[int] = set()
    batch_ids: set[str] = set()
    for index, batch in enumerate(batches):
        item = mapping(batch)
        require_fields(item, strings(policy.get("required_batch_fields")), result, f"layout.placement_batches[{index}]")
        identifier = str(item.get("id", ""))
        if identifier in batch_ids:
            result.issue(f"layout.placement_batches duplicates id {identifier}")
        batch_ids.add(identifier)
        order = item.get("order")
        if not isinstance(order, int) or isinstance(order, bool) or order < 1:
            result.issue(f"layout.placement_batches[{index}].order must be a positive integer")
        elif order in orders:
            result.issue(f"layout.placement_batches duplicates order {order}")
        else:
            orders.add(order)
        for ref in strings(item.get("refs")):
            if ref not in refs:
                result.issue(f"layout.placement_batches[{index}] references unknown component {ref}")
            batch_refs.append(ref)
    if sorted(batch_refs) != sorted(refs):
        result.issue("layout.placement_batches must cover every component exactly once")

    collections = mapping(policy.get("constraint_collections"))
    constraints = mapping(layout.get("constraints"))
    allowed_statuses = {normalized(item) for item in strings(policy.get("constraint_statuses"))}
    always_defined = {str(item) for item in strings(policy.get("always_defined_areas"))}
    for area in strings(policy.get("required_constraint_areas")):
        item = mapping(constraints.get(area))
        label = f"layout.constraints.{area}"
        status = normalized(item.get("status"))
        if status not in allowed_statuses:
            result.issue(f"{label}.status is unsupported")
        if area in always_defined and status != "defined":
            result.issue(f"{label}.status must be defined")
        if not string_value(item.get("rationale")):
            result.issue(f"{label}.rationale is required")
        if status == "defined":
            declared = strings(item.get("items"))
            if not declared:
                result.issue(f"{label}.items must identify concrete constraints")
            collection_path = collections.get(area)
            if string_value(collection_path):
                known = item_ids(get_path(spec, str(collection_path)))
                unknown = sorted(set(declared) - known)
                if unknown:
                    result.issue(f"{label}.items reference missing constraints: {', '.join(unknown)}")
        elif status == "not-applicable" and string_value(collections.get(area)) and sequence(get_path(spec, str(collections[area]))):
            result.issue(f"{label} is not_applicable but its concrete collection is non-empty")

    fixed = sequence(layout.get("fixed_placements"))
    for index, raw in enumerate(fixed):
        item = mapping(raw)
        require_fields(item, strings(policy.get("required_fixed_placement_fields")), result, f"layout.fixed_placements[{index}]")
        if str(item.get("ref")) not in refs:
            result.issue(f"layout.fixed_placements[{index}] references unknown component")

    check_unique_ids(layout.get("regions"), result, "layout.regions")
    for index, raw in enumerate(sequence(layout.get("regions"))):
        item = mapping(raw)
        points = polygon(item.get("polygon"), result, f"layout.regions[{index}].polygon")
        if not points or not strings(item.get("refs")) or not string_value(item.get("rationale")):
            result.issue(f"layout.regions[{index}] requires polygon, refs, and rationale")
        for ref in strings(item.get("refs")):
            if ref not in refs:
                result.issue(f"layout.regions[{index}] references unknown component {ref}")

    for index, raw in enumerate(sequence(layout.get("overlap_waivers"))):
        item = mapping(raw)
        pair = strings(item.get("refs"))
        if len(pair) != 2 or any(ref not in refs for ref in pair) or not string_value(item.get("rationale")):
            result.issue(f"layout.overlap_waivers[{index}] requires two known refs and rationale")

    keepout_ids = check_unique_ids(layout.get("keepouts"), result, "layout.keepouts")
    keepouts_by_id = {str(item.get("id")): item for item in sequence(layout.get("keepouts")) if isinstance(item, dict)}
    for index, raw in enumerate(sequence(layout.get("keepouts"))):
        item = mapping(raw)
        require_fields(item, strings(policy.get("required_keepout_fields")), result, f"layout.keepouts[{index}]")
        polygon(item.get("polygon"), result, f"layout.keepouts[{index}].polygon")
        unsupported = set(strings(item.get("restrictions"))) - set(strings(policy.get("keepout_restrictions")))
        if unsupported:
            result.issue(f"layout.keepouts[{index}] has unsupported restrictions: {', '.join(sorted(unsupported))}")
        for ref in strings(item.get("allowed_refs")):
            if ref not in refs:
                result.issue(f"layout.keepouts[{index}].allowed_refs references unknown component {ref}")

    check_unique_ids(get_path(spec, "board.mechanical_features"), result, "board.mechanical_features")
    for index, raw in enumerate(sequence(get_path(spec, "board.mechanical_features"))):
        item = mapping(raw)
        require_fields(item, strings(policy.get("required_mechanical_fields")), result, f"board.mechanical_features[{index}]")
        if str(item.get("ref", "")) not in refs:
            result.issue(f"board.mechanical_features[{index}].ref is unknown")
        if not isinstance(item.get("clearance_mm"), (int, float)) or float(item.get("clearance_mm", -1)) < 0:
            result.issue(f"board.mechanical_features[{index}].clearance_mm must be non-negative")

    zone_ids = check_unique_ids(get_path(spec, "board.copper_zones"), result, "board.copper_zones")
    nets = {str(item.get("name")) for item in sequence(spec.get("nets")) if isinstance(item, dict)}
    for index, raw in enumerate(sequence(get_path(spec, "board.copper_zones"))):
        item = mapping(raw)
        require_fields(item, strings(policy.get("required_zone_fields")), result, f"board.copper_zones[{index}]")
        polygon(item.get("polygon"), result, f"board.copper_zones[{index}].polygon")
        if str(item.get("net")) not in nets:
            result.issue(f"board.copper_zones[{index}].net references an unknown net")

    for collection_name, distance_field, required_fields in [
        ("proximity_constraints", "max_distance_mm", "required_proximity_fields"),
        ("separation_constraints", "min_distance_mm", "required_separation_fields"),
    ]:
        check_unique_ids(layout.get(collection_name), result, f"layout.{collection_name}")
        for index, raw in enumerate(sequence(layout.get(collection_name))):
            item = mapping(raw)
            require_fields(item, strings(policy.get(required_fields)), result, f"layout.{collection_name}[{index}]")
            pair = strings(item.get("refs"))
            anchors = sequence(item.get("anchors"))
            if anchors:
                if len(anchors) != 2:
                    result.issue(f"layout.{collection_name}[{index}].anchors must contain two ref/pad mappings")
                for anchor in anchors:
                    if not isinstance(anchor, dict) or str(anchor.get("ref", "")) not in refs or not string_value(anchor.get("pad")):
                        result.issue(f"layout.{collection_name}[{index}] contains an invalid ref/pad anchor")
            elif len(pair) != 2 or any(ref not in refs for ref in pair):
                result.issue(f"layout.{collection_name}[{index}] requires two refs or two ref/pad anchors")
            if not isinstance(item.get(distance_field), (int, float)) or float(item.get(distance_field, 0)) <= 0:
                result.issue(f"layout.{collection_name}[{index}].{distance_field} must be positive")

    external_ids: set[str] = set()
    for index, raw in enumerate(sequence(layout.get("external_connectors"))):
        item = mapping(raw)
        label = f"layout.external_connectors[{index}]"
        require_fields(item, strings(policy.get("required_external_connector_fields")), result, label)
        ref = str(item.get("ref", ""))
        if ref not in refs:
            result.issue(f"{label}.ref is unknown")
        if ref in external_ids:
            result.issue(f"layout.external_connectors duplicates {ref}")
        external_ids.add(ref)
        if not isinstance(item.get("max_edge_distance_mm"), (int, float)) or float(item.get("max_edge_distance_mm", -1)) < 0:
            result.issue(f"{label}.max_edge_distance_mm must be non-negative")

    for collection_name in ["high_current_paths", "high_speed_paths", "antenna_constraints", "power_copper", "return_paths", "thermal_paths", "assembly_constraints"]:
        check_unique_ids(layout.get(collection_name), result, f"layout.{collection_name}")
        for index, raw in enumerate(sequence(layout.get(collection_name))):
            item = mapping(raw)
            require_fields(item, strings(policy.get("required_path_fields")), result, f"layout.{collection_name}[{index}]")
            require_fields(
                item,
                strings(get_path(policy, f"collection_required_fields.{collection_name}")),
                result,
                f"layout.{collection_name}[{index}]",
            )
            for net in strings(item.get("nets")) + strings(item.get("signal_nets")):
                if net not in nets:
                    result.issue(f"layout.{collection_name}[{index}] references unknown net {net}")
            for ref in strings(item.get("refs")) + strings(item.get("source_refs")) + strings(item.get("sink_refs")):
                if ref not in refs:
                    result.issue(f"layout.{collection_name}[{index}] references unknown component {ref}")
            for zone_id in strings(item.get("zone_ids")):
                if zone_id not in zone_ids:
                    result.issue(f"layout.{collection_name}[{index}] references unknown copper zone {zone_id}")
            keepout_id = item.get("keepout_id")
            if string_value(keepout_id) and str(keepout_id) not in keepout_ids:
                result.issue(f"layout.{collection_name}[{index}] references unknown keepout {keepout_id}")
            if collection_name in {"high_current_paths", "high_speed_paths", "power_copper"} and not strings(item.get("nets")):
                result.issue(f"layout.{collection_name}[{index}].nets must be non-empty")
            if collection_name == "antenna_constraints":
                if str(item.get("ref", "")) not in refs or not string_value(item.get("keepout_id")):
                    result.issue(f"layout.antenna_constraints[{index}] requires ref and keepout_id")
                keepout = mapping(keepouts_by_id.get(str(item.get("keepout_id", ""))))
                if not set(strings(item.get("required_layers"))).issubset(set(strings(keepout.get("layers")))):
                    result.issue(f"layout.antenna_constraints[{index}] keepout does not cover every required layer")
                if not {"tracks", "vias", "copper", "footprints"}.issubset(set(strings(keepout.get("restrictions")))):
                    result.issue(f"layout.antenna_constraints[{index}] keepout must block tracks, vias, copper, and footprints")
            if collection_name == "return_paths":
                if not strings(item.get("signal_nets")) or str(item.get("reference_net", "")) not in nets:
                    result.issue(f"layout.return_paths[{index}] requires signal_nets and a declared reference_net")
            if collection_name == "thermal_paths" and not strings(item.get("refs")):
                result.issue(f"layout.thermal_paths[{index}].refs must be non-empty")
            if collection_name == "thermal_paths":
                unknown_objects = set(strings(item.get("layout_objects"))) - (zone_ids | keepout_ids)
                if unknown_objects:
                    result.issue(f"layout.thermal_paths[{index}] references unknown layout objects: {', '.join(sorted(unknown_objects))}")
            if collection_name == "high_current_paths":
                corridor = polygon(item.get("corridor_polygon"), result, f"layout.high_current_paths[{index}].corridor_polygon")
                if corridor:
                    bounds = polygon_bbox(corridor)
                    available = min(bounds["right"] - bounds["left"], bounds["bottom"] - bounds["top"])
                    if not isinstance(item.get("min_corridor_width_mm"), (int, float)) or float(item.get("min_corridor_width_mm", 0)) <= 0:
                        result.issue(f"layout.high_current_paths[{index}].min_corridor_width_mm must be positive")
                    elif available < float(item["min_corridor_width_mm"]):
                        result.issue(f"layout.high_current_paths[{index}] corridor is narrower than min_corridor_width_mm")
                if not isinstance(item.get("current_a"), (int, float)) or float(item.get("current_a", 0)) <= 0:
                    result.issue(f"layout.high_current_paths[{index}].current_a must be positive")
            if collection_name == "high_speed_paths":
                if not isinstance(item.get("max_length_mm"), (int, float)) or float(item.get("max_length_mm", 0)) <= 0:
                    result.issue(f"layout.high_speed_paths[{index}].max_length_mm must be positive")
                if not isinstance(item.get("max_vias"), int) or isinstance(item.get("max_vias"), bool) or int(item.get("max_vias", -1)) < 0:
                    result.issue(f"layout.high_speed_paths[{index}].max_vias must be a non-negative integer")

    root = project_root(spec, spec_path)
    board_relative = Path(str(get_path(spec, "project.output_dir"))) / f"{get_path(spec, 'project.name')}.kicad_pcb"
    details.update({"root": str(root), "board": str(resolve(root, board_relative))})
    return details


def to_mm(pcbnew, value: int) -> float:
    return round(float(pcbnew.ToMM(value)), 6)


def box_record(pcbnew, box) -> dict[str, float]:
    return {
        "left": to_mm(pcbnew, box.GetLeft()),
        "top": to_mm(pcbnew, box.GetTop()),
        "right": to_mm(pcbnew, box.GetRight()),
        "bottom": to_mm(pcbnew, box.GetBottom()),
    }


def footprint_box(pcbnew, footprint):
    layers = pcbnew.LSET()
    layers.AddLayer(pcbnew.B_CrtYd if footprint.IsFlipped() else pcbnew.F_CrtYd)
    box = footprint.GetLayerBoundingBox(layers)
    return box if box.GetWidth() > 0 and box.GetHeight() > 0 else footprint.GetBoundingBox(False, False)


def board_snapshot(board_path: Path, result: CheckResult) -> dict[str, Any]:
    try:
        import pcbnew
    except Exception as error:
        result.issue(f"pcbnew is required for layout inspection: {error}")
        return {}
    if not board_path.is_file():
        result.issue(f"generated PCB is missing: {board_path}")
        return {}
    board = pcbnew.LoadBoard(str(board_path))
    edges: list[dict[str, Any]] = []
    for drawing in board.GetDrawings():
        if drawing.GetLayer() != pcbnew.Edge_Cuts:
            continue
        record = {"shape": int(drawing.GetShape())}
        if hasattr(drawing, "GetStart") and hasattr(drawing, "GetEnd"):
            start, end = drawing.GetStart(), drawing.GetEnd()
            record.update({"start": [to_mm(pcbnew, start.x), to_mm(pcbnew, start.y)], "end": [to_mm(pcbnew, end.x), to_mm(pcbnew, end.y)]})
        edges.append(record)
    footprints: dict[str, dict[str, Any]] = {}
    for footprint in board.GetFootprints():
        position = footprint.GetPosition()
        pads = {}
        for pad in footprint.Pads():
            pad_position = pad.GetPosition()
            pads[str(pad.GetNumber())] = {"x": to_mm(pcbnew, pad_position.x), "y": to_mm(pcbnew, pad_position.y)}
        footprints[str(footprint.GetReference())] = {
            "x": to_mm(pcbnew, position.x),
            "y": to_mm(pcbnew, position.y),
            "rotation": round(float(footprint.GetOrientationDegrees()) % 360, 6),
            "side": "bottom" if footprint.IsFlipped() else "top",
            "courtyard_bbox": box_record(pcbnew, footprint_box(pcbnew, footprint)),
            "pads": pads,
        }
    zones: dict[str, dict[str, Any]] = {}
    for zone in board.Zones():
        zones[str(zone.GetZoneName())] = {
            "rule_area": bool(zone.GetIsRuleArea()),
            "net": str(zone.GetNetname()),
            "bbox": box_record(pcbnew, zone.GetBoundingBox()),
            "tracks_blocked": bool(zone.GetDoNotAllowTracks()),
            "vias_blocked": bool(zone.GetDoNotAllowVias()),
            "copper_blocked": bool(zone.GetDoNotAllowZoneFills()),
            "footprints_blocked": bool(zone.GetDoNotAllowFootprints()),
        }
    return {
        "outline_edges": sorted(edges, key=lambda item: json.dumps(item, sort_keys=True)),
        "outline_bbox": box_record(pcbnew, board.GetBoardEdgesBoundingBox()),
        "footprints": {key: footprints[key] for key in sorted(footprints)},
        "zones": {key: zones[key] for key in sorted(zones)},
        "track_count": sum(1 for _ in board.GetTracks()),
    }


def point_inside(point: tuple[float, float], polygon_points: list[dict[str, float]]) -> bool:
    x, y = point
    inside = False
    previous = polygon_points[-1]
    for current in polygon_points:
        x1, y1 = previous["x"], previous["y"]
        x2, y2 = current["x"], current["y"]
        if ((y1 > y) != (y2 > y)) and x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-30) + x1:
            inside = not inside
        previous = current
    return inside


def bbox_corners(box: dict[str, float]) -> list[tuple[float, float]]:
    return [(box["left"], box["top"]), (box["right"], box["top"]), (box["right"], box["bottom"]), (box["left"], box["bottom"])]


def boxes_overlap(first: dict[str, float], second: dict[str, float], tolerance: float) -> bool:
    return min(first["right"], second["right"]) - max(first["left"], second["left"]) > tolerance and min(first["bottom"], second["bottom"]) - max(first["top"], second["top"]) > tolerance


def box_distance(first: dict[str, float], second: dict[str, float]) -> float:
    dx = max(float(first["left"]) - float(second["right"]), float(second["left"]) - float(first["right"]), 0.0)
    dy = max(float(first["top"]) - float(second["bottom"]), float(second["top"]) - float(first["bottom"]), 0.0)
    return math.hypot(dx, dy)


def polygon_bbox(points: list[dict[str, float]]) -> dict[str, float]:
    return {"left": min(item["x"] for item in points), "right": max(item["x"] for item in points), "top": min(item["y"] for item in points), "bottom": max(item["y"] for item in points)}


def outline_segments(loops: list[list[dict[str, float]]]) -> list[list[list[float]]]:
    segments: list[list[list[float]]] = []
    for loop in loops:
        for start, end in zip(loop, loop[1:] + loop[:1]):
            pair = sorted([[round(start["x"], 6), round(start["y"], 6)], [round(end["x"], 6), round(end["y"], 6)]])
            segments.append(pair)
    return sorted(segments)


def boxes_equal(first: dict[str, Any], second: dict[str, Any], tolerance: float) -> bool:
    return all(abs(float(first.get(field, math.inf)) - float(second.get(field, -math.inf))) <= tolerance for field in ["left", "top", "right", "bottom"])


def angular_difference(first: float, second: float) -> float:
    return abs((first - second + 180) % 360 - 180)


def check_snapshot(spec: dict[str, Any], snapshot: dict[str, Any], policy: dict[str, Any], result: CheckResult, require_unrouted: bool) -> None:
    if not snapshot:
        return
    if require_unrouted and snapshot.get("track_count"):
        result.issue("layout-stage PCB contains tracks before routing is unlocked")
    components = {str(item.get("ref")): item for item in board_items(spec)}
    actual = mapping(snapshot.get("footprints"))
    if set(actual) != set(components):
        result.issue("generated layout footprint refs do not exactly match the Spec")
    position_tolerance = float(policy.get("position_tolerance_mm", 0))
    rotation_tolerance = float(policy.get("rotation_tolerance_deg", 0))
    for ref, component in components.items():
        expected = mapping(component.get("position_mm"))
        found = mapping(actual.get(ref))
        for field in ["x", "y"]:
            if abs(float(found.get(field, math.inf)) - float(expected.get(field, 0))) > position_tolerance:
                result.issue(f"{ref} actual {field} does not match the frozen placement")
        if angular_difference(float(found.get("rotation", math.inf)), float(expected.get("rotation", 0))) > rotation_tolerance:
            result.issue(f"{ref} actual rotation does not match the frozen placement")
        expected_side = str(expected.get("side", component.get("side", "top"))).lower()
        if found.get("side") != expected_side:
            result.issue(f"{ref} actual side does not match the frozen placement")

    layout = mapping(spec.get("layout"))
    overhang = {str(item.get("ref")) for item in sequence(layout.get("external_connectors")) if isinstance(item, dict) and item.get("allow_body_overhang") is True}
    loops = outline_loops(spec)
    outer, holes = (loops[0], loops[1:]) if loops else ([], [])
    actual_segments = sorted(
        sorted([item["start"], item["end"]])
        for item in sequence(snapshot.get("outline_edges"))
        if isinstance(item, dict) and isinstance(item.get("start"), list) and isinstance(item.get("end"), list)
    )
    if actual_segments != outline_segments(loops):
        result.issue("actual Edge.Cuts segments do not exactly match board.outline.loops")
    if loops:
        all_points = [point for loop in loops for point in loop]
        expected_outline_bbox = polygon_bbox(all_points)
        edge_points = [point for pair in actual_segments for point in pair]
        actual_endpoint_bbox = {
            "left": min(point[0] for point in edge_points), "right": max(point[0] for point in edge_points),
            "top": min(point[1] for point in edge_points), "bottom": max(point[1] for point in edge_points),
        } if edge_points else {}
        if not boxes_equal(actual_endpoint_bbox, expected_outline_bbox, float(policy.get("outline_tolerance_mm", 0))):
            result.issue("actual Edge.Cuts bounding box does not match the frozen outline")
    for ref, record in actual.items():
        if ref in overhang or not outer:
            continue
        corners = bbox_corners(mapping(record.get("courtyard_bbox")))
        if any(not point_inside(corner, outer) for corner in corners) or any(any(point_inside(corner, hole) for corner in corners) for hole in holes):
            result.issue(f"{ref} courtyard leaves the permitted board outline")

    waivers = {
        tuple(sorted(strings(item.get("refs"))))
        for item in sequence(layout.get("overlap_waivers"))
        if isinstance(item, dict) and len(strings(item.get("refs"))) == 2 and string_value(item.get("rationale"))
    }
    refs = sorted(actual)
    overlap_tolerance = float(policy.get("overlap_tolerance_mm", 0))
    for index, first in enumerate(refs):
        for second in refs[index + 1 :]:
            if actual[first].get("side") != actual[second].get("side") or (first, second) in waivers:
                continue
            if boxes_overlap(mapping(actual[first]["courtyard_bbox"]), mapping(actual[second]["courtyard_bbox"]), overlap_tolerance):
                result.issue(f"unauthorized courtyard overlap: {first}, {second}")

    for collection, maximum in [("proximity_constraints", True), ("separation_constraints", False)]:
        for item in sequence(layout.get(collection)):
            if not isinstance(item, dict):
                continue
            anchors = sequence(item.get("anchors"))
            if len(anchors) == 2:
                points = []
                for anchor in anchors:
                    ref, pad = str(anchor.get("ref", "")), str(anchor.get("pad", ""))
                    point = get_path(actual, f"{ref}.pads.{pad}")
                    if not isinstance(point, dict):
                        result.issue(f"layout {collection} {item.get('id')} anchor {ref}.{pad} is absent from the actual PCB")
                        points = []
                        break
                    points.append(point)
                if len(points) != 2:
                    continue
                distance = math.hypot(float(points[0]["x"]) - float(points[1]["x"]), float(points[0]["y"]) - float(points[1]["y"]))
            else:
                pair = strings(item.get("refs"))
                if len(pair) != 2 or any(ref not in actual for ref in pair):
                    continue
                first, second = actual[pair[0]], actual[pair[1]]
                distance = math.hypot(float(first["x"]) - float(second["x"]), float(first["y"]) - float(second["y"])) if maximum else box_distance(mapping(first["courtyard_bbox"]), mapping(second["courtyard_bbox"]))
            limit_field = "max_distance_mm" if maximum else "min_distance_mm"
            limit = float(item.get(limit_field, 0))
            if (maximum and distance > limit) or (not maximum and distance < limit):
                result.issue(f"layout {collection} {item.get('id')} distance {distance:.3f}mm violates {limit_field}={limit}")

    for feature in sequence(get_path(spec, "board.mechanical_features")):
        if not isinstance(feature, dict) or str(feature.get("ref")) not in actual:
            continue
        ref = str(feature["ref"])
        clearance = float(feature.get("clearance_mm", 0))
        for other_ref, record in actual.items():
            if other_ref == ref:
                continue
            distance = box_distance(mapping(actual[ref]["courtyard_bbox"]), mapping(record["courtyard_bbox"]))
            if distance < clearance:
                result.issue(f"mechanical feature {feature.get('id')} clearance to {other_ref} is {distance:.3f}mm, below {clearance}mm")

    for path in sequence(layout.get("high_current_paths")):
        if not isinstance(path, dict):
            continue
        corridor = polygon(path.get("corridor_polygon"), CheckResult(), "high-current corridor")
        for ref in strings(path.get("source_refs")) + strings(path.get("sink_refs")):
            if ref in actual and any(not point_inside(corner, corridor) for corner in bbox_corners(mapping(actual[ref]["courtyard_bbox"]))):
                result.issue(f"high-current path {path.get('id')} endpoint {ref} leaves its placement corridor")

    for item in sequence(layout.get("fixed_placements")):
        if not isinstance(item, dict) or str(item.get("ref")) not in actual:
            continue
        record = actual[str(item["ref"])]
        if record.get("side") != item.get("side") or angular_difference(float(record.get("rotation", 0)), float(item.get("rotation_deg", 0))) > rotation_tolerance:
            result.issue(f"fixed placement {item['ref']} side/rotation does not match its contract")

    for region in sequence(layout.get("regions")):
        if not isinstance(region, dict):
            continue
        points = polygon(region.get("polygon"), CheckResult(), "region")
        for ref in strings(region.get("refs")):
            if ref in actual and any(not point_inside(corner, points) for corner in bbox_corners(mapping(actual[ref]["courtyard_bbox"]))):
                result.issue(f"{ref} leaves assigned layout region {region.get('id')}")

    expected_keepouts = {str(item.get("id")): item for item in sequence(layout.get("keepouts")) if isinstance(item, dict)}
    expected_zones = {str(item.get("id")): item for item in sequence(get_path(spec, "board.copper_zones")) if isinstance(item, dict)}
    zones = mapping(snapshot.get("zones"))
    if set(zones) != set(expected_keepouts) | set(expected_zones):
        result.issue("actual layout zones/keepouts do not exactly match the Spec IDs")
    for identifier, item in expected_keepouts.items():
        found = mapping(zones.get(identifier))
        if not found.get("rule_area"):
            result.issue(f"layout keepout {identifier} is not a KiCad rule area")
        restrictions = set(strings(item.get("restrictions")))
        expected_box = polygon_bbox(polygon(item.get("polygon"), CheckResult(), "keepout"))
        if not boxes_equal(mapping(found.get("bbox")), expected_box, float(policy.get("position_tolerance_mm", 0))):
            result.issue(f"layout keepout {identifier} geometry does not match the Spec")
        for restriction, field in [("tracks", "tracks_blocked"), ("vias", "vias_blocked"), ("copper", "copper_blocked"), ("footprints", "footprints_blocked")]:
            if bool(found.get(field)) != (restriction in restrictions):
                result.issue(f"layout keepout {identifier} does not implement {restriction} restriction")
        if "footprints" in restrictions:
            keepout_box = polygon_bbox(polygon(item.get("polygon"), CheckResult(), "keepout"))
            allowed_refs = set(strings(item.get("allowed_refs")))
            for ref, record in actual.items():
                if ref not in allowed_refs and boxes_overlap(mapping(record["courtyard_bbox"]), keepout_box, 0):
                    result.issue(f"{ref} overlaps footprint keepout {identifier}")
    for identifier, item in expected_zones.items():
        found = mapping(zones.get(identifier))
        if found.get("rule_area") or found.get("net") != item.get("net"):
            result.issue(f"copper zone {identifier} net/type does not match the Spec")
        expected_box = polygon_bbox(polygon(item.get("polygon"), CheckResult(), "zone"))
        if not boxes_equal(mapping(found.get("bbox")), expected_box, float(policy.get("position_tolerance_mm", 0))):
            result.issue(f"copper zone {identifier} geometry does not match the Spec")

    for item in sequence(layout.get("external_connectors")):
        if not isinstance(item, dict) or str(item.get("ref")) not in actual:
            continue
        record = actual[str(item["ref"])]
        outward = (float(record["rotation"]) + float(item.get("footprint_outward_axis_deg", 0))) % 360
        if angular_difference(outward, float(item.get("outward_direction_deg", 0))) > float(item.get("orientation_tolerance_deg", 0)):
            result.issue(f"external connector {item['ref']} does not face the required direction")
        edge = str(item.get("board_edge"))
        board_box = polygon_bbox([point for loop in loops for point in loop]) if loops else mapping(snapshot.get("outline_bbox"))
        footprint_box_value = mapping(record.get("courtyard_bbox"))
        distances = {
            "left": abs(float(footprint_box_value.get("left", 0)) - float(board_box.get("left", 0))),
            "right": abs(float(board_box.get("right", 0)) - float(footprint_box_value.get("right", 0))),
            "top": abs(float(footprint_box_value.get("top", 0)) - float(board_box.get("top", 0))),
            "bottom": abs(float(board_box.get("bottom", 0)) - float(footprint_box_value.get("bottom", 0))),
        }
        if edge not in distances or distances[edge] > float(item.get("max_edge_distance_mm", 0)):
            result.issue(f"external connector {item['ref']} is not within its required board-edge distance")


def evidence_path(spec: dict[str, Any], root: Path, policy: dict[str, Any]) -> Path:
    configured = get_path(spec, "validation.layout_stage.evidence_file")
    if string_value(configured):
        return resolve(root, configured)
    return artifacts_root(spec, root) / str(policy["default_evidence_subdir"]) / str(get_path(spec, "project.name")) / str(policy["default_evidence_filename"])


def executor_dependencies(policy: dict[str, Any]) -> list[dict[str, str]]:
    records = []
    for value in strings(policy.get("executor_dependencies")):
        path = SKILL_ROOT / value
        records.append({"path": value, "sha256": sha256_file(path)})
    return records


def run_after_generation(
    spec: dict[str, Any], spec_path: Path, generator: Path, result: CheckResult, force: bool = False
) -> dict[str, Any]:
    details = check_contract(spec, spec_path, result, force)
    if not details.get("required"):
        return details
    board_path = Path(details["board"])
    snapshot = board_snapshot(board_path, result)
    policy = load_policy()
    check_snapshot(spec, snapshot, policy, result, require_unrouted=True)
    if not generator.is_file():
        result.issue(f"layout generator is missing: {generator}")
    if result.ok():
        target = evidence_path(spec, Path(details["root"]), policy)
        ensure_artifact(target, artifacts_root(spec, Path(details["root"])), result, "layout stage evidence")
        payload = {
            "schema_version": policy["manifest_schema_version"],
            "status": "passed",
            "project_name": get_path(spec, "project.name"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "spec_sha256": sha256_file(spec_path),
            "policy_sha256": sha256_file(POLICY_PATH),
            "executor_sha256": sha256_file(Path(__file__).resolve()),
            "executor_dependencies": executor_dependencies(policy),
            "generator": {"path": str(generator.resolve()), "sha256": sha256_file(generator)},
            "layout_fingerprint": snapshot,
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        details["evidence"] = str(target)
    return details


def check_evidence(spec: dict[str, Any], spec_path: Path, result: CheckResult, force: bool = False) -> dict[str, Any]:
    details = check_contract(spec, spec_path, result, force)
    if not details.get("required"):
        return details
    policy = load_policy()
    root = Path(details["root"])
    target = evidence_path(spec, root, policy)
    details["evidence"] = str(target)
    if not target.is_file():
        result.issue(f"layout stage evidence is missing: {target}")
        return details
    with target.open("r", encoding="utf-8") as handle:
        evidence = yaml.safe_load(handle) or {}
    snapshot = board_snapshot(Path(details["board"]), result)
    check_snapshot(spec, snapshot, policy, result, require_unrouted=False)
    comparable = dict(snapshot)
    comparable["track_count"] = get_path(evidence, "layout_fingerprint.track_count")
    if evidence.get("schema_version") != policy.get("manifest_schema_version") or evidence.get("status") != "passed":
        result.issue("layout stage evidence schema/status is stale")
    if evidence.get("project_name") != get_path(spec, "project.name") or evidence.get("spec_sha256") != sha256_file(spec_path):
        result.issue("layout stage evidence does not bind the current Spec")
    if evidence.get("policy_sha256") != sha256_file(POLICY_PATH) or evidence.get("executor_sha256") != sha256_file(Path(__file__).resolve()):
        result.issue("layout stage policy or executor changed after validation")
    if sequence(evidence.get("executor_dependencies")) != executor_dependencies(policy):
        result.issue("layout stage executor dependencies changed after validation")
    generator = mapping(evidence.get("generator"))
    generator_path = Path(str(generator.get("path", "")))
    if not generator_path.is_file() or generator.get("sha256") != sha256_file(generator_path):
        result.issue("layout generator changed after validation")
    if mapping(evidence.get("layout_fingerprint")) != comparable:
        result.issue("actual PCB layout changed after layout-stage acceptance")
    return details
