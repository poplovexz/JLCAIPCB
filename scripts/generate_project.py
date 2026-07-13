#!/usr/bin/env python3
"""Generate the KiCad MVP project from a YAML specification."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path

import yaml

from pcb_technology import (
    inject_physical_stackup,
    load_routing_stage_policy,
    validate_copper_layer_count,
)


def load_spec(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def mm(value: float) -> int:
    import pcbnew

    return pcbnew.FromMM(float(value))


def require_pcbnew():
    try:
        import pcbnew
    except Exception as exc:  # pragma: no cover - environment dependent
        raise SystemExit(f"KiCad pcbnew Python module is required to generate PCB: {exc}") from exc
    return pcbnew


def split_library_id(library_id: str) -> tuple[str, str]:
    if ":" not in library_id:
        raise ValueError(f"Library id must use Library:Name form: {library_id}")
    library, name = library_id.split(":", 1)
    return library, name


def sexpr_text(value) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "\\n")


def library_path_from_entry(entry) -> Path:
    if isinstance(entry, dict):
        if "path" not in entry:
            raise SystemExit(f"Library mapping entry must contain path: {entry}")
        return Path(entry["path"])
    return Path(entry)


def symbol_library_path(spec: dict, library: str) -> Path:
    libraries = spec["kicad"].get("symbol_libraries", {})
    if library in libraries:
        return library_path_from_entry(libraries[library])
    return Path(spec["kicad"]["symbol_root"]) / f"{library}.kicad_sym"


def footprint_library_path(spec: dict, library: str) -> Path:
    libraries = spec["kicad"].get("footprint_libraries", {})
    if library in libraries:
        return library_path_from_entry(libraries[library])
    return Path(spec["kicad"]["footprint_root"]) / f"{library}.pretty"


def schematic_extra_symbol_ids(spec: dict) -> set[str]:
    return {
        str(item["symbol"])
        for item in spec.get("schematic", {}).get("power_flags", [])
        if isinstance(item, dict) and "symbol" in item
    }


def electrical_components(spec: dict) -> list[dict]:
    return [
        component for component in spec["components"]
        if component.get("electrical") is not False
    ]


def used_symbol_library_ids(spec: dict) -> set[str]:
    return {str(component["symbol"]) for component in electrical_components(spec)} | schematic_extra_symbol_ids(spec)


def board_footprint_items(spec: dict) -> list[dict]:
    return electrical_components(spec) + spec.get("mechanical_footprints", [])


def symbol_library_manifest(spec: dict) -> dict[str, str]:
    return {
        library: str(symbol_library_path(spec, library))
        for library in sorted({split_library_id(library_id)[0] for library_id in used_symbol_library_ids(spec)})
    }


def footprint_library_manifest(spec: dict) -> dict[str, str]:
    return {
        library: str(footprint_library_path(spec, library))
        for library in sorted({split_library_id(component["footprint"])[0] for component in board_footprint_items(spec)})
    }


def file_audit_record(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"Cannot audit missing library file: {path}")
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def library_audit(spec: dict) -> dict:
    symbol_libraries = {}
    for library in sorted({split_library_id(library_id)[0] for library_id in used_symbol_library_ids(spec)}):
        symbol_libraries[library] = file_audit_record(symbol_library_path(spec, library))

    footprint_libraries = {}
    footprint_records = []
    for library in sorted({split_library_id(component["footprint"])[0] for component in board_footprint_items(spec)}):
        library_path = footprint_library_path(spec, library)
        if not library_path.exists():
            raise SystemExit(f"Cannot audit missing footprint library: {library_path}")
        footprint_libraries[library] = {
            "path": str(library_path),
            "file_count": len(list(library_path.glob("*.kicad_mod"))),
        }

    for component in sorted(board_footprint_items(spec), key=lambda item: item["ref"]):
        library, footprint_name = split_library_id(component["footprint"])
        footprint_path = footprint_library_path(spec, library) / f"{footprint_name}.kicad_mod"
        record = file_audit_record(footprint_path)
        record.update(
            {
                "ref": component["ref"],
                "footprint": component["footprint"],
                "library": library,
            }
        )
        footprint_records.append(record)

    return {
        "project": spec["project"]["name"],
        "symbol_libraries": symbol_libraries,
        "footprint_libraries": footprint_libraries,
        "footprints": footprint_records,
    }


def write_library_audit(spec: dict, output_dir: Path) -> None:
    audit = library_audit(spec)
    json_path = output_dir / "library_audit.json"
    md_path = output_dir / "library_audit.md"
    json_path.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        f"# {spec['project']['title']} Library Audit",
        "",
        "Generated from spec-defined KiCad library paths.",
        "",
        "## Symbol Libraries",
    ]
    for library, record in sorted(audit["symbol_libraries"].items()):
        lines.append(f"- `{library}`: `{record['path']}` ({record['size_bytes']} bytes, sha256 `{record['sha256']}`)")
    lines.extend(["", "## Footprint Libraries"])
    for library, record in sorted(audit["footprint_libraries"].items()):
        lines.append(f"- `{library}`: `{record['path']}` ({record['file_count']} footprint files)")
    lines.extend(["", "## Used Footprints"])
    for record in audit["footprints"]:
        lines.append(
            f"- `{record['ref']}` `{record['footprint']}`: `{record['path']}` "
            f"({record['size_bytes']} bytes, sha256 `{record['sha256']}`)"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_library_tables(spec: dict, output_dir: Path) -> None:
    def project_uri(path: Path) -> str:
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(output_dir.resolve())
            return "${KIPRJMOD}/" + relative.as_posix()
        except ValueError:
            return str(resolved)

    symbol_libs = {
        library: project_uri(symbol_library_path(spec, library))
        for library in symbol_library_manifest(spec)
    }
    footprint_libs = {
        library: project_uri(footprint_library_path(spec, library))
        for library in footprint_library_manifest(spec)
    }

    sym_lines = ["(sym_lib_table"]
    for library, library_path in symbol_libs.items():
        sym_lines.append(
            f'  (lib (name "{library}")(type "KiCad")(uri "{library_path}")(options "")(descr "Generated from specs.yaml"))'
        )
    sym_lines.append(")")
    (output_dir / "sym-lib-table").write_text("\n".join(sym_lines) + "\n", encoding="utf-8")

    fp_lines = ["(fp_lib_table"]
    for library, library_path in footprint_libs.items():
        fp_lines.append(
            f'  (lib (name {library})(type KiCad)(uri {library_path})(options "")(descr "Generated from specs.yaml"))'
        )
    fp_lines.append(")")
    (output_dir / "fp-lib-table").write_text("\n".join(fp_lines) + "\n", encoding="utf-8")


def write_custom_drc_rules(spec: dict, output_dir: Path) -> None:
    path = output_dir / f"{spec['project']['name']}.kicad_dru"
    rules = spec.get("board", {}).get("custom_drc_rules", [])
    if not rules:
        path.unlink(missing_ok=True)
        return
    allowed_constraints = {"hole_clearance", "physical_hole_clearance"}
    known_refs = {str(item["ref"]) for item in board_footprint_items(spec)}
    identifiers: set[str] = set()
    lines = ["(version 1)", ""]
    for rule in rules:
        identifier = str(rule.get("id", ""))
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", identifier) or identifier in identifiers:
            raise SystemExit(f"Invalid or duplicate custom DRC rule id: {identifier}")
        identifiers.add(identifier)
        constraint = str(rule.get("constraint", ""))
        if constraint not in allowed_constraints:
            raise SystemExit(f"Unsupported custom DRC constraint for {identifier}: {constraint}")
        minimum = rule.get("minimum_mm")
        if not isinstance(minimum, (int, float)) or isinstance(minimum, bool) or float(minimum) <= 0:
            raise SystemExit(f"custom DRC rule {identifier} minimum_mm must be positive")
        refs = [str(ref) for ref in rule.get("within_refs", [])]
        if not refs or any(ref not in known_refs for ref in refs):
            raise SystemExit(f"custom DRC rule {identifier} requires known within_refs")
        if not str(rule.get("rationale", "")).strip() or not rule.get("evidence_refs"):
            raise SystemExit(f"custom DRC rule {identifier} requires rationale and evidence_refs")
        conditions = [f"(A.Reference == '{ref}' && B.Reference == '{ref}')" for ref in refs]
        lines.extend([
            f'(rule "{identifier}"',
            f'    (condition "{" || ".join(conditions)}")',
            f"    (constraint {constraint} (min {float(minimum):g}mm))",
            ")",
            "",
        ])
    path.write_text("\n".join(lines), encoding="utf-8")


def apply_board_silkscreen_policy(footprint, pcbnew, spec: dict) -> bool:
    policy = spec.get("board", {}).get("silkscreen", {})
    mode = str(policy.get("mode", "footprint-default"))
    if mode == "footprint-default":
        return False
    if mode != "assembly-data-only" or not str(policy.get("rationale", "")).strip():
        raise SystemExit("board.silkscreen requires a supported mode and rationale")
    if policy.get("hide_references") is True:
        footprint.Reference().SetVisible(False)
    if policy.get("hide_values") is True:
        footprint.Value().SetVisible(False)
    if policy.get("omit_footprint_graphics") is True:
        for item in list(footprint.GraphicalItems()):
            if isinstance(item, pcbnew.PCB_SHAPE) and item.GetLayer() in {pcbnew.F_SilkS, pcbnew.B_SilkS}:
                item.SetLayer(pcbnew.B_Fab if item.GetLayer() == pcbnew.B_SilkS else pcbnew.F_Fab)
    return True


def build_project_file(spec: dict, output_dir: Path) -> None:
    board_rules = spec["board"]["fabrication"]
    project = {
        "board": {
            "design_settings": {
                "defaults": {
                    "copper_line_width": board_rules["default_signal_width_mm"],
                    "silk_line_width": board_rules["silk_line_width_mm"],
                    "silk_text_size_h": board_rules["silk_text_size_mm"],
                    "silk_text_size_v": board_rules["silk_text_size_mm"],
                    "silk_text_thickness": board_rules["silk_text_thickness_mm"],
                },
                "rules": {
                    "min_clearance": board_rules["min_clearance_mm"],
                    "min_track_width": board_rules["min_track_width_mm"],
                    "min_via_diameter": board_rules["min_via_diameter_mm"],
                    "min_via_drill": board_rules["min_via_drill_mm"],
                },
                "track_widths": [
                    0.0,
                    board_rules["default_signal_width_mm"],
                    board_rules["default_power_width_mm"],
                ],
                "via_dimensions": [
                    {
                        "diameter": board_rules["min_via_diameter_mm"],
                        "drill": board_rules["min_via_drill_mm"],
                    }
                ],
                "drc_exclusions": [],
                "meta": {
                    "version": 2,
                    "filename": "board_design_settings.json",
                },
            }
        },
        "boards": [],
        "erc": {
            "erc_exclusions": [],
            "meta": {
                "version": 0,
            },
        },
        "libraries": {
            "footprint_libraries": footprint_library_manifest(spec),
            "pinned_footprint_root": spec["kicad"]["footprint_root"],
            "pinned_symbol_root": spec["kicad"]["symbol_root"],
            "symbol_libraries": symbol_library_manifest(spec),
        },
        "meta": {
            "filename": f"{spec['project']['name']}.kicad_pro",
            "version": 1,
        },
        "net_settings": {
            "classes": [
                {
                    "name": net_class["name"],
                    "clearance": net_class["clearance_mm"],
                    "track_width": net_class["track_width_mm"],
                    "via_dia": net_class["via_diameter_mm"],
                    "via_drill": net_class["via_drill_mm"],
                }
                for net_class in spec["board"]["net_classes"]
            ]
        },
        "project": {
            "title": spec["project"]["title"],
            "generated_by": spec["project"]["generated_by"],
        },
    }
    path = output_dir / f"{spec['project']['name']}.kicad_pro"
    path.write_text(json.dumps(project, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def outline_loops(spec: dict) -> list[list[dict]]:
    configured = spec.get("board", {}).get("outline", {}).get("loops")
    if isinstance(configured, list) and configured:
        return configured
    origin = spec["board"]["origin_mm"]
    size = spec["board"]["size_mm"]
    x0 = float(origin["x"])
    y0 = float(origin["y"])
    x1 = x0 + float(size["width"])
    y1 = y0 + float(size["height"])
    return [[{"x": x0, "y": y0}, {"x": x1, "y": y0}, {"x": x1, "y": y1}, {"x": x0, "y": y1}]]


def add_edges(board, pcbnew, loops: list[list[dict]], width_mm: float) -> None:
    for loop in loops:
        points = [(float(point["x"]), float(point["y"])) for point in loop]
        if len(points) < 3:
            raise SystemExit("board.outline.loops entries require at least three points")
        points.append(points[0])
        for start, end in zip(points, points[1:]):
            segment = pcbnew.PCB_SHAPE(board)
            segment.SetShape(pcbnew.SHAPE_T_SEGMENT)
            segment.SetStart(pcbnew.VECTOR2I(mm(start[0]), mm(start[1])))
            segment.SetEnd(pcbnew.VECTOR2I(mm(end[0]), mm(end[1])))
            segment.SetWidth(mm(width_mm))
            segment.SetLayer(pcbnew.Edge_Cuts)
            board.Add(segment)


def polygon_chain(pcbnew, points: list[dict]):
    chain = pcbnew.SHAPE_LINE_CHAIN()
    for point in points:
        chain.Append(pcbnew.VECTOR2I(mm(point["x"]), mm(point["y"])))
    chain.SetClosed(True)
    return chain


def add_layout_zones(board, pcbnew, spec: dict, net_info_by_name: dict) -> None:
    for item in spec.get("board", {}).get("copper_zones", []):
        zone = pcbnew.ZONE(board)
        zone.SetZoneName(str(item["id"]))
        zone.SetLayer(board.GetLayerID(str(item["layer"])))
        zone.SetNet(net_info_by_name[str(item["net"])])
        zone.AddPolygon(polygon_chain(pcbnew, item["polygon"]))
        island_mode = item.get("island_removal_mode")
        if island_mode is not None:
            island_modes = {
                "always": pcbnew.ISLAND_REMOVAL_MODE_ALWAYS,
                "area": pcbnew.ISLAND_REMOVAL_MODE_AREA,
                "never": pcbnew.ISLAND_REMOVAL_MODE_NEVER,
            }
            if island_mode not in island_modes:
                raise SystemExit(f"Unsupported island removal mode for zone {item['id']}: {island_mode}")
            zone.SetIslandRemovalMode(island_modes[island_mode])
        if "min_island_area_mm2" in item:
            area_mm2 = float(item["min_island_area_mm2"])
            if area_mm2 < 0:
                raise SystemExit(f"Zone {item['id']} min_island_area_mm2 must be non-negative")
            internal_units_per_mm = pcbnew.FromMM(1.0)
            zone.SetMinIslandArea(round(area_mm2 * internal_units_per_mm * internal_units_per_mm))
        board.Add(zone)

    for item in spec.get("layout", {}).get("keepouts", []):
        zone = pcbnew.ZONE(board)
        zone.SetZoneName(str(item["id"]))
        layers = pcbnew.LSET()
        for layer in item["layers"]:
            layers.AddLayer(board.GetLayerID(str(layer)))
        zone.SetLayerSet(layers)
        zone.SetIsRuleArea(True)
        restrictions = set(item.get("restrictions", []))
        zone.SetDoNotAllowTracks("tracks" in restrictions)
        zone.SetDoNotAllowVias("vias" in restrictions)
        zone.SetDoNotAllowZoneFills("copper" in restrictions)
        zone.SetDoNotAllowFootprints("footprints" in restrictions)
        zone.AddPolygon(polygon_chain(pcbnew, item["polygon"]))
        board.Add(zone)


def pad_center(footprints_by_ref: dict, ref: str, pad_number: str):
    footprint = footprints_by_ref[ref]
    for pad in footprint.Pads():
        if pad.GetNumber() == str(pad_number):
            return pad.GetCenter()
    raise SystemExit(f"Cannot find pad {ref}.{pad_number}")


def route_endpoint(footprints_by_ref: dict, endpoint: dict):
    return pad_center(footprints_by_ref, endpoint["ref"], str(endpoint["pad"]))


def add_routes(board, pcbnew, routes: list[dict], footprints_by_ref: dict, net_info_by_name: dict) -> None:
    for route in routes:
        net_info = net_info_by_name[route["net"]]
        points = [route_endpoint(footprints_by_ref, route["from"])]
        for waypoint in route.get("waypoints_mm", []):
            points.append(pcbnew.VECTOR2I(mm(waypoint["x"]), mm(waypoint["y"])))
        points.append(route_endpoint(footprints_by_ref, route["to"]))

        for start, end in zip(points, points[1:]):
            track = pcbnew.PCB_TRACK(board)
            track.SetStart(start)
            track.SetEnd(end)
            track.SetWidth(mm(route["width_mm"]))
            track.SetLayer(board.GetLayerID(route["layer"]))
            track.SetNet(net_info)
            board.Add(track)


def normalized_policy_key(value) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def configured_policy_key(value, choices, label: str) -> str:
    wanted = normalized_policy_key(value)
    matches = [str(choice) for choice in choices if normalized_policy_key(choice) == wanted]
    if len(matches) != 1:
        raise SystemExit(f"{label} is unsupported by the trusted routing-stage policy: {value}")
    return matches[0]


def enabled_copper_layers(board) -> dict[str, int]:
    return {
        str(board.GetLayerName(layer)): int(layer)
        for layer in board.GetEnabledLayers().CuStack()
    }


def configure_locked_via(board, pcbnew, item, record: dict, routing_policy: dict) -> None:
    enabled_layers = enabled_copper_layers(board)
    layers = record.get("layers")
    if (
        not isinstance(layers, list)
        or len(layers) != 2
        or len(set(map(str, layers))) != 2
        or any(str(layer) not in enabled_layers for layer in layers)
    ):
        raise SystemExit("Routing lock via layers must identify two distinct enabled copper layers")
    layer_names = [str(layer) for layer in layers]
    layer_positions = [list(enabled_layers).index(layer) for layer in layer_names]
    if layer_positions != sorted(layer_positions):
        raise SystemExit("Routing lock via layers must be ordered from top to bottom")

    via_policy = routing_policy.get("via_types")
    definitions = via_policy.get("definitions") if isinstance(via_policy, dict) else None
    if not isinstance(definitions, dict) or not definitions:
        raise SystemExit("Trusted routing-stage policy has no via type definitions")
    raw_via_type = record.get("via_type", via_policy.get("default_type"))
    via_type = configured_policy_key(raw_via_type, definitions, "Routing lock via_type")
    definition = definitions.get(via_type)
    enum_name = definition.get("pcbnew_enum") if isinstance(definition, dict) else None
    via_enum = getattr(pcbnew, str(enum_name), None) if isinstance(enum_name, str) else None
    if via_enum is None:
        raise SystemExit(f"Routing lock via_type {via_type} has no available pcbnew enum")
    if definition.get("full_span_required") is True and layer_positions != [0, len(enabled_layers) - 1]:
        raise SystemExit(f"Routing lock via_type {via_type} requires the full copper stack")
    expected_outer_count = definition.get("outer_endpoint_count")
    if isinstance(expected_outer_count, int) and not isinstance(expected_outer_count, bool):
        actual_outer_count = sum(
            position in {0, len(enabled_layers) - 1} for position in layer_positions
        )
        if actual_outer_count != expected_outer_count:
            raise SystemExit(f"Routing lock via_type {via_type} has invalid outer-layer endpoints")
    if definition.get("adjacent_layers_required") is True and layer_positions[1] - layer_positions[0] != 1:
        raise SystemExit(f"Routing lock via_type {via_type} requires adjacent copper layers")

    item.SetViaType(via_enum)
    item.SetLayerPair(enabled_layers[layer_names[0]], enabled_layers[layer_names[1]])
    actual_layers = [str(board.GetLayerName(item.TopLayer())), str(board.GetLayerName(item.BottomLayer()))]
    if int(item.GetViaType()) != int(via_enum) or actual_layers != layer_names:
        raise SystemExit(
            f"KiCad did not preserve routing lock via technology {via_type} on {layer_names}"
        )

    backdrill_policy = routing_policy.get("backdrill")
    if not isinstance(backdrill_policy, dict):
        raise SystemExit("Trusted routing-stage policy has no backdrill definition")
    mode_enums = backdrill_policy.get("mode_enums")
    mode_sides = backdrill_policy.get("mode_sides")
    if not isinstance(mode_enums, dict) or not isinstance(mode_sides, dict):
        raise SystemExit("Trusted routing-stage policy has incomplete backdrill definitions")
    backdrill = record.get("backdrill", {})
    if not isinstance(backdrill, dict):
        raise SystemExit("Routing lock via backdrill must be a mapping")
    raw_mode = backdrill.get("mode", backdrill_policy.get("default_mode"))
    mode = configured_policy_key(raw_mode, mode_enums, "Routing lock backdrill.mode")
    if mode not in mode_sides or not isinstance(mode_sides[mode], list):
        raise SystemExit(f"Trusted routing-stage policy has no side mapping for backdrill mode {mode}")
    backdrill_enum_name = mode_enums.get(mode)
    backdrill_enum = (
        getattr(pcbnew, str(backdrill_enum_name), None)
        if isinstance(backdrill_enum_name, str)
        else None
    )
    if backdrill_enum is None:
        raise SystemExit(f"Routing lock backdrill mode {mode} has no available pcbnew enum")
    active_sides = [str(side) for side in mode_sides[mode]]
    if active_sides and definition.get("backdrill_allowed") is not True:
        raise SystemExit(f"Routing lock via_type {via_type} does not allow backdrill")
    item.SetBackdrillMode(backdrill_enum)
    validation_rules = backdrill_policy.get("validation")
    if not isinstance(validation_rules, dict):
        raise SystemExit("Trusted routing-stage policy has no backdrill validation rules")
    stop_positions: dict[str, int] = {}
    for side in active_sides:
        detail = backdrill.get(side)
        if not isinstance(detail, dict):
            raise SystemExit(f"Routing lock backdrill.{side} must be a mapping")
        stop_layer = str(detail.get("stop_layer", ""))
        drill_mm = detail.get("drill_mm")
        if stop_layer not in enabled_layers:
            raise SystemExit(f"Routing lock backdrill.{side}.stop_layer is not enabled copper")
        stop_position = list(enabled_layers).index(stop_layer)
        stop_positions[side] = stop_position
        if (
            validation_rules.get("require_stop_between_via_endpoints") is True
            and not layer_positions[0] < stop_position < layer_positions[1]
        ):
            raise SystemExit(
                f"Routing lock backdrill.{side}.stop_layer must be between the via endpoints"
            )
        if (
            not isinstance(drill_mm, (int, float))
            or isinstance(drill_mm, bool)
            or float(drill_mm) <= 0
        ):
            raise SystemExit(f"Routing lock backdrill.{side}.drill_mm must be positive numeric")
        if (
            validation_rules.get("require_drill_larger_than_via_drill") is True
            and mm(drill_mm) <= int(item.GetDrillValue())
        ):
            raise SystemExit(
                f"Routing lock backdrill.{side}.drill_mm must exceed the via drill"
            )
        suffix = side[:1].upper() + side[1:]
        layer_setter = getattr(item, f"Set{suffix}BackdrillLayer", None)
        size_setter = getattr(item, f"Set{suffix}BackdrillSize", None)
        layer_getter = getattr(item, f"Get{suffix}BackdrillLayer", None)
        size_getter = getattr(item, f"Get{suffix}BackdrillSize", None)
        if not all(callable(method) for method in [layer_setter, size_setter, layer_getter, size_getter]):
            raise SystemExit(f"KiCad has no routing lock backdrill API for side {side}")
        layer_setter(enabled_layers[stop_layer])
        size_setter(mm(drill_mm))
        if int(layer_getter()) != enabled_layers[stop_layer] or int(size_getter()) != mm(drill_mm):
            raise SystemExit(f"KiCad did not preserve routing lock backdrill.{side}")
    if int(item.GetBackdrillMode()) != int(backdrill_enum):
        raise SystemExit(f"KiCad did not preserve routing lock backdrill mode {mode}")
    if (
        validation_rules.get("require_ordered_dual_stops") is True
        and {"top", "bottom"} <= set(stop_positions)
        and stop_positions["top"] >= stop_positions["bottom"]
    ):
        raise SystemExit("Routing lock top/bottom backdrill stop layers overlap or cross")


def add_route_lock(board, pcbnew, spec: dict, net_info_by_name: dict, root: Path) -> bool:
    lock = spec.get("routing", {}).get("route_lock", {})
    artifact = lock.get("artifact", {}) if isinstance(lock, dict) else {}
    configured = artifact.get("path") if isinstance(artifact, dict) else None
    if not configured:
        return False
    path = Path(str(configured))
    path = path if path.is_absolute() else root / path
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != artifact.get("sha256"):
        raise SystemExit(f"Routing lock artifact hash mismatch: {path}")
    data = json.loads(payload)
    if data.get("project_name") != spec["project"]["name"] or data.get("routing_revision") != spec["routing"].get("revision"):
        raise SystemExit("Routing lock artifact project/revision mismatch")
    try:
        routing_policy = load_routing_stage_policy()
    except (OSError, ValueError, yaml.YAMLError) as error:
        raise SystemExit(f"Cannot load trusted routing-stage policy: {error}") from error
    enabled_layers = enabled_copper_layers(board)
    for record in data.get("tracks", []):
        net = net_info_by_name.get(record.get("net"))
        if net is None:
            raise SystemExit(f"Routing lock references unknown net: {record.get('net')}")
        if record.get("type") in {"segment", "arc"}:
            item = pcbnew.PCB_ARC(board) if record.get("type") == "arc" else pcbnew.PCB_TRACK(board)
            item.SetStart(pcbnew.VECTOR2I(mm(record["start"][0]), mm(record["start"][1])))
            if record.get("type") == "arc":
                item.SetMid(pcbnew.VECTOR2I(mm(record["mid"][0]), mm(record["mid"][1])))
            item.SetEnd(pcbnew.VECTOR2I(mm(record["end"][0]), mm(record["end"][1])))
            item.SetWidth(mm(record["width_mm"]))
            layer_name = str(record.get("layer", ""))
            if layer_name not in enabled_layers:
                raise SystemExit(f"Routing lock track layer is not enabled copper: {layer_name}")
            item.SetLayer(enabled_layers[layer_name])
        elif record.get("type") == "via":
            item = pcbnew.PCB_VIA(board)
            item.SetPosition(pcbnew.VECTOR2I(mm(record["at"][0]), mm(record["at"][1])))
            item.SetWidth(mm(record["size_mm"]))
            item.SetDrill(mm(record["drill_mm"]))
            configure_locked_via(board, pcbnew, item, record, routing_policy)
        else:
            raise SystemExit("Routing lock contains unsupported track type")
        item.SetNet(net)
        board.Add(item)
    return True


def generate_board(spec: dict, output_dir: Path, include_routes: bool = True, root: Path | None = None) -> None:
    pcbnew = require_pcbnew()
    board = pcbnew.BOARD()
    try:
        copper_layers = validate_copper_layer_count(spec.get("board", {}).get("layers", {}).get("copper"))
    except ValueError as error:
        raise SystemExit(str(error)) from error
    board.SetCopperLayerCount(copper_layers)
    raw_stackup = spec.get("board", {}).get("stackup")
    stackup = {} if raw_stackup is None else raw_stackup
    if not isinstance(stackup, dict):
        raise SystemExit("board.stackup must be a mapping")
    board_thickness_mm = stackup.get("board_thickness_mm")
    if board_thickness_mm is not None:
        if (
            not isinstance(board_thickness_mm, (int, float))
            or isinstance(board_thickness_mm, bool)
            or board_thickness_mm <= 0
        ):
            raise SystemExit("board.stackup.board_thickness_mm must be a positive number")
        board.GetDesignSettings().SetBoardThickness(mm(board_thickness_mm))
    elif stackup.get("layers"):
        raise SystemExit("board.stackup.board_thickness_mm is required with physical stackup layers")
    net_info_by_name = {}

    net_names = {net["name"] for net in spec["nets"]}
    for component in electrical_components(spec):
        net_names.update(component.get("pads", {}).values())

    for net_name in sorted(net_names):
        net_info = pcbnew.NETINFO_ITEM(board, net_name)
        board.Add(net_info)
        net_info_by_name[net_name] = net_info

    footprints_by_ref = {}
    for component in board_footprint_items(spec):
        library, footprint_name = split_library_id(component["footprint"])
        footprint_dir = footprint_library_path(spec, library)
        footprint = pcbnew.FootprintLoad(str(footprint_dir), footprint_name)
        if footprint is None:
            raise SystemExit(f"Cannot load footprint {component['footprint']} from {footprint_dir}")

        board_variant = apply_board_silkscreen_policy(footprint, pcbnew, spec)

        position = component["position_mm"]
        footprint.SetReference(component["ref"])
        footprint.SetValue(component.get("value", component["ref"]))
        footprint.SetFPIDAsString("" if board_variant else component["footprint"])
        footprint.SetPosition(pcbnew.VECTOR2I(mm(position["x"]), mm(position["y"])))
        board.Add(footprint)
        side = str(position.get("side", component.get("side", "top"))).lower()
        if side == "bottom":
            footprint.Flip(footprint.GetPosition(), False)
        elif side != "top":
            raise SystemExit(f"Unsupported component side for {component['ref']}: {side}")
        footprint.SetOrientationDegrees(float(position.get("rotation", 0)))
        for pad in footprint.Pads():
            pad_number = pad.GetNumber()
            pad_map = component.get("pads", {})
            if pad_number in pad_map:
                pad.SetNet(net_info_by_name[pad_map[pad_number]])

        footprints_by_ref[component["ref"]] = footprint

    add_layout_zones(board, pcbnew, spec, net_info_by_name)
    if include_routes:
        if not add_route_lock(board, pcbnew, spec, net_info_by_name, root or Path.cwd()):
            add_routes(board, pcbnew, spec.get("routes", []), footprints_by_ref, net_info_by_name)

    add_edges(
        board,
        pcbnew,
        outline_loops(spec),
        spec["board"]["fabrication"]["edge_cut_width_mm"],
    )
    board.BuildListOfNets()
    board_path = output_dir / f"{spec['project']['name']}.kicad_pcb"
    board.Save(str(board_path))
    try:
        inject_physical_stackup(board_path, stackup)
    except ValueError as error:
        raise SystemExit(str(error)) from error


def symbol_instance(
    component: dict,
    x: float,
    y: float,
    project_name: str,
    layout: dict,
    offsets: dict,
    symbol_pins: dict[str, dict],
) -> str:
    component_uuid = uuid.uuid5(uuid.NAMESPACE_URL, component["ref"])
    path_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, component["ref"])
    font_size = layout["font_size_mm"]
    pin_lines = []
    for pin_number in sorted(symbol_pins.get(component["symbol"], {}).keys(), key=str):
        pin_uuid = uuid.uuid5(uuid.NAMESPACE_OID, f"{component['ref']}:{pin_number}")
        pin_lines.append(f'    (pin "{pin_number}" (uuid {pin_uuid}))')
    pin_block = "\n".join(pin_lines)
    return f"""  (symbol (lib_id "{sexpr_text(component["symbol"])}") (at {x:.2f} {y:.2f} 0) (unit 1)
    (in_bom yes) (on_board yes) (dnp no)
    (uuid {component_uuid})
    (property "Reference" "{sexpr_text(component["ref"])}" (at {x:.2f} {y + offsets["reference_y"]:.2f} 0)
      (effects (font (size {font_size} {font_size})))
    )
    (property "Value" "{sexpr_text(component["value"])}" (at {x:.2f} {y + offsets["value_y"]:.2f} 0)
      (effects (font (size {font_size} {font_size})))
    )
    (property "Footprint" "{sexpr_text(component["footprint"])}" (at {x:.2f} {y + offsets["footprint_y"]:.2f} 0)
      (effects (font (size {font_size} {font_size})) hide)
    )
    (property "Datasheet" "{sexpr_text(component["datasheet"])}" (at {x:.2f} {y + offsets["datasheet_y"]:.2f} 0)
      (effects (font (size {font_size} {font_size})) hide)
    )
{pin_block}
    (instances
      (project "{sexpr_text(project_name)}"
        (path "/{path_uuid}"
          (reference "{component["ref"]}") (unit 1)
        )
      )
    )
  )"""


def paren_delta(line: str) -> int:
    delta = 0
    in_string = False
    escaped = False
    for char in line:
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_string:
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "(":
            delta += 1
        elif char == ")":
            delta -= 1
    return delta


def extract_symbol_block(spec: dict, library_id: str) -> str:
    library, name = split_library_id(library_id)

    library_path = symbol_library_path(spec, library)
    lines = library_path.read_text(encoding="utf-8").splitlines()
    start = None
    needle = f'  (symbol "{name}"'
    for index, line in enumerate(lines):
        if line.startswith(needle):
            start = index
            break
    if start is None:
        raise SystemExit(f"Cannot find symbol {library_id} in {library_path}")

    block = []
    balance = 0
    for line in lines[start:]:
        block.append(line)
        balance += paren_delta(line)
        if block and balance == 0:
            break

    return "\n".join(block)


def inherited_symbol_library_id(spec: dict, library_id: str) -> str | None:
    library, _name = split_library_id(library_id)
    block = extract_symbol_block(spec, library_id)
    first_line = block.splitlines()[0] if block else ""
    match = re.search(r'\(extends\s+"([^"]+)"\)', first_line)
    return f"{library}:{match.group(1)}" if match else None


def apply_symbol_pin_type_overrides(spec: dict, library_id: str, text: str) -> str:
    pin_type_overrides = spec["kicad"].get("symbol_pin_types", {}).get(library_id, {})
    if not pin_type_overrides:
        return text

    pin_pattern = re.compile(
        r'(\(pin\s+)(\S+)(\s+\S+\s+\(at\s+[-\d.]+\s+[-\d.]+\s+[-\d.]+\).*?\(number\s+"([^"]+)")',
        re.S,
    )

    def replace(match: re.Match) -> str:
        pin_number = match.group(4)
        pin_type = pin_type_overrides.get(pin_number)
        if pin_type is None:
            return match.group(0)
        return f"{match.group(1)}{pin_type}{match.group(3)}"

    return pin_pattern.sub(replace, text)


def extract_library_symbol(spec: dict, library_id: str, collected: dict[str, str]) -> None:
    library, name = split_library_id(library_id)
    embedded_name = f"{library}:{name}"
    if embedded_name in collected:
        return

    parent_library_id = inherited_symbol_library_id(spec, library_id)
    if parent_library_id:
        extract_library_symbol(spec, parent_library_id, collected)

    text = extract_symbol_block(spec, library_id)
    text = apply_symbol_pin_type_overrides(spec, library_id, text)
    text = text.replace(f'(symbol "{name}"', f'(symbol "{embedded_name}"', 1)
    if parent_library_id:
        _parent_library, parent_name = split_library_id(parent_library_id)
        text = text.replace(f'(extends "{parent_name}")', f'(extends "{library}:{parent_name}")', 1)
    collected[embedded_name] = text


def symbol_pin_map(spec: dict) -> dict[str, dict]:
    pins_by_symbol: dict[str, dict] = {}
    pin_pattern = re.compile(
        r'\(pin\s+\S+\s+\S+\s+\(at\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\).*?\(number\s+"([^"]+)"',
        re.S,
    )
    def collect_pins(library_id: str, visiting: set[str] | None = None) -> dict:
        if library_id in pins_by_symbol:
            return pins_by_symbol[library_id]
        visiting = visiting or set()
        if library_id in visiting:
            raise SystemExit(f"Symbol inheritance cycle detected for {library_id}")
        visiting.add(library_id)
        parent_library_id = inherited_symbol_library_id(spec, library_id)
        pins = dict(collect_pins(parent_library_id, visiting)) if parent_library_id else {}
        block = extract_symbol_block(spec, library_id)
        for match in pin_pattern.finditer(block):
            x, y, angle, number = match.groups()
            pins[number] = {
                "x": float(x),
                "y": float(y),
                "angle": float(angle) % 360,
            }
        pins_by_symbol[library_id] = pins
        visiting.remove(library_id)
        return pins

    for component in electrical_components(spec):
        collect_pins(component["symbol"])
    return pins_by_symbol


def schematic_component_positions(spec: dict) -> dict[str, tuple[float, float]]:
    layout = spec["schematic"]["layout"]
    positions = {}
    for index, component in enumerate(electrical_components(spec)):
        if "schematic_position_mm" in component:
            x = component["schematic_position_mm"]["x"]
            y = component["schematic_position_mm"]["y"]
        else:
            x = layout["origin_x_mm"] + (index % layout["columns"]) * layout["column_spacing_mm"]
            y = layout["origin_y_mm"] + (index // layout["columns"]) * layout["row_spacing_mm"]
        positions[component["ref"]] = (x, y)
    return positions


def pin_connection_point(component: dict, component_positions: dict, symbol_pins: dict, pin_number: str) -> tuple[float, float, float]:
    pin = symbol_pins[component["symbol"]][str(pin_number)]
    origin_x, origin_y = component_positions[component["ref"]]
    return origin_x + pin["x"], origin_y - pin["y"], pin["angle"]


def outward_stub_end(x: float, y: float, angle: float, stub_mm: float) -> tuple[float, float, int]:
    rounded = int(angle) % 360
    if rounded == 0:
        return x - stub_mm, y, 0
    if rounded == 90:
        return x, y + stub_mm, 90
    if rounded == 180:
        return x + stub_mm, y, 180
    if rounded == 270:
        return x, y - stub_mm, 270
    raise SystemExit(f"Unsupported schematic pin angle for net label stub: {angle}")


def schematic_label(spec: dict, net_name: str, x: float, y: float, angle: int, label_uuid, font_size: float) -> str:
    scope = spec.get("schematic", {}).get("connectivity", {}).get("label_scope", "global")
    if scope == "global":
        configured_shape = spec.get("schematic", {}).get("connectivity", {}).get("default_label_shape", "passive")
        shape = next(
            (
                net.get("schematic_label_shape", configured_shape)
                for net in spec.get("nets", [])
                if isinstance(net, dict) and str(net.get("name")) == str(net_name)
            ),
            configured_shape,
        )
        if shape not in {"passive", "input", "output", "bidirectional", "tri_state"}:
            raise SystemExit(f"Unsupported schematic global label shape: {shape}")
        return f"""  (global_label "{sexpr_text(net_name)}" (shape {shape}) (at {x:.2f} {y:.2f} {angle})
    (effects (font (size {font_size} {font_size})) (justify left bottom))
    (uuid {label_uuid})
  )"""
    if scope == "local":
        return f"""  (label "{sexpr_text(net_name)}" (at {x:.2f} {y:.2f} {angle})
    (effects (font (size {font_size} {font_size})) (justify left bottom))
    (uuid {label_uuid})
  )"""
    raise SystemExit(f"Unsupported schematic label scope: {scope}")


def schematic_net_labels(spec: dict, component_positions: dict, symbol_pins: dict) -> str:
    layout = spec["schematic"]["layout"]
    font_size = layout["font_size_mm"]
    stub_mm = layout["label_stub_mm"]
    blocks = []
    for component in electrical_components(spec):
        for pin_number, net_name in component.get("pads", {}).items():
            if str(pin_number) not in symbol_pins.get(component["symbol"], {}):
                continue
            pin_x, pin_y, angle = pin_connection_point(component, component_positions, symbol_pins, str(pin_number))
            label_x, label_y, label_angle = outward_stub_end(pin_x, pin_y, angle, stub_mm)
            wire_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"wire:{component['ref']}:{pin_number}:{net_name}")
            label_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"label:{component['ref']}:{pin_number}:{net_name}")
            wire_block = f"""  (wire (pts (xy {pin_x:.2f} {pin_y:.2f}) (xy {label_x:.2f} {label_y:.2f}))
    (stroke (width 0) (type default))
    (uuid {wire_uuid})
  )"""
            blocks.append(wire_block + "\n" + schematic_label(spec, str(net_name), label_x, label_y, label_angle, label_uuid, font_size))
    return "\n\n".join(blocks)


def schematic_net_wires(spec: dict, component_positions: dict, symbol_pins: dict) -> str:
    pins_by_net: dict[str, list[tuple[str, str, float, float, float]]] = {}
    font_size = spec["schematic"]["layout"]["font_size_mm"]
    for component in electrical_components(spec):
        for pin_number, net_name in component.get("pads", {}).items():
            if str(pin_number) not in symbol_pins.get(component["symbol"], {}):
                continue
            pin_x, pin_y, angle = pin_connection_point(component, component_positions, symbol_pins, str(pin_number))
            pins_by_net.setdefault(net_name, []).append((component["ref"], str(pin_number), pin_x, pin_y, angle))

    blocks = []
    for net_name in sorted(pins_by_net):
        pins = sorted(pins_by_net[net_name], key=lambda item: (item[0], item[1]))
        if len(pins) < 2:
            continue
        source = pins[0]
        for target in pins[1:]:
            x1, y1 = source[2], source[3]
            x2, y2 = target[2], target[3]
            points = [(x1, y1), (x1, y2), (x2, y2)]
            for index, (start, end) in enumerate(zip(points, points[1:])):
                if start == end:
                    continue
                wire_uuid = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"schematic-wire:{net_name}:{source[0]}:{source[1]}:{target[0]}:{target[1]}:{index}",
                )
                blocks.append(
                    f"""  (wire (pts (xy {start[0]:.2f} {start[1]:.2f}) (xy {end[0]:.2f} {end[1]:.2f}))
    (stroke (width 0) (type default))
    (uuid {wire_uuid})
  )"""
                )
        label_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"schematic-wire-label:{net_name}")
        blocks.append(
            schematic_label(spec, str(net_name), source[2], source[3], int(source[4]) % 360, label_uuid, font_size)
        )
    return "\n\n".join(blocks)


def embedded_lib_symbols(spec: dict) -> str:
    collected: dict[str, str] = {}
    for library_id in sorted(used_symbol_library_ids(spec)):
        extract_library_symbol(spec, library_id, collected)
    if not collected:
        return "  (lib_symbols)"
    body = "\n".join(collected[name] for name in sorted(collected))
    indented = "\n".join(f"  {line}" for line in body.splitlines())
    return f"  (lib_symbols\n{indented}\n  )"


def schematic_no_connects(spec: dict, component_positions: dict, symbol_pins: dict) -> str:
    components_by_ref = {component["ref"]: component for component in electrical_components(spec)}
    blocks = []
    for entry in spec.get("schematic", {}).get("no_connects", []):
        component = components_by_ref[entry["ref"]]
        for pin_number in entry.get("pins", []):
            pin_x, pin_y, _angle = pin_connection_point(component, component_positions, symbol_pins, str(pin_number))
            marker_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"no-connect:{component['ref']}:{pin_number}")
            blocks.append(f"  (no_connect (at {pin_x:.2f} {pin_y:.2f}) (uuid {marker_uuid}))")
    return "\n".join(blocks)


def power_flag_instance(flag: dict, index: int, project_name: str, layout: dict) -> str:
    position = flag["position_mm"]
    x = float(position["x"])
    y = float(position["y"])
    ref = flag.get("ref", f"#FLG{index:02d}")
    value = flag.get("value", "PWR_FLAG")
    symbol = flag["symbol"]
    flag_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"power-flag:{project_name}:{ref}:{flag['net']}")
    path_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, f"power-flag:{project_name}:{ref}:{flag['net']}")
    pin_uuid = uuid.uuid5(uuid.NAMESPACE_OID, f"power-flag:{project_name}:{ref}:1")
    font_size = layout["font_size_mm"]
    return f"""  (symbol (lib_id "{sexpr_text(symbol)}") (at {x:.2f} {y:.2f} 0) (unit 1)
    (in_bom yes) (on_board yes) (dnp no)
    (uuid {flag_uuid})
    (property "Reference" "{sexpr_text(ref)}" (at {x:.2f} {y + 1.91:.2f} 0)
      (effects (font (size {font_size} {font_size})) hide)
    )
    (property "Value" "{sexpr_text(value)}" (at {x:.2f} {y + 3.81:.2f} 0)
      (effects (font (size {font_size} {font_size})))
    )
    (property "Footprint" "" (at {x:.2f} {y:.2f} 0)
      (effects (font (size {font_size} {font_size})) hide)
    )
    (property "Datasheet" "~" (at {x:.2f} {y:.2f} 0)
      (effects (font (size {font_size} {font_size})) hide)
    )
    (pin "1" (uuid {pin_uuid}))
    (instances
      (project "{sexpr_text(project_name)}"
        (path "/{path_uuid}"
          (reference "{sexpr_text(ref)}") (unit 1)
        )
      )
    )
  )"""


def schematic_power_flags(spec: dict, project_name: str, layout: dict) -> str:
    blocks = []
    stub_mm = spec.get("schematic", {}).get("power_flag_stub_mm", layout.get("label_stub_mm", 2.54))
    font_size = layout["font_size_mm"]
    for index, flag in enumerate(spec.get("schematic", {}).get("power_flags", []), start=1):
        position = flag["position_mm"]
        x = float(position["x"])
        y = float(position["y"])
        net_name = flag["net"]
        label_y = y - float(stub_mm)
        wire_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"power-flag-wire:{project_name}:{flag.get('ref', index)}:{net_name}")
        label_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"power-flag-label:{project_name}:{flag.get('ref', index)}:{net_name}")
        blocks.append(power_flag_instance(flag, index, project_name, layout))
        wire_block = f"""  (wire (pts (xy {x:.2f} {y:.2f}) (xy {x:.2f} {label_y:.2f}))
    (stroke (width 0) (type default))
    (uuid {wire_uuid})
  )"""
        blocks.append(wire_block + "\n" + schematic_label(spec, str(net_name), x, label_y, 90, label_uuid, font_size))
    return "\n\n".join(blocks)


def generate_schematic(spec: dict, output_dir: Path) -> None:
    project_name = spec["project"]["name"]
    title = spec["project"]["title"]
    layout = spec["schematic"]["layout"]
    offsets = spec["schematic"]["property_offsets_mm"]
    symbol_pins = symbol_pin_map(spec)
    component_positions = schematic_component_positions(spec)
    symbol_lines = []
    for component in electrical_components(spec):
        x, y = component_positions[component["ref"]]
        symbol_lines.append(symbol_instance(component, x, y, project_name, layout, offsets, symbol_pins))

    notes = "\n".join(
        f'  (text "{sexpr_text(note)}" (at {layout["note_origin_x_mm"]:.2f} {layout["note_origin_y_mm"] + index * layout["note_spacing_mm"]:.2f} 0)\n'
        f'    (effects (font (size {layout["font_size_mm"]} {layout["font_size_mm"]})) (justify left bottom))\n'
        '  )'
        for index, note in enumerate(spec.get("schematic_notes", []))
    )

    lib_symbols = embedded_lib_symbols(spec)
    connectivity = spec["schematic"].get("connectivity", {})
    connectivity_mode = connectivity.get("mode", "wires")
    if connectivity_mode == "wires":
        net_connectivity_lines = schematic_net_wires(spec, component_positions, symbol_pins)
    elif connectivity_mode == "net_labels":
        net_connectivity_lines = schematic_net_labels(spec, component_positions, symbol_pins)
    else:
        raise SystemExit(f"Unsupported schematic connectivity mode: {connectivity_mode}")
    no_connect_lines = schematic_no_connects(spec, component_positions, symbol_pins)
    power_flag_lines = schematic_power_flags(spec, project_name, layout)
    schematic = f"""(kicad_sch (version {spec["project"]["schematic_version"]}) (generator "{sexpr_text(spec["project"]["generated_by"])}")

  (uuid {uuid.uuid5(uuid.NAMESPACE_URL, project_name)})

  (paper "{sexpr_text(spec["project"]["pcb_page_size"])}")

  (title_block
    (title "{sexpr_text(title)}")
  )

{lib_symbols}

{notes}

{net_connectivity_lines}

{no_connect_lines}

{power_flag_lines}

{chr(10).join(symbol_lines)}

  (sheet_instances
    (path "/" (page "1"))
  )
)
"""
    (output_dir / f"{project_name}.kicad_sch").write_text(schematic, encoding="utf-8")


def generate_bom(spec: dict, output_dir: Path) -> None:
    path = output_dir / "bom.csv"
    fields = ["ref", "value", "symbol", "footprint", "datasheet", "status"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for component in spec["components"]:
            writer.writerow({field: component.get(field, "") for field in fields})


def generate_todo(spec: dict, output_dir: Path) -> None:
    lines = [
        f"# {spec['project']['title']} TODO",
        "",
        "这些项目必须在量产前关闭；未关闭时不能声明该设计可生产。",
        "",
    ]
    for item in spec.get("todo", []):
        lines.append(f"- {item}")
    lines.append("")
    (output_dir / "TODO.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Generate Spec-bound KiCad project artifacts.")
    parser.add_argument("spec", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--schematic-only", action="store_true")
    mode.add_argument("--board-only", action="store_true")
    mode.add_argument("--layout-only", action="store_true")
    args = parser.parse_args(argv[1:])

    spec_path = args.spec
    spec = load_spec(spec_path)
    output_dir = Path(spec["project"]["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    write_library_tables(spec, output_dir)
    if not args.board_only and not args.layout_only:
        generate_schematic(spec, output_dir)
    if not args.schematic_only:
        generate_board(spec, output_dir, include_routes=not args.layout_only, root=Path.cwd())
    build_project_file(spec, output_dir)
    write_custom_drc_rules(spec, output_dir)
    write_library_audit(spec, output_dir)
    generate_bom(spec, output_dir)
    generate_todo(spec, output_dir)

    generated_scope = "schematic" if args.schematic_only else "layout" if args.layout_only else "board" if args.board_only else "project"
    print(f"Generated KiCad {generated_scope} artifacts in {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
