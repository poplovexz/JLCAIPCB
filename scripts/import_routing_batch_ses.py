#!/usr/bin/env python3
"""Import only routed copper from a Freerouting SES into an isolated KiCad PCB."""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path
from typing import Any

import pcbnew
import yaml

from pcb_technology import routing_stage_policy_path


TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:\\.|[^"\\])*)"|([^\s()]+))')
VIA_NAME = re.compile(r"_([0-9.]+):([0-9.]+)_um$")


def mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def string_list(value: Any) -> list[str]:
    return [str(item) for item in value if isinstance(item, str) and item.strip()] if isinstance(value, list) else []


def normalized(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def canonical_key(value: Any, choices: Any) -> str | None:
    wanted = normalized(value)
    return next((str(choice) for choice in choices if normalized(choice) == wanted), None)


def load_routing_policy(path: Path | None = None) -> dict[str, Any]:
    path = path or routing_stage_policy_path()
    policy = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(policy, dict):
        raise ValueError(f"routing policy must be a mapping: {path}")
    return policy


def via_type_definitions(policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(name): mapping(value)
        for name, value in mapping(mapping(policy.get("via_types")).get("definitions")).items()
    }


def default_via_type(policy: dict[str, Any]) -> str:
    definitions = via_type_definitions(policy)
    configured = canonical_key(mapping(policy.get("via_types")).get("default_type"), definitions)
    if configured is None:
        raise ValueError("routing policy via_types.default_type is not configured")
    return configured


def pcbnew_enum(policy_value: Any, label: str) -> int:
    enum_name = policy_value.strip() if isinstance(policy_value, str) else ""
    enum_value = getattr(pcbnew, enum_name, None) if enum_name else None
    if enum_value is None:
        raise ValueError(f"routing policy {label} has an unavailable pcbnew enum")
    return int(enum_value)


def configured_backdrill_default(policy: dict[str, Any]) -> int:
    backdrill = mapping(policy.get("backdrill"))
    mode_enums = mapping(backdrill.get("mode_enums"))
    mode_sides = mapping(backdrill.get("mode_sides"))
    if set(mode_enums) != set(mode_sides):
        raise ValueError("routing policy backdrill mode_enums and mode_sides must have identical keys")
    default_mode = canonical_key(backdrill.get("default_mode"), mode_enums)
    if default_mode is None:
        raise ValueError("routing policy backdrill.default_mode is not configured")
    if string_list(mode_sides.get(default_mode)):
        raise ValueError("routing policy backdrill.default_mode must not enable a backdrill side")
    return pcbnew_enum(mode_enums[default_mode], f"backdrill.mode_enums.{default_mode}")


def board_copper_layers(board) -> list[str]:
    return [str(board.GetLayerName(layer_id)) for layer_id in board.GetEnabledLayers().CuStack()]


def validate_via_span(via_type: str, endpoints: list[str], copper_layers: list[str], definition: dict[str, Any]) -> None:
    positions = [copper_layers.index(layer) for layer in endpoints]
    if definition.get("full_span_required") is True and positions != [0, len(copper_layers) - 1]:
        raise ValueError(f"via type {via_type} requires the full copper stack")
    expected_outer_count = definition.get("outer_endpoint_count")
    if isinstance(expected_outer_count, int) and not isinstance(expected_outer_count, bool):
        actual_outer_count = sum(position in {0, len(copper_layers) - 1} for position in positions)
        if actual_outer_count != expected_outer_count:
            raise ValueError(f"via type {via_type} has invalid outer-layer endpoints")
    if definition.get("adjacent_layers_required") is True and positions[1] - positions[0] != 1:
        raise ValueError(f"via type {via_type} requires adjacent copper layers")


def resolve_via_type(
    definition: dict[str, Any],
    board,
    policy: dict[str, Any],
    net_constraint: dict[str, Any] | None,
) -> tuple[str, int, int]:
    copper_layers = board_copper_layers(board)
    padstack_layers = string_list(definition.get("layers"))
    if len(padstack_layers) < 2 or len(padstack_layers) != len(set(padstack_layers)):
        raise ValueError("SES via padstack must identify at least two distinct copper layers")
    if any(layer not in copper_layers for layer in padstack_layers):
        raise ValueError(f"SES via padstack uses a layer outside the enabled copper stack: {padstack_layers}")
    positions = [copper_layers.index(layer) for layer in padstack_layers]
    if positions != list(range(positions[0], positions[-1] + 1)):
        raise ValueError(f"SES via padstack layers must form an ordered contiguous copper span: {padstack_layers}")
    endpoints = [padstack_layers[0], padstack_layers[-1]]
    definitions = via_type_definitions(policy)
    default_type = default_via_type(policy)
    is_full_span = positions == list(range(len(copper_layers)))
    if is_full_span:
        full_span_defaults = [name for name, item in definitions.items() if item.get("ses_full_span_default") is True]
        if full_span_defaults != [default_type]:
            raise ValueError("routing policy must define exactly one SES full-span default matching via_types.default_type")
        via_type = default_type
    else:
        if net_constraint is None:
            raise ValueError(f"non-full-span SES via {endpoints} requires a net constraint technology mapping")
        matches = []
        for entry in net_constraint.get("via_type_by_layer_pair", []):
            if isinstance(entry, dict) and string_list(entry.get("layers")) == endpoints:
                matches.append(entry)
        if len(matches) != 1:
            raise ValueError(f"non-full-span SES via {endpoints} requires exactly one via_type_by_layer_pair mapping")
        via_type = canonical_key(matches[0].get("via_type"), definitions)
        if via_type is None:
            raise ValueError(f"non-full-span SES via {endpoints} maps to an unsupported via type")
    allowed_values = string_list(net_constraint.get("allowed_via_types")) if net_constraint is not None and "allowed_via_types" in net_constraint else [default_type]
    allowed_types = {canonical_key(value, definitions) for value in allowed_values}
    if None in allowed_types or via_type not in allowed_types:
        raise ValueError(f"SES via type {via_type} is outside the net constraint allowed_via_types")
    if net_constraint is not None and mapping(net_constraint.get("backdrill")).get("required") is True:
        raise ValueError("SES import cannot satisfy a required backdrill constraint")
    via_definition = definitions[via_type]
    validate_via_span(via_type, endpoints, copper_layers, via_definition)
    return via_type, pcbnew_enum(via_definition.get("pcbnew_enum"), f"via_types.definitions.{via_type}.pcbnew_enum"), configured_backdrill_default(policy)


def parse_sexpr(text: str) -> list[Any]:
    root: list[Any] = []
    stack = [root]
    position = 0
    while position < len(text):
        match = TOKEN.match(text, position)
        if not match:
            if text[position:].strip():
                raise ValueError(f"invalid SES token near offset {position}")
            break
        position = match.end()
        opening, closing, quoted, bare = match.groups()
        if opening:
            item: list[Any] = []
            stack[-1].append(item)
            stack.append(item)
        elif closing:
            if len(stack) == 1:
                raise ValueError("unexpected closing parenthesis in SES")
            stack.pop()
        elif quoted is not None:
            stack[-1].append(bytes(quoted, "utf-8").decode("unicode_escape"))
        elif bare is not None:
            stack[-1].append(bare)
    if len(stack) != 1:
        raise ValueError("unterminated SES expression")
    return root


def child(node: list[Any], name: str) -> list[Any] | None:
    return next((item for item in node[1:] if isinstance(item, list) and item and item[0] == name), None)


def children(node: list[Any], name: str) -> list[list[Any]]:
    return [item for item in node[1:] if isinstance(item, list) and item and item[0] == name]


def numeric(value: Any, label: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid numeric {label}: {value}") from error


def coordinate_scale(routes: list[Any]) -> float:
    resolution = child(routes, "resolution")
    if resolution is None or len(resolution) != 3:
        raise ValueError("SES routes.resolution is required")
    units = str(resolution[1])
    per_unit = numeric(resolution[2], "resolution")
    unit_per_mm = {"um": 1000.0, "mm": 1.0, "mil": 1000.0 / 25.4}.get(units)
    if unit_per_mm is None or per_unit <= 0:
        raise ValueError(f"unsupported SES resolution: {resolution[1:]}")
    return 1.0 / (per_unit * unit_per_mm)


def via_definitions(routes: list[Any], scale: float) -> dict[str, dict[str, Any]]:
    library = child(routes, "library_out")
    if library is None:
        raise ValueError("SES routes.library_out is required")
    definitions: dict[str, dict[str, Any]] = {}
    for padstack in children(library, "padstack"):
        if len(padstack) < 2:
            continue
        identifier = str(padstack[1])
        shapes = []
        for shape_wrapper in children(padstack, "shape"):
            circle = child(shape_wrapper, "circle")
            if circle is not None and len(circle) >= 3:
                shapes.append({"layer": str(circle[1]), "diameter_mm": numeric(circle[2], "via diameter") * scale})
        match = VIA_NAME.search(identifier)
        if not shapes or match is None:
            continue
        definitions[identifier] = {
            "diameter_mm": max(item["diameter_mm"] for item in shapes),
            "drill_mm": float(match.group(2)) / 1000.0,
            "layers": [item["layer"] for item in shapes],
        }
    return definitions


def add_path(board, net, path: list[Any], scale: float) -> int:
    if len(path) < 7 or (len(path) - 3) % 2:
        raise ValueError(f"invalid SES path for net {net.GetNetname()}")
    layer = board.GetLayerID(str(path[1]))
    if layer == pcbnew.UNDEFINED_LAYER or not board.IsLayerEnabled(layer):
        raise ValueError(f"SES path uses an unavailable layer: {path[1]}")
    width = pcbnew.FromMM(numeric(path[2], "path width") * scale)
    points = [
        pcbnew.VECTOR2I(
            pcbnew.FromMM(numeric(path[index], "path x") * scale),
            pcbnew.FromMM(-numeric(path[index + 1], "path y") * scale),
        )
        for index in range(3, len(path), 2)
    ]
    count = 0
    for start, end in zip(points, points[1:]):
        if start == end:
            continue
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(start)
        track.SetEnd(end)
        track.SetWidth(width)
        track.SetLayer(layer)
        track.SetNet(net)
        board.Add(track)
        count += 1
    return count


def add_via(
    board,
    net,
    via: list[Any],
    definitions: dict[str, dict[str, Any]],
    scale: float,
    policy: dict[str, Any],
    net_constraint: dict[str, Any] | None = None,
) -> None:
    if len(via) < 4:
        raise ValueError(f"invalid SES via for net {net.GetNetname()}")
    identifier = str(via[1])
    definition = definitions.get(identifier)
    if definition is None:
        raise ValueError(f"SES via references unknown padstack: {identifier}")
    layers = definition["layers"]
    via_type, via_type_enum, default_backdrill_enum = resolve_via_type(definition, board, policy, net_constraint)
    layer_ids = [board.GetLayerID(layer) for layer in layers]
    if any(layer_id == pcbnew.UNDEFINED_LAYER or not board.IsLayerEnabled(layer_id) for layer_id in layer_ids):
        raise ValueError(f"SES via uses an unavailable PCB copper layer: {layers}")
    item = pcbnew.PCB_VIA(board)
    item.SetPosition(
        pcbnew.VECTOR2I(
            pcbnew.FromMM(numeric(via[2], "via x") * scale),
            pcbnew.FromMM(-numeric(via[3], "via y") * scale),
        )
    )
    item.SetWidth(pcbnew.FromMM(definition["diameter_mm"]))
    item.SetDrill(pcbnew.FromMM(definition["drill_mm"]))
    item.SetViaType(via_type_enum)
    item.SetLayerPair(layer_ids[0], layer_ids[-1])
    item.SetBackdrillMode(default_backdrill_enum)
    item.SetNet(net)
    board.Add(item)
    actual_type = int(item.GetViaType())
    actual_layers = [str(board.GetLayerName(item.TopLayer())), str(board.GetLayerName(item.BottomLayer()))]
    if actual_type != via_type_enum or actual_layers != [layers[0], layers[-1]]:
        board.Remove(item)
        raise ValueError(f"pcbnew did not preserve SES via technology {via_type} on layers {[layers[0], layers[-1]]}")


def import_copper(
    board,
    session_path: Path,
    allowed_nets: set[str] | None = None,
    net_constraints: dict[str, dict[str, Any]] | None = None,
    policy: dict[str, Any] | None = None,
) -> tuple[int, int]:
    routing_policy = policy if policy is not None else load_routing_policy()
    parsed = parse_sexpr(session_path.read_text(encoding="utf-8"))
    if len(parsed) != 1 or not isinstance(parsed[0], list) or not parsed[0] or parsed[0][0] != "session":
        raise ValueError("SES root session section is required")
    routes = child(parsed[0], "routes")
    if routes is None:
        raise ValueError("SES routes section is required")
    scale = coordinate_scale(routes)
    definitions = via_definitions(routes, scale)
    network = child(routes, "network_out")
    if network is None:
        raise ValueError("SES routes.network_out is required")
    segments = 0
    vias = 0
    for net_node in children(network, "net"):
        if len(net_node) < 2:
            raise ValueError("SES network_out net name is required")
        net_name = str(net_node[1])
        if allowed_nets is not None and net_name not in allowed_nets:
            continue
        net = board.FindNet(net_name)
        if net is None:
            raise ValueError(f"SES references unknown PCB net: {net_name}")
        for wire in children(net_node, "wire"):
            wire_type = child(wire, "type")
            if allowed_nets is None and wire_type is not None and len(wire_type) > 1 and str(wire_type[1]) == "protect":
                continue
            path = child(wire, "path")
            if path is not None and len(path) > 3:
                segments += add_path(board, net, path, scale)
        for via in children(net_node, "via"):
            add_via(board, net, via, definitions, scale, routing_policy, mapping(net_constraints.get(net_name)) if net_constraints is not None else None)
            vias += 1
    board.BuildConnectivity()
    return segments, vias


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--batch-id")
    parser.add_argument("--routing-policy", type=Path)
    args = parser.parse_args()
    try:
        if bool(args.spec) != bool(args.batch_id):
            raise ValueError("--spec and --batch-id must be provided together")
        policy = load_routing_policy(args.routing_policy)
        allowed_nets: set[str] | None = None
        net_constraints: dict[str, dict[str, Any]] | None = None
        if args.spec:
            spec = yaml.safe_load(args.spec.read_text(encoding="utf-8")) or {}
            batches = spec.get("routing", {}).get("batches", [])
            batch = next((item for item in batches if isinstance(item, dict) and item.get("id") == args.batch_id), None)
            if batch is None:
                raise ValueError(f"routing batch does not exist: {args.batch_id}")
            allowed_nets = {str(item) for item in batch.get("nets", [])}
            if not allowed_nets:
                raise ValueError(f"routing batch has no nets: {args.batch_id}")
            raw_constraints = mapping(mapping(spec.get("routing")).get("net_constraints"))
            net_constraints = {}
            for net_name in allowed_nets:
                constraint = raw_constraints.get(net_name)
                if not isinstance(constraint, dict):
                    raise ValueError(f"routing net constraint does not exist: {net_name}")
                net_constraints[net_name] = constraint
        board = pcbnew.LoadBoard(str(args.input))
        if board is None:
            raise ValueError(f"cannot load PCB: {args.input}")
        segments, vias = import_copper(board, args.session, allowed_nets, net_constraints, policy)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        board.Save(str(args.output))
        input_rules = args.input.with_suffix(".kicad_dru")
        if input_rules.is_file():
            shutil.copy2(input_rules, args.output.with_suffix(".kicad_dru"))
        if not args.output.is_file():
            raise ValueError(f"candidate PCB was not written: {args.output}")
    except Exception as error:
        print(f"FAIL: {error}")
        return 1
    print(f"PASS: imported {segments} segment(s) and {vias} via(s) to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
