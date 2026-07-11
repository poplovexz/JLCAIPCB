#!/usr/bin/env python3
"""Shared deterministic checks for the kicad-production-pcb skill."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import yaml


class CheckResult:
    def __init__(self) -> None:
        self.issues: list[str] = []
        self.warnings: list[str] = []

    def issue(self, message: str) -> None:
        self.issues.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def ok(self) -> bool:
        return not self.issues


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Top-level YAML must be a mapping: {path}")
    return data


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Top-level JSON must be a mapping: {path}")
    return data


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_mapping(value: Any) -> bool:
    return isinstance(value, dict)


def is_sequence(value: Any) -> bool:
    return isinstance(value, list)


def string_value(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def number_value(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def positive_number(value: Any) -> bool:
    return number_value(value) and float(value) > 0


def get_path(data: dict[str, Any], dotted_path: str) -> Any:
    current: Any = data
    for part in dotted_path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def require_mapping(data: dict[str, Any], dotted_path: str, result: CheckResult) -> dict[str, Any]:
    value = get_path(data, dotted_path)
    if not isinstance(value, dict):
        result.issue(f"{dotted_path} must be a mapping")
        return {}
    return value


def require_sequence(data: dict[str, Any], dotted_path: str, result: CheckResult) -> list[Any]:
    value = get_path(data, dotted_path)
    if not isinstance(value, list):
        result.issue(f"{dotted_path} must be a list")
        return []
    return value


def require_string(data: dict[str, Any], dotted_path: str, result: CheckResult) -> str:
    value = get_path(data, dotted_path)
    if not string_value(value):
        result.issue(f"{dotted_path} must be a non-empty string")
        return ""
    return str(value)


def require_positive(data: dict[str, Any], dotted_path: str, result: CheckResult) -> float:
    value = get_path(data, dotted_path)
    if not positive_number(value):
        result.issue(f"{dotted_path} must be a positive number")
        return 0.0
    return float(value)


def split_library_id(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, str) or ":" not in value:
        return None
    library, item = value.split(":", 1)
    if not library or not item:
        return None
    return library, item


def load_spec(spec_path: Path) -> dict[str, Any]:
    return load_yaml(spec_path)


def resolve_spec_project_path(spec_path: Path, spec: dict[str, Any], value: Path) -> Path:
    if value.is_absolute():
        return value.resolve()
    configured_root = get_path(spec, "project.root_dir")
    if string_value(configured_root):
        root = (spec_path.resolve().parent / str(configured_root)).resolve()
        return (root / value).resolve()
    return (Path.cwd() / value).resolve()


def component_map(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    components = spec.get("components", [])
    if not isinstance(components, list):
        return {}
    return {str(component.get("ref")): component for component in components if isinstance(component, dict) and component.get("ref")}


def net_names(spec: dict[str, Any]) -> set[str]:
    nets = spec.get("nets", [])
    if not isinstance(nets, list):
        return set()
    return {str(net.get("name")) for net in nets if isinstance(net, dict) and net.get("name")}


def net_classes(spec: dict[str, Any]) -> dict[str, str]:
    nets = spec.get("nets", [])
    if not isinstance(nets, list):
        return {}
    return {str(net.get("name")): str(net.get("class", "")) for net in nets if isinstance(net, dict) and net.get("name")}


def pin_id(ref: str, pad: str) -> str:
    return f"{ref}.{pad}"


def parse_pin(value: Any) -> tuple[str, str] | None:
    if isinstance(value, dict):
        ref = value.get("ref")
        pad = value.get("pad")
        if string_value(ref) and string_value(pad):
            return str(ref), str(pad)
        return None
    if not isinstance(value, str) or "." not in value:
        return None
    ref, pad = value.rsplit(".", 1)
    if not ref or not pad:
        return None
    return ref, pad


def endpoint_is_declared_point(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    point = value.get("point_mm")
    if not isinstance(point, dict):
        return False
    return number_value(point.get("x")) and number_value(point.get("y"))


def actual_net_graph(spec: dict[str, Any]) -> dict[str, list[str]]:
    graph: dict[str, list[str]] = {}
    for component in spec.get("components", []) if isinstance(spec.get("components"), list) else []:
        if not isinstance(component, dict):
            continue
        ref = component.get("ref")
        pads = component.get("pads")
        if not string_value(ref) or not isinstance(pads, dict):
            continue
        for pad, net in pads.items():
            if string_value(net):
                graph.setdefault(str(net), []).append(pin_id(str(ref), str(pad)))
    return {net: sorted(pins) for net, pins in sorted(graph.items())}


def spec_pin_to_net(spec: dict[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for net, pins in actual_net_graph(spec).items():
        for pin in pins:
            mapping[pin] = net
    return mapping


def pad_net(spec: dict[str, Any], ref: str, pad: str) -> str | None:
    component = component_map(spec).get(ref)
    if not component:
        return None
    pads = component.get("pads")
    if not isinstance(pads, dict):
        return None
    value = pads.get(str(pad))
    if not string_value(value):
        return None
    return str(value)


def configured_validation(spec: dict[str, Any]) -> dict[str, Any]:
    value = spec.get("validation", {})
    return value if isinstance(value, dict) else {}


def expected_net_graph_config(spec: dict[str, Any]) -> dict[str, Any]:
    validation = configured_validation(spec)
    value = spec.get("expected_net_graph", validation.get("expected_net_graph", {}))
    return value if isinstance(value, dict) else {}


def check_schema(
    spec: dict[str, Any], result: CheckResult, production: bool = False, layout: bool = False
) -> None:
    for section in ["project", "kicad", "board"]:
        require_mapping(spec, section, result)
    require_sequence(spec, "nets", result)
    require_sequence(spec, "components", result)

    for field in ["project.name", "project.output_dir", "project.artifacts_dir"]:
        require_string(spec, field, result)
    require_positive(spec, "project.kicad_major_required", result)

    require_positive(spec, "board.size_mm.width", result)
    require_positive(spec, "board.size_mm.height", result)
    require_positive(spec, "board.layers.copper", result)
    for field in [
        "board.fabrication.min_track_width_mm",
        "board.fabrication.default_signal_width_mm",
        "board.fabrication.default_power_width_mm",
        "board.fabrication.min_clearance_mm",
        "board.fabrication.min_via_diameter_mm",
        "board.fabrication.min_via_drill_mm",
    ]:
        require_positive(spec, field, result)

    declared_nets: set[str] = set()
    for index, net in enumerate(spec.get("nets", []) if isinstance(spec.get("nets"), list) else []):
        if not isinstance(net, dict):
            result.issue(f"nets[{index}] must be a mapping")
            continue
        name = net.get("name")
        if not string_value(name):
            result.issue(f"nets[{index}].name must be a non-empty string")
            continue
        if str(name) in declared_nets:
            result.issue(f"Duplicate net name: {name}")
        declared_nets.add(str(name))

    refs: set[str] = set()
    for index, component in enumerate(spec.get("components", []) if isinstance(spec.get("components"), list) else []):
        if not isinstance(component, dict):
            result.issue(f"components[{index}] must be a mapping")
            continue
        ref = component.get("ref")
        ref_label = str(ref) if string_value(ref) else f"components[{index}]"
        for field in ["ref", "value", "symbol", "footprint", "pads"]:
            if field not in component or component[field] in ("", None):
                result.issue(f"{ref_label} missing required component field: {field}")
        if string_value(ref):
            if str(ref) in refs:
                result.issue(f"Duplicate component ref: {ref}")
            refs.add(str(ref))
        if split_library_id(component.get("symbol")) is None:
            result.issue(f"{ref_label}.symbol must use Library:Name form")
        if split_library_id(component.get("footprint")) is None:
            result.issue(f"{ref_label}.footprint must use Library:Name form")
        pads = component.get("pads")
        if not isinstance(pads, dict) or not pads:
            result.issue(f"{ref_label}.pads must be a non-empty mapping")
        else:
            for pad, net in pads.items():
                if not string_value(str(pad)):
                    result.issue(f"{ref_label}.pads contains an empty pad key")
                if not string_value(net):
                    result.issue(f"{ref_label}.{pad} must map to a non-empty net")
                elif str(net) not in declared_nets:
                    result.issue(f"{ref_label}.{pad} references undeclared net: {net}")
        if "position_mm" in component:
            position = component["position_mm"]
            if not isinstance(position, dict):
                result.issue(f"{ref_label}.position_mm must be a mapping")
            else:
                for field in ["x", "y", "rotation"]:
                    if field not in position or not number_value(position[field]):
                        result.issue(f"{ref_label}.position_mm.{field} must be numeric")
        elif layout:
            result.issue(f"{ref_label}.position_mm is required for a frozen layout input")

    for index, route in enumerate(spec.get("routes", []) if isinstance(spec.get("routes"), list) else []):
        if not isinstance(route, dict):
            result.issue(f"routes[{index}] must be a mapping")
            continue
        net = route.get("net")
        route_label = f"routes[{index}]"
        if not string_value(net):
            result.issue(f"{route_label}.net must be a non-empty string")
        elif str(net) not in declared_nets:
            result.issue(f"{route_label}.net references undeclared net: {net}")
        if not positive_number(route.get("width_mm")):
            result.issue(f"{route_label}.width_mm must be a positive number")
        for endpoint_name in ["from", "to"]:
            endpoint = route.get(endpoint_name)
            parsed = parse_pin(endpoint)
            if parsed is None:
                if not endpoint_is_declared_point(endpoint):
                    result.issue(f"{route_label}.{endpoint_name} must identify a component pad or point_mm")
                continue
            endpoint_net = pad_net(spec, parsed[0], parsed[1])
            if endpoint_net is None:
                result.issue(f"{route_label}.{endpoint_name} references unknown pad: {pin_id(parsed[0], parsed[1])}")
            elif string_value(net) and endpoint_net != str(net):
                result.issue(f"{route_label}.{endpoint_name} pad net {endpoint_net} does not match route net {net}")

    manufacturing = spec.get("manufacturing", {})
    if manufacturing and not isinstance(manufacturing, dict):
        result.issue("manufacturing must be a mapping")
    if isinstance(manufacturing, dict) and manufacturing.get("target") == "jlcpcb":
        jlcpcb = manufacturing.get("jlcpcb")
        if not isinstance(jlcpcb, dict):
            result.issue("manufacturing.jlcpcb is required when manufacturing.target is jlcpcb")
        elif production:
            for field in [
                "conservative_rules_mm.min_track_width_mm",
                "conservative_rules_mm.min_clearance_mm",
                "package.output_dir",
                "package.gerber_drill_zip",
                "package.manifest",
            ]:
                if get_path(jlcpcb, field) in (None, ""):
                    result.issue(f"manufacturing.jlcpcb.{field} is required in production mode")


def check_net_graph(spec: dict[str, Any], result: CheckResult, exact: bool = False) -> None:
    graph = actual_net_graph(spec)
    declared = net_names(spec)
    classes = net_classes(spec)
    validation = configured_validation(spec)
    graph_options = validation.get("net_graph", {}) if isinstance(validation.get("net_graph"), dict) else {}
    allow_single = set(str(item) for item in graph_options.get("allow_single_pin_nets", []))

    for net in sorted(declared - set(graph)):
        result.issue(f"Declared net has no component pads: {net}")

    for net, pins in graph.items():
        if net not in declared:
            result.issue(f"Net graph contains undeclared net: {net}")
            continue
        if len(pins) < 2 and net not in allow_single:
            result.issue(f"Net has fewer than two connected pins: {net} ({', '.join(pins)})")
        if classes.get(net) == "power" and len(pins) < 2 and net not in allow_single:
            result.issue(f"Power net has no real source/load pair: {net}")

    expected = expected_net_graph_config(spec)
    expected_nets = expected.get("nets", expected)
    if isinstance(expected_nets, dict):
        for net, rule in expected_nets.items():
            pins = set(graph.get(str(net), []))
            if not isinstance(rule, dict):
                result.issue(f"expected_net_graph.nets.{net} must be a mapping")
                continue
            required_pins = set(str(item) for item in rule.get("required_pins", rule.get("pins", [])))
            for required_pin in sorted(required_pins - pins):
                result.issue(f"Expected pin missing from net {net}: {required_pin}")
            min_pins = rule.get("min_pins")
            if min_pins is not None and len(pins) < int(min_pins):
                result.issue(f"Net {net} has {len(pins)} pins; expected at least {min_pins}")
            if exact or rule.get("exact", False):
                unexpected = sorted(pins - required_pins)
                if unexpected:
                    result.issue(f"Net {net} has unexpected pins: {', '.join(unexpected)}")

    for index, route in enumerate(spec.get("routes", []) if isinstance(spec.get("routes"), list) else []):
        if not isinstance(route, dict):
            continue
        net = route.get("net")
        if not string_value(net):
            continue
        for endpoint_name in ["from", "to"]:
            endpoint = parse_pin(route.get(endpoint_name))
            if endpoint is None:
                if not endpoint_is_declared_point(route.get(endpoint_name)):
                    result.issue(f"Route {index} endpoint {endpoint_name} is neither a pad nor point_mm")
                continue
            endpoint_id = pin_id(endpoint[0], endpoint[1])
            if endpoint_id not in graph.get(str(net), []):
                result.issue(f"Route {index} endpoint {endpoint_id} is not in net graph for {net}")


def generated_schematic_path(spec: dict[str, Any]) -> Path:
    return Path(str(spec["project"]["output_dir"])) / f"{spec['project']['name']}.kicad_sch"


def generated_netlist_path(spec: dict[str, Any]) -> Path:
    return Path(str(spec["project"]["artifacts_dir"])) / "checks" / str(spec["project"]["name"]) / "generated_netlist.xml"


def generated_net_graph_path(spec: dict[str, Any]) -> Path:
    return Path(str(spec["project"]["artifacts_dir"])) / "checks" / str(spec["project"]["name"]) / "generated_net_graph.json"


def export_generated_netlist(spec: dict[str, Any], result: CheckResult) -> Path | None:
    schematic = generated_schematic_path(spec)
    if not schematic.exists():
        result.issue(f"Generated schematic is missing: {schematic}")
        return None
    output = generated_netlist_path(spec)
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "kicad-cli",
        "sch",
        "export",
        "netlist",
        "--format",
        "kicadxml",
        "--output",
        str(output),
        str(schematic),
    ]
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode:
        message = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        result.issue(f"Failed to export generated KiCad netlist: {message}")
        return None
    return output


def parse_kicadxml_net_graph(netlist_path: Path) -> dict[str, list[str]]:
    tree = ElementTree.parse(netlist_path)
    root = tree.getroot()
    graph: dict[str, list[str]] = {}
    for index, net in enumerate(root.findall("./nets/net")):
        name = net.attrib.get("name") or net.attrib.get("code") or f"generated_net_{index}"
        pins: list[str] = []
        for node in net.findall("node"):
            ref = node.attrib.get("ref")
            pin = node.attrib.get("pin")
            if ref and pin:
                pins.append(pin_id(ref, pin))
        graph[str(name)] = sorted(pins)
    return {net: pins for net, pins in sorted(graph.items())}


def compare_generated_net_graph(
    spec: dict[str, Any],
    generated_graph: dict[str, list[str]],
    result: CheckResult,
    strict_names: bool = False,
) -> None:
    expected_graph = actual_net_graph(spec)
    expected_pin_net = spec_pin_to_net(spec)
    generated_pin_net: dict[str, str] = {}
    for generated_net, pins in generated_graph.items():
        for pin in pins:
            if pin in generated_pin_net:
                result.issue(f"Generated pin appears in multiple nets: {pin}")
            generated_pin_net[pin] = generated_net

    if strict_names:
        for expected_net, expected_pins_list in expected_graph.items():
            expected_pins = set(expected_pins_list)
            if expected_net not in generated_graph:
                observed = sorted({generated_pin_net[pin] for pin in expected_pins if pin in generated_pin_net})
                suffix = f"; pins appeared on: {', '.join(observed)}" if observed else ""
                result.issue(f"Generated KiCad net name is missing: {expected_net}{suffix}")
                continue
            generated_spec_pins = {pin for pin in generated_graph[expected_net] if pin in expected_pin_net}
            missing = sorted(expected_pins - generated_spec_pins)
            unexpected = sorted(generated_spec_pins - expected_pins)
            if missing:
                result.issue(f"Generated KiCad net {expected_net} is missing expected pins: {', '.join(missing)}")
            if unexpected:
                result.issue(f"Generated KiCad net {expected_net} has unexpected spec pins: {', '.join(unexpected)}")
        for generated_net, pins in generated_graph.items():
            spec_pins = {pin for pin in pins if pin in expected_pin_net}
            if spec_pins and generated_net not in expected_graph:
                result.issue(
                    f"Generated KiCad net has a non-Spec name {generated_net}: {', '.join(sorted(spec_pins))}"
                )

    for pin in sorted(expected_pin_net):
        if pin not in generated_pin_net:
            result.issue(f"Spec pin is missing from generated KiCad netlist: {pin}")

    for expected_net, expected_pins_list in expected_graph.items():
        expected_pins = set(expected_pins_list)
        generated_nets = {generated_pin_net[pin] for pin in expected_pins if pin in generated_pin_net}
        if not generated_nets:
            result.issue(f"Spec net has no generated KiCad connectivity: {expected_net}")
            continue
        if len(generated_nets) > 1:
            result.issue(f"Spec net is split across generated KiCad nets: {expected_net} -> {', '.join(sorted(generated_nets))}")
            continue
        generated_net = next(iter(generated_nets))
        generated_pins = set(generated_graph.get(generated_net, []))
        missing = sorted(expected_pins - generated_pins)
        if missing:
            result.issue(f"Generated KiCad net {generated_net} is missing expected pins for {expected_net}: {', '.join(missing)}")
        spec_pins_on_generated_net = {pin for pin in generated_pins if pin in expected_pin_net}
        other_spec_nets = sorted({expected_pin_net[pin] for pin in spec_pins_on_generated_net if expected_pin_net[pin] != expected_net})
        if other_spec_nets:
            result.issue(
                f"Generated KiCad net {generated_net} shorts spec net {expected_net} with: {', '.join(other_spec_nets)}"
            )

    spec_refs = set(component_map(spec))
    allow_extra = set(
        str(item)
        for item in (
            configured_validation(spec)
            .get("generated_netlist", {})
            if isinstance(configured_validation(spec).get("generated_netlist"), dict)
            else {}
        ).get("allow_extra_pins", [])
    )
    for generated_net, pins in generated_graph.items():
        for pin in pins:
            if generated_net.startswith("unconnected-"):
                continue
            ref, _, _pad = pin.partition(".")
            if ref in spec_refs and pin not in expected_pin_net and pin not in allow_extra:
                result.issue(f"Generated KiCad net {generated_net} contains a pin not declared in spec pads: {pin}")


def check_generated_netlist(spec: dict[str, Any], result: CheckResult, strict_names: bool = False) -> None:
    netlist = export_generated_netlist(spec, result)
    if netlist is None:
        return
    try:
        generated_graph = parse_kicadxml_net_graph(netlist)
    except ElementTree.ParseError as error:
        result.issue(f"Failed to parse generated KiCad netlist {netlist}: {error}")
        return
    graph_path = generated_net_graph_path(spec)
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    graph_path.write_text(json.dumps(generated_graph, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    compare_generated_net_graph(spec, generated_graph, result, strict_names=strict_names)


def batch_nets(batch: dict[str, Any], field: str) -> set[str]:
    value = batch.get(field, [])
    if not isinstance(value, list):
        return set()
    return {str(item) for item in value if string_value(item)}


def connectivity_batch_policy(spec: dict[str, Any]) -> dict[str, Any]:
    default_path = Path(__file__).resolve().parents[1] / "assets" / "connectivity-batch-policy.yaml"
    builtin = load_yaml(default_path)
    policy_file = get_path(spec, "validation.connectivity_batch_policy_file")
    stage = str(get_path(spec, "project.stage") or "")
    protected_stages = {str(value) for value in builtin.get("require_for_stages", [])}
    if string_value(policy_file) and stage not in protected_stages:
        policy_path = Path(str(policy_file))
        if not policy_path.is_absolute():
            policy_path = Path.cwd() / policy_path
    else:
        policy_path = default_path
    try:
        return load_yaml(policy_path)
    except (OSError, ValueError, yaml.YAMLError):
        return {}


def connectivity_batch_requirement_reasons(spec: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    policy = connectivity_batch_policy(spec)

    validation = configured_validation(spec)
    connectivity_validation = validation.get("connectivity_batches", {})
    if isinstance(connectivity_validation, dict) and connectivity_validation.get("required") is True:
        reasons.append("validation.connectivity_batches.required is true")

    stage = get_path(spec, "project.stage")
    require_for_stages = policy.get("require_for_stages", [])
    if not isinstance(require_for_stages, list):
        require_for_stages = []
    if string_value(stage) and str(stage) in {str(item) for item in require_for_stages if string_value(item)}:
        reasons.append(f"project.stage is {stage}")

    require_when = policy.get("require_when", {})
    if not isinstance(require_when, dict):
        require_when = {}
    if require_when.get("spec_closure_required") is True and isinstance(validation.get("spec_closure"), dict) and validation["spec_closure"].get("required") is True:
        reasons.append("validation.spec_closure.required is true")

    declared_sections = require_when.get("declared_sections", [])
    if not isinstance(declared_sections, list):
        declared_sections = []
    for section in [str(item) for item in declared_sections if string_value(item)]:
        value = spec.get(section)
        if isinstance(value, dict) and bool(value):
            reasons.append(f"{section} is declared")

    declared_list_sections = require_when.get("declared_list_sections", [])
    if not isinstance(declared_list_sections, list):
        declared_list_sections = []
    for section in [str(item) for item in declared_list_sections if string_value(item)]:
        value = spec.get(section)
        if isinstance(value, list) and bool(value):
            reasons.append(f"{section} is declared")

    components = spec.get("components", [])
    nets = spec.get("nets", [])
    component_count = len(components) if isinstance(components, list) else 0
    net_count = len(nets) if isinstance(nets, list) else 0
    thresholds = policy.get("complexity_thresholds", {})
    if isinstance(thresholds, dict):
        spec_thresholds = validation.get("connectivity_complexity", {})
        if isinstance(spec_thresholds, dict):
            thresholds = {**thresholds, **spec_thresholds}
        min_components = thresholds.get("min_components_for_batches")
        min_nets = thresholds.get("min_nets_for_batches")
        if isinstance(min_components, int) and component_count >= min_components:
            reasons.append(f"component count {component_count} >= {min_components}")
        if isinstance(min_nets, int) and net_count >= min_nets:
            reasons.append(f"net count {net_count} >= {min_nets}")

    return reasons


def check_connectivity_batches(
    spec: dict[str, Any],
    result: CheckResult,
    require_batches: bool = False,
    auto_require: bool = False,
) -> None:
    if string_value(get_path(spec, "validation.connectivity_batch_policy_file")):
        builtin = load_yaml(Path(__file__).resolve().parents[1] / "assets" / "connectivity-batch-policy.yaml")
        stage = str(get_path(spec, "project.stage") or "")
        if stage in {str(value) for value in builtin.get("require_for_stages", [])}:
            result.issue("required-stage connectivity policy override is forbidden; use the bundled trusted policy")
    batches = spec.get("connectivity_batches", [])
    if batches in (None, []):
        reasons = connectivity_batch_requirement_reasons(spec) if auto_require else []
        if require_batches or reasons:
            reason_text = "; ".join(reasons) if reasons else "required by command line"
            result.issue(f"connectivity_batches is required for this check ({reason_text})")
        return
    if not isinstance(batches, list):
        result.issue("connectivity_batches must be a list")
        return

    declared_nets = net_names(spec)
    components = component_map(spec)
    graph = actual_net_graph(spec)
    available_nets: set[str] = set()

    for index, batch in enumerate(batches):
        if not isinstance(batch, dict):
            result.issue(f"connectivity_batches[{index}] must be a mapping")
            continue
        name = batch.get("name", f"batch[{index}]")
        if not string_value(name):
            result.issue(f"connectivity_batches[{index}].name must be a non-empty string")
            name = f"batch[{index}]"
        label = str(name)

        for field in ["upstream_nets", "provided_nets", "consumed_nets", "required_nets"]:
            value = batch.get(field, [])
            if value is not None and not isinstance(value, list):
                result.issue(f"{label}.{field} must be a list")
            for net in batch_nets(batch, field):
                if net not in declared_nets:
                    result.issue(f"{label}.{field} references undeclared net: {net}")

        missing_upstream = sorted(batch_nets(batch, "upstream_nets") - available_nets)
        if missing_upstream:
            result.issue(f"{label} missing upstream nets from prior batches: {', '.join(missing_upstream)}")

        for ref in batch.get("components", []) if isinstance(batch.get("components", []), list) else []:
            if str(ref) not in components:
                result.issue(f"{label}.components references unknown component: {ref}")

        for net in sorted(batch_nets(batch, "required_nets")):
            if len(graph.get(net, [])) < 2:
                result.issue(f"{label} required net is not connected by at least two pins: {net}")

        connections = batch.get("connections", [])
        if connections and not isinstance(connections, list):
            result.issue(f"{label}.connections must be a list")
            connections = []
        for connection_index, connection in enumerate(connections):
            if not isinstance(connection, dict):
                result.issue(f"{label}.connections[{connection_index}] must be a mapping")
                continue
            net = connection.get("net")
            if not string_value(net) or str(net) not in declared_nets:
                result.issue(f"{label}.connections[{connection_index}].net references an unknown net: {net}")
                continue
            pins = connection.get("pins", [])
            if not isinstance(pins, list) or not pins:
                result.issue(f"{label}.connections[{connection_index}].pins must be a non-empty list")
                continue
            for raw_pin in pins:
                parsed = parse_pin(raw_pin)
                if parsed is None:
                    result.issue(f"{label}.connections[{connection_index}] contains invalid pin: {raw_pin}")
                    continue
                actual = pad_net(spec, parsed[0], parsed[1])
                if actual is None:
                    result.issue(f"{label}.connections[{connection_index}] references unknown pad: {pin_id(parsed[0], parsed[1])}")
                elif actual != str(net):
                    result.issue(
                        f"{label}.connections[{connection_index}] pin {pin_id(parsed[0], parsed[1])} maps to {actual}, expected {net}"
                    )

        available_nets.update(batch_nets(batch, "provided_nets"))
        available_nets.update(batch_nets(batch, "required_nets"))


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("._-") or "batch"


def pins_from_values(values: Any) -> set[str]:
    pins: set[str] = set()
    if not isinstance(values, list):
        return pins
    for value in values:
        parsed = parse_pin(value)
        if parsed is not None:
            pins.add(pin_id(parsed[0], parsed[1]))
    return pins


def batch_connection_pins(batch: dict[str, Any]) -> set[str]:
    pins: set[str] = set()
    connections = batch.get("connections", [])
    if isinstance(connections, list):
        for connection in connections:
            if isinstance(connection, dict):
                pins.update(pins_from_values(connection.get("pins", [])))
    pins.update(pins_from_values(batch.get("required_pins", [])))
    return pins


def refs_from_pins(pins: set[str]) -> set[str]:
    refs: set[str] = set()
    for item in pins:
        ref, dot, _pad = item.partition(".")
        if dot and ref:
            refs.add(ref)
    return refs


def batch_component_refs(batch: dict[str, Any]) -> set[str]:
    refs = {str(item) for item in batch.get("components", []) if string_value(item)} if isinstance(batch.get("components"), list) else set()
    refs.update(refs_from_pins(batch_connection_pins(batch)))
    return refs


def route_endpoint_pin(endpoint: Any) -> str | None:
    parsed = parse_pin(endpoint)
    if parsed is None:
        return None
    return pin_id(parsed[0], parsed[1])


def route_is_in_scope(route: dict[str, Any], nets_in_scope: set[str], pins_in_scope: set[str]) -> bool:
    net = route.get("net")
    if not string_value(net) or str(net) not in nets_in_scope:
        return False
    for endpoint_name in ["from", "to"]:
        endpoint = route.get(endpoint_name)
        parsed = parse_pin(endpoint)
        if parsed is None:
            if not endpoint_is_declared_point(endpoint):
                return False
            continue
        if pin_id(parsed[0], parsed[1]) not in pins_in_scope:
            return False
    return True


def scoped_expected_net_graph(expected: dict[str, Any], nets_in_scope: set[str], pins_in_scope: set[str]) -> dict[str, Any]:
    if not isinstance(expected, dict):
        return {}
    original_nets = expected.get("nets", expected)
    if not isinstance(original_nets, dict):
        return {}
    scoped_nets = {}
    for net, rule in original_nets.items():
        if str(net) not in nets_in_scope:
            continue
        scoped_rule = copy.deepcopy(rule)
        if isinstance(scoped_rule, dict):
            for field in ["pins", "required_pins"]:
                value = scoped_rule.get(field)
                if isinstance(value, list):
                    scoped_rule[field] = [pin for pin in value if str(pin) in pins_in_scope]
        scoped_nets[str(net)] = scoped_rule
    return {"nets": scoped_nets} if "nets" in expected else scoped_nets


def make_batch_spec(
    spec: dict[str, Any],
    batch: dict[str, Any],
    index: int,
    temp_root: Path,
    cumulative_refs: set[str],
    cumulative_nets: set[str],
) -> dict[str, Any]:
    temp_spec = copy.deepcopy(spec)
    base_project = spec.get("project", {}) if isinstance(spec.get("project"), dict) else {}
    base_name = str(base_project.get("name", "pcb"))
    batch_name = safe_name(str(batch.get("name", f"batch_{index + 1}")))
    temp_project_name = f"{base_name}__batch_{index + 1:02d}_{batch_name}"
    temp_project_root = temp_root / temp_project_name

    project = temp_spec.setdefault("project", {})
    project["name"] = temp_project_name
    project["title"] = f"{base_project.get('title', base_name)} batch {index + 1}: {batch_name}"
    project["output_dir"] = str(temp_project_root / "project")
    project["artifacts_dir"] = str(temp_project_root / "artifacts")

    temp_spec["nets"] = [
        copy.deepcopy(net)
        for net in spec.get("nets", [])
        if isinstance(net, dict) and string_value(net.get("name")) and str(net["name"]) in cumulative_nets
    ]

    scoped_components: list[dict[str, Any]] = []
    pins_in_scope: set[str] = set()
    for component in spec.get("components", []) if isinstance(spec.get("components"), list) else []:
        if not isinstance(component, dict) or not string_value(component.get("ref")):
            continue
        ref = str(component["ref"])
        if ref not in cumulative_refs:
            continue
        pads = component.get("pads")
        if not isinstance(pads, dict):
            continue
        scoped_pads = {str(pad): str(net) for pad, net in pads.items() if string_value(net) and str(net) in cumulative_nets}
        if not scoped_pads:
            continue
        scoped = copy.deepcopy(component)
        scoped["pads"] = scoped_pads
        scoped_components.append(scoped)
        for pad in scoped_pads:
            pins_in_scope.add(pin_id(ref, str(pad)))
    temp_spec["components"] = scoped_components

    temp_spec["routes"] = [
        copy.deepcopy(route)
        for route in spec.get("routes", [])
        if isinstance(route, dict) and route_is_in_scope(route, cumulative_nets, pins_in_scope)
    ]

    if "expected_net_graph" in temp_spec:
        temp_spec["expected_net_graph"] = scoped_expected_net_graph(temp_spec["expected_net_graph"], cumulative_nets, pins_in_scope)
    validation = temp_spec.get("validation")
    if isinstance(validation, dict) and "expected_net_graph" in validation:
        validation["expected_net_graph"] = scoped_expected_net_graph(validation["expected_net_graph"], cumulative_nets, pins_in_scope)

    return temp_spec


def run_command(command: list[str], result: CheckResult) -> bool:
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode:
        output = "\n".join(part.strip() for part in [completed.stdout, completed.stderr] if part.strip())
        result.issue(f"Command failed ({completed.returncode}): {' '.join(command)}{': ' + output if output else ''}")
        return False
    return True


def remove_tree(path: Path, result: CheckResult, label: str) -> None:
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except OSError as error:
        result.issue(f"rollback_failed: failed to remove temporary {label} {path}: {error}")


def check_generated_connectivity_batches(
    spec_path: Path,
    spec: dict[str, Any],
    result: CheckResult,
    generator: Path,
    generator_args: list[str] | None = None,
    keep_temp: bool = False,
) -> None:
    batches = spec.get("connectivity_batches", [])
    if not isinstance(batches, list) or not batches:
        return
    if not generator.exists():
        result.issue(f"Generated batch validation requires generator script: {generator}")
        return

    project = spec.get("project", {}) if isinstance(spec.get("project"), dict) else {}
    base_name = safe_name(str(project.get("name", spec_path.stem)))
    artifacts_dir = Path(str(project.get("artifacts_dir", "artifacts")))
    temp_root = artifacts_dir / "connectivity_batch_check" / base_name
    report_dir = artifacts_dir / "checks" / str(project.get("name", spec_path.stem))
    report_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"project": project.get("name", spec_path.stem), "batches": []}

    cumulative_refs: set[str] = set()
    cumulative_nets: set[str] = set()

    for index, batch in enumerate(batches):
        if not isinstance(batch, dict):
            continue
        label = str(batch.get("name", f"batch_{index + 1}"))
        cumulative_refs.update(batch_component_refs(batch))
        cumulative_nets.update(batch_nets(batch, "upstream_nets"))
        cumulative_nets.update(batch_nets(batch, "consumed_nets"))
        cumulative_nets.update(batch_nets(batch, "provided_nets"))
        cumulative_nets.update(batch_nets(batch, "required_nets"))
        for connection in batch.get("connections", []) if isinstance(batch.get("connections"), list) else []:
            if isinstance(connection, dict) and string_value(connection.get("net")):
                cumulative_nets.add(str(connection["net"]))
            cumulative_refs.update(refs_from_pins(batch_connection_pins(batch)))

        temp_spec = make_batch_spec(spec, batch, index, temp_root, cumulative_refs, cumulative_nets)
        temp_project_root = Path(str(temp_spec["project"]["output_dir"])).parent
        remove_tree(temp_project_root, result, label)
        temp_project_root.mkdir(parents=True, exist_ok=True)
        temp_spec_path = temp_project_root / "spec.yaml"
        temp_spec_path.write_text(yaml.safe_dump(temp_spec, sort_keys=False), encoding="utf-8")

        batch_result = CheckResult()
        check_schema(temp_spec, batch_result)
        check_net_graph(temp_spec, batch_result)
        if batch_result.ok():
            if run_command([sys.executable, str(generator), *(generator_args or []), str(temp_spec_path)], batch_result):
                check_generated_netlist(temp_spec, batch_result, strict_names=True)

        report["batches"].append(
            {
                "name": label,
                "ok": batch_result.ok(),
                "temp_project_root": str(temp_project_root),
                "kept_temp": keep_temp or not batch_result.ok(),
                "issues": batch_result.issues,
                "warnings": batch_result.warnings,
            }
        )
        for warning in batch_result.warnings:
            result.warning(f"{label}: {warning}")
        for issue in batch_result.issues:
            result.issue(f"{label}: {issue}")

        if batch_result.ok() and not keep_temp:
            remove_tree(temp_project_root, result, label)
        if not batch_result.ok():
            break

    report_path = report_dir / "connectivity_batch_generated_check.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def release_dir_from_spec(spec: dict[str, Any]) -> Path | None:
    release = get_path(spec, "manufacturing.jlcpcb.release")
    if not isinstance(release, dict):
        return None
    output_dir = release.get("output_dir")
    if not string_value(output_dir):
        return None
    return Path(str(output_dir))


def check_evidence_manifest(spec: dict[str, Any], result: CheckResult, order_ready: bool = False) -> None:
    release = get_path(spec, "manufacturing.jlcpcb.release")
    if not isinstance(release, dict):
        if order_ready:
            result.issue("manufacturing.jlcpcb.release is required for order-ready evidence")
        return

    release_dir = release_dir_from_spec(spec)
    if release_dir is None:
        result.issue("manufacturing.jlcpcb.release.output_dir is required")
        return

    order_review = release.get("order_review", {})
    if not isinstance(order_review, dict):
        if order_ready:
            result.issue("manufacturing.jlcpcb.release.order_review is required for order-ready evidence")
        return

    manifest_name = order_review.get("evidence_manifest", "evidence_manifest.json")
    manifest_path = release_dir / str(manifest_name)
    if not manifest_path.exists():
        if order_ready:
            result.issue(f"Missing evidence manifest: {manifest_path}")
        return

    try:
        manifest = load_json(manifest_path)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        result.issue(f"Invalid evidence manifest {manifest_path}: {error}")
        return

    evidence = manifest.get("evidence")
    if not isinstance(evidence, dict):
        result.issue(f"{manifest_path} must contain evidence mapping")
        return

    required_items = order_review.get("required_items", [])
    if required_items and not isinstance(required_items, list):
        result.issue("order_review.required_items must be a list")
        required_items = []

    required_fields = order_review.get("required_evidence_fields", [])
    if not isinstance(required_fields, list):
        result.issue("order_review.required_evidence_fields must be a list")
        required_fields = []
    allowed_extensions = {str(item).lower() for item in order_review.get("required_evidence_file_extensions", [])}
    source_pattern = order_review.get("evidence_source_url_pattern")
    source_re = re.compile(str(source_pattern)) if string_value(source_pattern) else None

    release_manifest_path = release_dir / str(release.get("manifest", "release_manifest.json"))
    release_files_by_name: dict[str, str] = {}
    release_roles: dict[str, list[str]] = {}
    if release_manifest_path.exists():
        try:
            release_manifest = load_json(release_manifest_path)
            for entry in release_manifest.get("release_files", []):
                if isinstance(entry, dict) and string_value(entry.get("file")) and string_value(entry.get("sha256")):
                    release_files_by_name[str(entry["file"])] = str(entry["sha256"])
                    release_roles.setdefault(str(entry.get("role", "")), []).append(str(entry["file"]))
        except (OSError, json.JSONDecodeError, ValueError) as error:
            result.issue(f"Invalid release manifest {release_manifest_path}: {error}")
    elif order_ready:
        result.issue(f"Missing release manifest for order-ready evidence: {release_manifest_path}")

    required_roles = order_review.get("fingerprint_roles", [])
    if order_ready and (not isinstance(required_roles, list) or not required_roles):
        result.issue("order_review.fingerprint_roles must be a non-empty list for order-ready evidence")
        required_roles = []
    expected_fingerprint_files = {
        file_name: release_files_by_name[file_name]
        for role in required_roles if isinstance(required_roles, list)
        for file_name in release_roles.get(str(role), [])
    }
    for role in required_roles if isinstance(required_roles, list) else []:
        if not release_roles.get(str(role)):
            result.issue(f"Release manifest missing fingerprint role: {role}")

    max_age_hours = order_review.get("evidence_max_age_hours")
    max_age = None
    if max_age_hours is not None:
        try:
            max_age = timedelta(hours=float(max_age_hours))
            if max_age.total_seconds() <= 0:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            result.issue("order_review.evidence_max_age_hours must be a positive number")
            max_age = None

    expected_item_types: dict[str, str] = {}
    item_source_patterns: dict[str, re.Pattern[str]] = {}
    for item in required_items:
        if not isinstance(item, dict) or not string_value(item.get("id")):
            result.issue("order_review.required_items entries must contain id")
            continue
        item_id = str(item["id"])
        if string_value(item.get("evidence_type")):
            expected_item_types[item_id] = str(item["evidence_type"])
        if string_value(item.get("evidence_source_url_pattern")):
            item_source_patterns[item_id] = re.compile(str(item["evidence_source_url_pattern"]))
        if order_ready and item_id not in evidence:
            result.issue(f"Missing required evidence item: {item_id}")

    for item_id, entry in evidence.items():
        if not isinstance(entry, dict):
            result.issue(f"Evidence item must be a mapping: {item_id}")
            continue
        if item_id in expected_item_types and entry.get("evidence_type") != expected_item_types[item_id]:
            result.issue(f"Evidence item {item_id} has wrong evidence_type: {entry.get('evidence_type')}")
        if order_ready and entry.get("result") != "passed":
            result.issue(f"Evidence item {item_id} result must be passed for order-ready")
        imported_at = entry.get("imported_at_utc")
        if order_ready and not string_value(imported_at):
            result.issue(f"Evidence item {item_id} missing imported_at_utc")
        elif string_value(imported_at):
            try:
                imported_time = datetime.fromisoformat(str(imported_at).replace("Z", "+00:00"))
                if imported_time.tzinfo is None:
                    raise ValueError
                age = datetime.now(timezone.utc) - imported_time.astimezone(timezone.utc)
                if age < timedelta(0):
                    result.issue(f"Evidence item {item_id} imported_at_utc is in the future")
                elif max_age is not None and age > max_age:
                    result.issue(f"Evidence item {item_id} is older than evidence_max_age_hours")
            except (TypeError, ValueError, OverflowError):
                result.issue(f"Evidence item {item_id} has invalid imported_at_utc")
        for field in required_fields:
            if not string_value(entry.get(str(field))):
                result.issue(f"Evidence item {item_id} missing required field: {field}")

        evidence_file = entry.get("file")
        if not string_value(evidence_file):
            result.issue(f"Evidence item {item_id} missing file")
        else:
            evidence_path = Path(str(evidence_file))
            if not evidence_path.is_absolute():
                evidence_path = Path.cwd() / evidence_path
            if not evidence_path.exists():
                result.issue(f"Evidence item {item_id} file does not exist: {evidence_file}")
            else:
                if allowed_extensions and evidence_path.suffix.lower() not in allowed_extensions:
                    result.issue(f"Evidence item {item_id} file extension is not allowed: {evidence_path.suffix}")
                if string_value(entry.get("sha256")) and sha256_file(evidence_path) != str(entry["sha256"]):
                    result.issue(f"Evidence item {item_id} sha256 does not match file: {evidence_file}")

        source_url = entry.get("source_url")
        url_re = item_source_patterns.get(str(item_id), source_re)
        if url_re is not None:
            if not string_value(source_url) or not url_re.match(str(source_url)):
                result.issue(f"Evidence item {item_id} source_url does not match required pattern")

        fingerprint = entry.get("release_fingerprint")
        if order_ready and not isinstance(fingerprint, dict):
            result.issue(f"Evidence item {item_id} missing release_fingerprint")
            continue
        if isinstance(fingerprint, dict):
            roles = fingerprint.get("roles", [])
            if not isinstance(roles, list):
                result.issue(f"Evidence item {item_id} release_fingerprint.roles must be a list")
                roles = []
            if isinstance(required_roles, list):
                missing_roles = sorted(set(str(role) for role in required_roles) - set(str(role) for role in roles))
                if missing_roles:
                    result.issue(f"Evidence item {item_id} fingerprint missing roles: {', '.join(missing_roles)}")
            files = fingerprint.get("files", {})
            if not isinstance(files, dict):
                result.issue(f"Evidence item {item_id} release_fingerprint.files must be a mapping")
            elif order_ready and files != expected_fingerprint_files:
                result.issue(f"Evidence item {item_id} release fingerprint does not exactly match current release roles")


def print_result(name: str, result: CheckResult, json_output: bool = False) -> int:
    payload = {
        "check": name,
        "ok": result.ok(),
        "issues": result.issues,
        "warnings": result.warnings,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        status = "PASS" if result.ok() else "FAIL"
        print(f"{name}: {status}")
        for warning in result.warnings:
            print(f"WARNING: {warning}")
        for issue in result.issues:
            print(f"ISSUE: {issue}")
    return 0 if result.ok() else 1


def parser_with_spec(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("spec", type=Path)
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def schema_main(argv: list[str]) -> int:
    parser = parser_with_spec("Validate a specs.yaml structure before KiCad generation.")
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--layout", action="store_true", help="Require component placement inputs when layout is already frozen.")
    args = parser.parse_args(argv[1:])
    result = CheckResult()
    try:
        check_schema(load_spec(args.spec), result, production=args.production, layout=args.layout)
    except Exception as error:
        result.issue(str(error))
    return print_result("spec_schema_check", result, args.json_output)


def net_graph_main(argv: list[str]) -> int:
    parser = parser_with_spec("Validate the component pad net graph declared by a specs.yaml file.")
    parser.add_argument("--exact", action="store_true", help="Treat expected_net_graph pins as exact when present.")
    parser.add_argument("--generated", action="store_true", help="Also compare the generated KiCad schematic netlist.")
    parser.add_argument("--strict-names", action="store_true", help="Require generated KiCad net names to equal Spec net names.")
    args = parser.parse_args(argv[1:])
    result = CheckResult()
    try:
        spec = load_spec(args.spec)
        check_schema(spec, result)
        check_net_graph(spec, result, exact=args.exact)
        if args.generated:
            check_generated_netlist(spec, result, strict_names=args.strict_names)
    except Exception as error:
        result.issue(str(error))
    return print_result("spec_net_graph_check", result, args.json_output)


def connectivity_batch_main(argv: list[str]) -> int:
    parser = parser_with_spec("Validate module-level connectivity batch declarations.")
    parser.add_argument("--require-batches", action="store_true")
    parser.add_argument("--auto-require", action="store_true", help="Require batches for real, production-track, or complex specs.")
    parser.add_argument("--generated", action="store_true", help="Generate temporary KiCad projects for each batch.")
    parser.add_argument("--generator", type=Path, default=Path("scripts/generate_project.py"))
    parser.add_argument("--generator-arg", action="append", default=[])
    parser.add_argument("--keep-temp", action="store_true", help="Keep passing temporary batch projects for inspection.")
    args = parser.parse_args(argv[1:])
    result = CheckResult()
    try:
        spec = load_spec(args.spec)
        check_schema(spec, result)
        check_net_graph(spec, result)
        check_connectivity_batches(spec, result, require_batches=args.require_batches, auto_require=args.auto_require)
        if args.generated and result.ok():
            generator = resolve_spec_project_path(args.spec, spec, args.generator)
            check_generated_connectivity_batches(
                args.spec,
                spec,
                result,
                generator,
                generator_args=args.generator_arg,
                keep_temp=args.keep_temp,
            )
    except Exception as error:
        result.issue(str(error))
    return print_result("connectivity_batch_check", result, args.json_output)


def evidence_manifest_main(argv: list[str]) -> int:
    parser = parser_with_spec("Validate release evidence manifest files and order-ready evidence.")
    parser.add_argument("--order-ready", action="store_true")
    args = parser.parse_args(argv[1:])
    result = CheckResult()
    try:
        spec = load_spec(args.spec)
        check_evidence_manifest(spec, result, order_ready=args.order_ready)
    except Exception as error:
        result.issue(str(error))
    return print_result("evidence_manifest_check", result, args.json_output)


def main_by_name(script_name: str, argv: list[str]) -> int:
    entrypoints = {
        "spec_schema_check.py": schema_main,
        "spec_net_graph_check.py": net_graph_main,
        "connectivity_batch_check.py": connectivity_batch_main,
        "evidence_manifest_check.py": evidence_manifest_main,
    }
    entrypoint = entrypoints.get(script_name)
    if entrypoint is None:
        print(f"Unknown check entrypoint: {script_name}", file=sys.stderr)
        return 2
    return entrypoint(argv)
