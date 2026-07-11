#!/usr/bin/env python3
"""Run spec-driven SI/PI/EMC/thermal risk prechecks.

These checks reduce obvious design risk. They are not compliance, simulation,
or lab-test proof.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pcb_skill_checks import CheckResult, get_path, load_spec, net_names, positive_number, print_result, string_value  # noqa: E402


def mapping_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def number_or_none(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def bool_config(config: dict[str, Any], key: str, default: bool = False) -> bool:
    value = config.get(key)
    if value is None:
        return default
    return bool(value)


def validation_config(spec: dict[str, Any], key: str) -> dict[str, Any]:
    validation = mapping_value(spec.get("validation"))
    return mapping_value(validation.get(key))


def verification_config(spec: dict[str, Any]) -> dict[str, Any]:
    return mapping_value(spec.get("verification"))


def explicitly_not_applicable(section: Any) -> bool:
    return isinstance(section, dict) and str(section.get("status", "")).strip().lower() == "not_applicable"


def route_net_set(spec: dict[str, Any]) -> set[str]:
    return {str(route.get("net")) for route in list_value(spec.get("routes")) if isinstance(route, dict) and string_value(route.get("net"))}


def routes_by_net(spec: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for route in list_value(spec.get("routes")):
        if isinstance(route, dict) and string_value(route.get("net")):
            grouped.setdefault(str(route["net"]), []).append(route)
    return grouped


def declared_vias_by_net(spec: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for via in list_value(spec.get("vias")):
        if isinstance(via, dict) and string_value(via.get("net")):
            counts[str(via["net"])] = counts.get(str(via["net"]), 0) + 1
    return counts


def route_length_mm(route: dict[str, Any]) -> float | None:
    value = route.get("length_mm")
    if positive_number(value):
        return float(value)
    length = 0.0
    points: list[dict[str, Any]] = []
    for key in ["from", "to"]:
        endpoint = route.get(key)
        if isinstance(endpoint, dict) and isinstance(endpoint.get("point_mm"), dict):
            points.append(endpoint["point_mm"])
    waypoints = [item for item in list_value(route.get("waypoints_mm")) if isinstance(item, dict)]
    if len(points) == 2:
        ordered = [points[0], *waypoints, points[1]]
    else:
        return None
    for first, second in zip(ordered, ordered[1:]):
        x1 = number_or_none(first.get("x"))
        y1 = number_or_none(first.get("y"))
        x2 = number_or_none(second.get("x"))
        y2 = number_or_none(second.get("y"))
        if None in {x1, y1, x2, y2}:
            return None
        length += math.hypot(float(x2) - float(x1), float(y2) - float(y1))
    return length


def total_route_length_by_net(spec: dict[str, Any]) -> dict[str, float]:
    lengths: dict[str, float] = {}
    for net, routes in routes_by_net(spec).items():
        total = 0.0
        known = False
        for route in routes:
            length = route_length_mm(route)
            if length is not None:
                total += length
                known = True
        if known:
            lengths[net] = total
    return lengths


def check_net_exists(name: str, net: Any, declared_nets: set[str], result: CheckResult) -> str | None:
    if not string_value(net):
        result.issue(f"{name} must name a net")
        return None
    net_name = str(net)
    if net_name not in declared_nets:
        result.issue(f"{name} references undeclared net: {net_name}")
    return net_name


def check_signal_integrity(spec: dict[str, Any], result: CheckResult, required: bool = False) -> bool:
    config = mapping_value(verification_config(spec).get("signal_integrity"))
    if not required and not config:
        return False
    declared_nets = net_names(spec)
    routed_nets = route_net_set(spec)
    lengths = total_route_length_by_net(spec)
    via_counts = declared_vias_by_net(spec)
    executed = True

    differential_pairs = list_value(config.get("differential_pairs"))
    single_ended = list_value(config.get("single_ended_nets"))
    if required and not differential_pairs and not single_ended:
        result.issue("verification.signal_integrity must declare differential_pairs or single_ended_nets")

    for index, pair in enumerate(differential_pairs):
        if not isinstance(pair, dict):
            result.issue(f"verification.signal_integrity.differential_pairs[{index}] must be a mapping")
            continue
        label = str(pair.get("name", f"differential_pairs[{index}]"))
        nets = list_value(pair.get("nets"))
        if len(nets) != 2:
            result.issue(f"{label}.nets must contain exactly two nets")
            continue
        pair_nets = [check_net_exists(f"{label}.nets[{net_index}]", net, declared_nets, result) for net_index, net in enumerate(nets)]
        for net in [item for item in pair_nets if item]:
            if bool_config(pair, "require_routes", True) and net not in routed_nets:
                result.issue(f"{label}.{net} has no declared route")
        if bool_config(pair, "stackup_required", False) and not mapping_value(spec.get("board")).get("stackup"):
            result.issue(f"{label} requires board.stackup")
        if pair.get("impedance_ohm") is not None and not positive_number(pair.get("impedance_ohm")):
            result.issue(f"{label}.impedance_ohm must be a positive number when present")
        elif bool_config(pair, "require_impedance_target", False) and not positive_number(pair.get("impedance_ohm")):
            result.issue(f"{label}.impedance_ohm is required")
        max_skew = pair.get("max_skew_mm")
        if max_skew is not None and not positive_number(max_skew):
            result.issue(f"{label}.max_skew_mm must be a positive number when present")
        elif positive_number(max_skew) and all(net in lengths for net in pair_nets if net):
            skew = abs(lengths[str(pair_nets[0])] - lengths[str(pair_nets[1])])
            if skew > float(max_skew):
                result.issue(f"{label} length skew {skew:.3g}mm exceeds max_skew_mm {float(max_skew):.3g}mm")
        elif bool_config(pair, "require_length_check", False):
            result.issue(f"{label} requires length_mm or point_mm route geometry for both nets")
        max_vias = pair.get("max_vias_per_net")
        if max_vias is not None:
            if not isinstance(max_vias, int) or isinstance(max_vias, bool) or max_vias < 0:
                result.issue(f"{label}.max_vias_per_net must be a non-negative integer")
            else:
                for net in [item for item in pair_nets if item]:
                    if via_counts.get(net, 0) > max_vias:
                        result.issue(f"{label}.{net} has {via_counts.get(net, 0)} vias, above max_vias_per_net {max_vias}")

    for index, item in enumerate(single_ended):
        if not isinstance(item, dict):
            result.issue(f"verification.signal_integrity.single_ended_nets[{index}] must be a mapping")
            continue
        label = str(item.get("name", f"single_ended_nets[{index}]"))
        net = check_net_exists(f"{label}.net", item.get("net"), declared_nets, result)
        if net and bool_config(item, "require_route", True) and net not in routed_nets:
            result.issue(f"{label}.{net} has no declared route")
        max_length = item.get("max_length_mm")
        if max_length is not None and not positive_number(max_length):
            result.issue(f"{label}.max_length_mm must be a positive number when present")
        elif net and positive_number(max_length):
            if net not in lengths:
                result.issue(f"{label}.{net} requires length_mm or point_mm route geometry")
            elif lengths[net] > float(max_length):
                result.issue(f"{label}.{net} length {lengths[net]:.3g}mm exceeds max_length_mm {float(max_length):.3g}mm")
    return executed


def current_limit_from_power_domains(spec: dict[str, Any], name: str) -> float | None:
    for domain in list_value(spec.get("power_domains")):
        if isinstance(domain, dict) and str(domain.get("name")) == name:
            source = mapping_value(domain.get("source"))
            value = source.get("current_limit_a")
            if positive_number(value):
                return float(value)
    return None


def min_route_width(spec: dict[str, Any], nets: list[str]) -> float | None:
    widths: list[float] = []
    for route in list_value(spec.get("routes")):
        if not isinstance(route, dict) or str(route.get("net")) not in nets:
            continue
        if positive_number(route.get("width_mm")):
            widths.append(float(route["width_mm"]))
    return min(widths) if widths else None


def check_power_integrity(spec: dict[str, Any], result: CheckResult, required: bool = False) -> bool:
    config = mapping_value(verification_config(spec).get("power_integrity"))
    if not required and not config:
        return False
    rails = list_value(config.get("rails"))
    if required and not rails:
        result.issue("verification.power_integrity.rails must be a non-empty list")
    declared_nets = net_names(spec)
    routed_nets = route_net_set(spec)

    for index, rail in enumerate(rails):
        if not isinstance(rail, dict):
            result.issue(f"verification.power_integrity.rails[{index}] must be a mapping")
            continue
        label = str(rail.get("name", f"rails[{index}]"))
        if not string_value(rail.get("name")):
            result.issue(f"{label}.name must be a non-empty string")
        if not positive_number(rail.get("nominal_v")):
            result.issue(f"{label}.nominal_v must be a positive number")
        if not positive_number(rail.get("max_load_a")) and not positive_number(rail.get("max_load_ma")):
            result.issue(f"{label} must declare max_load_a or max_load_ma")
        if rail.get("tolerance_percent") is not None and not positive_number(rail.get("tolerance_percent")):
            result.issue(f"{label}.tolerance_percent must be a positive number when present")
        nets = [str(net) for net in list_value(rail.get("nets")) if string_value(net)]
        if not nets:
            result.issue(f"{label}.nets must declare at least one power net")
        for net in nets:
            if net not in declared_nets:
                result.issue(f"{label}.nets references undeclared net: {net}")
            if bool_config(rail, "require_routes", True) and net not in routed_nets:
                result.issue(f"{label}.{net} has no declared route")
        min_width = rail.get("min_route_width_mm")
        if min_width is not None and not positive_number(min_width):
            result.issue(f"{label}.min_route_width_mm must be a positive number when present")
        elif positive_number(min_width):
            observed_width = min_route_width(spec, nets)
            if observed_width is None:
                result.issue(f"{label} has no route width data for declared nets")
            elif observed_width < float(min_width):
                result.issue(f"{label} minimum route width {observed_width:.3g}mm is below required {float(min_width):.3g}mm")
        domain = rail.get("power_domain")
        if string_value(domain):
            current_limit = current_limit_from_power_domains(spec, str(domain))
            if current_limit is None:
                result.issue(f"{label}.power_domain {domain} is not declared in power_domains with source.current_limit_a")
    return True


def check_thermal(spec: dict[str, Any], result: CheckResult, required: bool = False) -> bool:
    config = mapping_value(verification_config(spec).get("thermal"))
    if not required and not config:
        return False
    components = list_value(config.get("components"))
    if required and not components:
        result.issue("verification.thermal.components must be a non-empty list")
    refs = set(mapping_value(item).get("ref") for item in list_value(spec.get("components")) if isinstance(item, dict))
    ambient = config.get("ambient_c")
    if ambient is not None and not isinstance(ambient, (int, float)):
        result.issue("verification.thermal.ambient_c must be numeric when present")
    default_ambient = float(ambient) if isinstance(ambient, (int, float)) and not isinstance(ambient, bool) else None

    for index, component in enumerate(components):
        if not isinstance(component, dict):
            result.issue(f"verification.thermal.components[{index}] must be a mapping")
            continue
        label = str(component.get("ref", f"components[{index}]"))
        if not string_value(component.get("ref")):
            result.issue(f"{label}.ref must be a non-empty string")
        elif component["ref"] not in refs:
            result.issue(f"{label}.ref is not a declared component")
        if not positive_number(component.get("max_power_w")):
            result.issue(f"{label}.max_power_w must be a positive number")
        if not positive_number(component.get("max_case_temp_c")) and not positive_number(component.get("max_junction_temp_c")):
            result.issue(f"{label} must declare max_case_temp_c or max_junction_temp_c")
        theta = component.get("theta_ja_c_per_w")
        if theta is not None and not positive_number(theta):
            result.issue(f"{label}.theta_ja_c_per_w must be a positive number when present")
        ambient_c = component.get("ambient_c", default_ambient)
        if bool_config(component, "require_temperature_estimate", True):
            if not isinstance(ambient_c, (int, float)) or isinstance(ambient_c, bool):
                result.issue(f"{label} requires ambient_c for temperature estimate")
            elif positive_number(theta) and positive_number(component.get("max_power_w")):
                estimate = float(ambient_c) + float(theta) * float(component["max_power_w"])
                limit = component.get("max_junction_temp_c", component.get("max_case_temp_c"))
                if positive_number(limit) and estimate > float(limit):
                    result.issue(f"{label} estimated temperature {estimate:.3g}C exceeds limit {float(limit):.3g}C")
            elif not positive_number(theta):
                result.issue(f"{label} requires theta_ja_c_per_w for temperature estimate")
    return True


def disposition_ok(item: dict[str, Any]) -> bool:
    if item.get("accepted") is True or item.get("mitigated") is True:
        return True
    status = str(item.get("status", "")).strip().lower()
    return status in {"accepted", "mitigated", "reviewed", "not_applicable", "closed"}


def check_emc(spec: dict[str, Any], result: CheckResult, required: bool = False) -> bool:
    config = mapping_value(verification_config(spec).get("emc"))
    if not required and not config:
        return False
    risk_items = list_value(config.get("risk_items"))
    if required and not risk_items:
        result.issue("verification.emc.risk_items must be a non-empty list")
    allowed = {"external_cable", "switching_node", "motor_load", "wireless", "clock", "esd", "connector", "high_current_loop", "other"}
    for index, item in enumerate(risk_items):
        if not isinstance(item, dict):
            result.issue(f"verification.emc.risk_items[{index}] must be a mapping")
            continue
        label = str(item.get("name", f"risk_items[{index}]"))
        category = str(item.get("category", "")).strip()
        if category not in allowed:
            result.issue(f"{label}.category must be one of {', '.join(sorted(allowed))}")
        if not string_value(item.get("description")):
            result.issue(f"{label}.description must be a non-empty string")
        mitigations = list_value(item.get("mitigations"))
        evidence = list_value(item.get("evidence"))
        if not mitigations and not evidence and not disposition_ok(item):
            result.issue(f"{label} must declare mitigations, evidence, accepted:true, or status reviewed/mitigated/not_applicable")
        for mitigation_index, mitigation in enumerate(mitigations):
            if not isinstance(mitigation, dict) or not string_value(mitigation.get("description")):
                result.issue(f"{label}.mitigations[{mitigation_index}] must include description")
    return True


def run_preflight(spec: dict[str, Any], strict: bool = False) -> tuple[CheckResult, list[str]]:
    result = CheckResult()
    executed: list[str] = []
    config = verification_config(spec)
    validation = validation_config(spec, "verification")
    if not config and not strict and not bool_config(validation, "required"):
        return result, executed

    required_all = strict or bool_config(validation, "required")
    requirements = {
        "signal_integrity": required_all or bool_config(validation, "signal_integrity_required"),
        "power_integrity": required_all or bool_config(validation, "power_integrity_required"),
        "thermal": required_all or bool_config(validation, "thermal_required"),
        "emc": required_all or bool_config(validation, "emc_required"),
    }
    checks = [
        ("signal_integrity", "SI risk precheck", check_signal_integrity, requirements["signal_integrity"]),
        ("power_integrity", "PI risk precheck", check_power_integrity, requirements["power_integrity"]),
        ("thermal", "thermal estimate precheck", check_thermal, requirements["thermal"]),
        ("emc", "EMC risk precheck", check_emc, requirements["emc"]),
    ]
    for section_name, label, check, required in checks:
        if explicitly_not_applicable(config.get(section_name)):
            executed.append(f"{label} disposition")
            result.warning(f"{label} is explicitly not applicable; this disposition is not simulation or lab proof")
            continue
        before = len(result.issues)
        executed_check = check(spec, result, required=required)
        if executed_check:
            executed.append(label)
            after = len(result.issues)
            if after == before:
                result.warning(f"{label} passed as a risk precheck only; it is not compliance, simulation, or lab proof")
    return result, executed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run spec-driven SI/PI/EMC/thermal risk prechecks.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])

    result = CheckResult()
    executed: list[str] = []
    try:
        spec = load_spec(args.spec)
        result, executed = run_preflight(spec, strict=args.strict)
    except Exception as error:
        result.issue(str(error))

    if args.json_output:
        print(json.dumps({"name": "verification_preflight", "ok": result.ok(), "issues": result.issues, "warnings": result.warnings, "executed": executed}, indent=2))
        return 0 if result.ok() else 1
    if executed:
        print("verification_preflight executed: " + ", ".join(executed))
    else:
        print("verification_preflight executed: no verification risk prechecks requested")
    return print_result("verification_preflight", result, False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
