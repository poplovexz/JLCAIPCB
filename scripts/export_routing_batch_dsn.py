#!/usr/bin/env python3
"""Export a Specctra DSN containing one planned routing batch plus locked copper."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pcbnew
import yaml

from pcb_technology import validate_copper_layer_count


def mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def sequence(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def mm(value: Any) -> int:
    return pcbnew.FromMM(float(value))


def load_spec(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("Spec must contain a YAML mapping")
    return data


def spec_copper_layer_count(spec: dict[str, Any]) -> int:
    value = mapping(mapping(spec.get("board")).get("layers")).get("copper")
    return validate_copper_layer_count(value)


def enabled_copper_layer_names(board) -> list[str]:
    return [str(board.GetLayerName(layer)) for layer in board.GetEnabledLayers().CuStack()]


def validate_board_copper_layers(board, expected_count: int) -> list[str]:
    actual_count = int(board.GetCopperLayerCount())
    if actual_count != expected_count:
        raise ValueError(
            f"PCB copper layer count {actual_count} does not match spec board.layers.copper {expected_count}"
        )
    enabled = enabled_copper_layer_names(board)
    if len(enabled) != actual_count:
        raise ValueError(
            f"PCB reports {actual_count} copper layers but has {len(enabled)} enabled copper layers"
        )
    return enabled


def batch_nets(spec: dict[str, Any], batch_id: str) -> tuple[set[str], set[str]]:
    batches = [item for item in sequence(mapping(spec.get("routing")).get("batches")) if isinstance(item, dict)]
    selected = next((item for item in batches if item.get("id") == batch_id), None)
    if selected is None:
        raise ValueError(f"unknown routing batch: {batch_id}")
    if selected.get("state") != "planned":
        raise ValueError(f"routing batch {batch_id} is not planned")
    selected_nets = {str(net) for net in sequence(selected.get("nets"))}
    locked_nets = {
        str(net) for item in batches if item.get("state") == "locked"
        for net in sequence(item.get("nets"))
    }
    return selected_nets, locked_nets


def configure_netclasses(board, spec: dict[str, Any], selected: set[str], locked: set[str], locked_class: str) -> None:
    settings = board.GetDesignSettings().m_NetSettings
    settings.ClearNetclassPatternAssignments()
    settings.ClearNetclassLabelAssignments()
    board_classes = {
        str(item["name"]): item for item in sequence(mapping(spec.get("board")).get("net_classes"))
        if isinstance(item, dict) and item.get("name")
    }
    for name, item in board_classes.items():
        netclass = pcbnew.NETCLASS(name)
        netclass.SetClearance(mm(item["clearance_mm"]))
        netclass.SetTrackWidth(mm(item["track_width_mm"]))
        netclass.SetViaDiameter(mm(item["via_diameter_mm"]))
        netclass.SetViaDrill(mm(item["via_drill_mm"]))
        if name == pcbnew.NETCLASS.Default:
            settings.SetDefaultNetclass(netclass)
        else:
            settings.SetNetclass(name, netclass)
    if locked:
        locked_rule = mapping(spec.get("routing", {})).get("locked_net_class")
        if not isinstance(locked_rule, dict):
            raise ValueError("routing.locked_net_class is required when locked batches exist")
        netclass = pcbnew.NETCLASS(locked_class)
        netclass.SetClearance(mm(locked_rule["clearance_mm"]))
        netclass.SetTrackWidth(mm(locked_rule["track_width_mm"]))
        netclass.SetViaDiameter(mm(locked_rule["via_diameter_mm"]))
        netclass.SetViaDrill(mm(locked_rule["via_drill_mm"]))
        settings.SetNetclass(locked_class, netclass)
    known = set(board_classes)
    fallback = str(mapping(spec.get("routing")).get("fallback_net_class", pcbnew.NETCLASS.Default))
    if fallback not in known:
        raise ValueError(f"routing fallback net class is not declared: {fallback}")
    constraints = mapping(mapping(spec.get("routing")).get("net_constraints"))
    for net in sorted(selected):
        board_class = str(mapping(constraints.get(net)).get("net_class", fallback))
        if board_class not in known:
            raise ValueError(f"routing constraint for {net} references unknown net class {board_class}")
        settings.SetNetclassPatternAssignment(net, board_class)
    for net in sorted(locked):
        settings.SetNetclassPatternAssignment(net, locked_class)
    settings.RecomputeEffectiveNetclasses()


def configure_routing_layers(
    board,
    spec: dict[str, Any],
    selected: set[str],
    enabled_copper_layers: list[str],
) -> None:
    constraints = mapping(mapping(spec.get("routing")).get("net_constraints"))
    allowed: set[str] = set()
    for net in sorted(selected):
        configured = mapping(constraints.get(net)).get("allowed_layers")
        if not isinstance(configured, list) or not configured or any(not isinstance(layer, str) for layer in configured):
            raise ValueError(f"routing constraint for {net} must declare non-empty allowed_layers")
        allowed.update(configured)
    unavailable = sorted(allowed - set(enabled_copper_layers))
    if unavailable:
        raise ValueError(f"routing allowed layers are not enabled copper layers: {', '.join(unavailable)}")
    for name in enabled_copper_layers:
        layer = board.GetLayerID(name)
        board.SetLayerType(layer, pcbnew.LT_SIGNAL if name in allowed else pcbnew.LT_POWER)


def export_batch(
    spec: dict[str, Any],
    batch_id: str,
    input_path: Path,
    output_path: Path,
    locked_class: str,
    isolate_locked_pads: bool,
    omit_locked_copper: bool,
) -> None:
    copper_layer_count = spec_copper_layer_count(spec)
    selected, locked = batch_nets(spec, batch_id)
    if omit_locked_copper:
        locked = set()
    board = pcbnew.LoadBoard(str(input_path))
    if board is None:
        raise ValueError(f"cannot load PCB: {input_path}")
    enabled_copper_layers = validate_board_copper_layers(board, copper_layer_count)
    configure_netclasses(board, spec, selected, locked, locked_class)
    configure_routing_layers(board, spec, selected, enabled_copper_layers)
    retained = selected | locked
    for footprint in board.GetFootprints():
        for pad in footprint.Pads():
            net_name = str(pad.GetNetname())
            if net_name not in retained or (isolate_locked_pads and net_name in locked):
                pad.SetNetCode(0)
    for zone in board.Zones():
        if not zone.GetIsRuleArea() and str(zone.GetNetname()) not in retained:
            zone.SetNetCode(0)
    for item in list(board.GetTracks()):
        if str(item.GetNetname()) not in retained:
            board.Remove(item)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not pcbnew.ExportSpecctraDSN(board, str(output_path)) or not output_path.is_file():
        raise ValueError(f"failed to export Specctra DSN: {output_path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--locked-class", required=True)
    parser.add_argument("--isolate-locked-pads", action="store_true")
    parser.add_argument("--omit-locked-copper", action="store_true")
    args = parser.parse_args()
    try:
        spec = load_spec(args.spec)
        export_batch(
            spec,
            args.batch_id,
            args.input,
            args.output,
            args.locked_class,
            args.isolate_locked_pads,
            args.omit_locked_copper,
        )
    except Exception as error:
        print(f"FAIL: {error}")
        return 1
    print(f"PASS: exported routing batch DSN to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
