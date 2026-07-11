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
from _pcb_skill_checks import CheckResult, get_path, sha256_file, string_value


SKILL_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = SKILL_ROOT / "assets" / "routing-stage-policy.yaml"


def load_policy() -> dict[str, Any]:
    data = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("routing stage policy must be a mapping")
    return data


def normalized(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-")


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
    copper_count = int(get_path(spec, "board.layers.copper") or 2)
    allowed_board_layers = {"F.Cu", "B.Cu"} | {f"In{index}.Cu" for index in range(1, max(copper_count - 1, 1))}
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
        for field in ["min_via_diameter_mm", "min_via_drill_mm"]:
            if field in item and (not isinstance(item[field], (int, float)) or isinstance(item[field], bool) or float(item[field]) <= 0):
                result.issue(f"{label}.{field} must be positive numeric")
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
    records: list[dict[str, Any]] = []
    metrics: dict[str, dict[str, Any]] = {}
    for item in board.GetTracks():
        net = str(item.GetNetname())
        metric = metrics.setdefault(net, {"length_mm": 0.0, "vias": 0, "segments": 0, "layers": set(), "min_width_mm": math.inf, "min_via_diameter_mm": math.inf, "min_via_drill_mm": math.inf})
        if isinstance(item, pcbnew.PCB_VIA):
            position = item.GetPosition()
            layers = [board.GetLayerName(item.TopLayer()), board.GetLayerName(item.BottomLayer())]
            record = {"type": "via", "net": net, "at": [pcbnew.ToMM(position.x), pcbnew.ToMM(position.y)], "size_mm": pcbnew.ToMM(item.GetWidth(item.TopLayer())), "drill_mm": pcbnew.ToMM(item.GetDrillValue()), "layers": layers}
            metric["vias"] += 1
            metric["min_via_diameter_mm"] = min(metric["min_via_diameter_mm"], float(record["size_mm"]))
            metric["min_via_drill_mm"] = min(metric["min_via_drill_mm"], float(record["drill_mm"]))
            metric["layers"].update(layers)
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
        normalized_metrics[net] = {**item, "length_mm": round(item["length_mm"], 6), "min_width_mm": None if math.isinf(item["min_width_mm"]) else round(item["min_width_mm"], 6), "min_via_diameter_mm": None if math.isinf(item["min_via_diameter_mm"]) else round(item["min_via_diameter_mm"], 6), "min_via_drill_mm": None if math.isinf(item["min_via_drill_mm"]) else round(item["min_via_drill_mm"], 6), "layers": sorted(item["layers"])}
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
        if int(actual.get("vias", 0)) > int(rule.get("max_vias", 0)):
            result.issue(f"routing net {net} exceeds max_vias")
        for field in ["min_via_diameter_mm", "min_via_drill_mm"]:
            if int(actual.get("vias", 0)) and field in rule and float(actual.get(field) or 0) + 1e-9 < float(rule[field]):
                result.issue(f"routing net {net} is below {field}")
        if not set(strings(actual.get("layers"))) <= set(strings(rule.get("allowed_layers"))):
            result.issue(f"routing net {net} uses a forbidden copper layer")
        if "max_length_mm" in rule and float(actual.get("length_mm", 0)) > float(rule["max_length_mm"]):
            result.issue(f"routing net {net} exceeds max_length_mm")
        check_route_topology(net, rule, snapshot, load_policy(), result)
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
        check_differential_pair_geometry(pair, snapshot, load_policy(), result)


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
