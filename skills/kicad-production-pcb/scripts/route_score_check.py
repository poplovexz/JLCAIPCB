#!/usr/bin/env python3
"""Score generated KiCad PCB routing against spec-declared routing limits."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pcb_skill_checks import CheckResult, get_path, load_spec, load_yaml, print_result, string_value  # noqa: E402
from _routing_stage import route_snapshot  # noqa: E402


def mapping_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def policy_path(spec: dict[str, Any]) -> Path:
    configured = get_path(spec, "validation.freerouting.policy_file")
    if string_value(configured):
        path = Path(str(configured))
        return path if path.is_absolute() else Path.cwd() / path
    return Path(__file__).resolve().parents[1] / "assets" / "freerouting-routing-policy.yaml"


def load_policy(spec: dict[str, Any]) -> dict[str, Any]:
    try:
        return load_yaml(policy_path(spec))
    except (OSError, ValueError, yaml.YAMLError):
        return {}


def enabled(spec: dict[str, Any], force: bool = False) -> bool:
    if force:
        return True
    validation = mapping_value(spec.get("validation"))
    config = mapping_value(validation.get("freerouting"))
    routing = mapping_value(spec.get("routing"))
    freerouting = mapping_value(routing.get("freerouting"))
    return config.get("required") is True or freerouting.get("enabled") is True


def resolve_path(spec_path: Path, value: Any) -> Path | None:
    if not string_value(value):
        return None
    path = Path(str(value))
    if path.is_absolute():
        return path
    candidate = Path.cwd() / path
    if candidate.exists():
        return candidate
    return (spec_path.parent / path).resolve()


def default_pcb_path(spec: dict[str, Any]) -> Path | None:
    project = mapping_value(spec.get("project"))
    if not string_value(project.get("output_dir")) or not string_value(project.get("name")):
        return None
    return Path(str(project["output_dir"])) / f"{project['name']}.kicad_pcb"


def configured_pcb_path(spec: dict[str, Any], spec_path: Path) -> Path | None:
    configured = get_path(spec, "routing.freerouting.pcb_file")
    path = resolve_path(spec_path, configured)
    return path or default_pcb_path(spec)


def extract_blocks(text: str, symbol: str) -> list[str]:
    blocks: list[str] = []
    token = f"({symbol}"
    position = 0
    while True:
        start = text.find(token, position)
        if start < 0:
            return blocks
        depth = 0
        for index in range(start, len(text)):
            char = text[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    blocks.append(text[start : index + 1])
                    position = index + 1
                    break
        else:
            return blocks


def number_pair(block: str, key: str) -> tuple[float, float] | None:
    match = re.search(rf"\({re.escape(key)}\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\)", block)
    if not match:
        return None
    return float(match.group(1)), float(match.group(2))


def number_value(block: str, key: str) -> float | None:
    match = re.search(rf"\({re.escape(key)}\s+([-+0-9.eE]+)\)", block)
    return float(match.group(1)) if match else None


def string_field(block: str, key: str) -> str:
    match = re.search(rf"\({re.escape(key)}\s+\"([^\"]+)\"\)", block)
    return match.group(1) if match else ""


def parse_pcb(path: Path) -> dict[str, Any]:
    result = CheckResult()
    snapshot = route_snapshot(path, result)
    if result.issues:
        raise ValueError("; ".join(result.issues))
    metrics = mapping_value(snapshot.get("metrics_by_net"))
    snapshot["length_by_net_mm"] = {net: float(item.get("length_mm", 0)) for net, item in metrics.items() if isinstance(item, dict)}
    snapshot["vias_by_net"] = {net: int(item.get("vias", 0)) for net, item in metrics.items() if isinstance(item, dict)}
    return snapshot


def length_by_net(segments: list[dict[str, Any]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for segment in segments:
        net = str(segment.get("net") or "")
        result[net] = result.get(net, 0.0) + float(segment.get("length_mm") or 0.0)
    return dict(sorted(result.items()))


def count_by_net(items: list[dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in items:
        net = str(item.get("net") or "")
        result[net] = result.get(net, 0) + 1
    return dict(sorted(result.items()))


def score_metrics(metrics: dict[str, Any], spec: dict[str, Any], policy: dict[str, Any]) -> int:
    weights = mapping_value(policy.get("score_weights"))
    configured = mapping_value(get_path(spec, "validation.freerouting.score_weights"))
    weights = {**weights, **configured}
    return int(
        float(weights.get("via", 0)) * int(metrics.get("via_count", 0))
        + float(weights.get("segment_mm", 0)) * float(metrics.get("total_length_mm", 0.0))
    )


def check_limits(metrics: dict[str, Any], spec: dict[str, Any], policy: dict[str, Any], result: CheckResult) -> None:
    limits = {**mapping_value(policy.get("default_limits")), **mapping_value(get_path(spec, "validation.freerouting.route_score"))}
    if "max_total_length_mm" in limits and float(metrics["total_length_mm"]) > float(limits["max_total_length_mm"]):
        result.issue(f"route total length {metrics['total_length_mm']:.3f}mm exceeds max_total_length_mm {limits['max_total_length_mm']}")
    if "max_vias" in limits and int(metrics["via_count"]) > int(limits["max_vias"]):
        result.issue(f"route via count {metrics['via_count']} exceeds max_vias {limits['max_vias']}")
    max_length_by_net = mapping_value(limits.get("max_length_by_net_mm"))
    for net, max_length in max_length_by_net.items():
        actual = float(metrics["length_by_net_mm"].get(str(net), 0.0))
        if actual > float(max_length):
            result.issue(f"{net} route length {actual:.3f}mm exceeds max_length_by_net_mm {max_length}")
    max_vias_by_net = mapping_value(limits.get("max_vias_by_net"))
    for net, max_vias in max_vias_by_net.items():
        actual = int(metrics["vias_by_net"].get(str(net), 0))
        if actual > int(max_vias):
            result.issue(f"{net} via count {actual} exceeds max_vias_by_net {max_vias}")
    required_nets = [str(item) for item in list_value(limits.get("required_routed_nets")) if string_value(item)]
    for net in required_nets:
        if float(metrics["length_by_net_mm"].get(net, 0.0)) <= 0:
            result.issue(f"{net} has no routed segment in PCB routing score")


def check_route_score(spec: dict[str, Any], spec_path: Path, result: CheckResult, force: bool = False) -> dict[str, Any]:
    policy = load_policy(spec)
    active = enabled(spec, force=force)
    details: dict[str, Any] = {"enabled": active}
    if not active:
        result.warning("freerouting route score gate not required for this spec")
        return details
    pcb_path = configured_pcb_path(spec, spec_path)
    if not pcb_path:
        result.issue("routing.freerouting.pcb_file or project.output_dir/project.name must identify a PCB file")
        return details
    details["pcb_file"] = str(pcb_path)
    if not pcb_path.exists():
        result.issue(f"Missing PCB file for route scoring: {pcb_path}")
        return details
    metrics = parse_pcb(pcb_path)
    metrics["score"] = score_metrics(metrics, spec, policy)
    details.update(metrics)
    if int(metrics["segment_count"]) == 0:
        result.issue("PCB has no routed segments")
    check_limits(metrics, spec, policy, result)
    return details


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Score KiCad PCB routing against spec-declared limits.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--require", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])

    result = CheckResult()
    details: dict[str, Any] = {}
    try:
        details = check_route_score(load_spec(args.spec), args.spec, result, force=args.require)
    except Exception as error:
        result.issue(str(error))

    if args.json_output:
        payload = {
            "check": "route_score_check",
            "ok": result.ok(),
            "issues": result.issues,
            "warnings": result.warnings,
            "details": details,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if result.ok() else 1
    if details.get("enabled") and "score" in details:
        print(
            "route_score_check metrics: "
            f"score={details['score']} segments={details['segment_count']} vias={details['via_count']} "
            f"total_length_mm={details['total_length_mm']:.3f}"
        )
    return print_result("route_score_check", result, False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
