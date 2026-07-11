#!/usr/bin/env python3
"""Transactional spec patch helpers for kicad-production-pcb."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pcb_skill_checks import (  # noqa: E402
    CheckResult,
    actual_net_graph,
    check_connectivity_batches,
    check_generated_connectivity_batches,
    check_net_graph,
    check_schema,
    load_spec,
    load_yaml,
    net_names,
    parse_pin,
    pin_id,
    print_result,
    resolve_spec_project_path,
    safe_name,
    string_value,
)


ALLOWED_TOP_LEVEL_KEYS = {"proposal", "add", "extend"}
FORBIDDEN_TOP_LEVEL_KEYS = {"set", "update", "modify", "replace", "delete", "remove", "patch"}
ALLOWED_ADD_KEYS = {"nets", "components", "routes", "connectivity_batches", "expected_net_graph"}
ALLOWED_EXTEND_KEYS = {"expected_net_graph"}
LIST_SECTIONS = ("nets", "components", "routes", "connectivity_batches")


def proposal_id(proposal: dict[str, Any], proposal_path: Path) -> str:
    metadata = proposal.get("proposal", {})
    if isinstance(metadata, dict) and string_value(metadata.get("id")):
        return safe_name(str(metadata["id"]))
    return safe_name(proposal_path.stem)


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def expected_nets_container(spec: dict[str, Any]) -> dict[str, Any]:
    graph = spec.setdefault("expected_net_graph", {})
    if not isinstance(graph, dict):
        spec["expected_net_graph"] = {}
        graph = spec["expected_net_graph"]
    nets = graph.setdefault("nets", {})
    if not isinstance(nets, dict):
        graph["nets"] = {}
        nets = graph["nets"]
    return nets


def expected_nets_view(spec: dict[str, Any]) -> dict[str, Any]:
    graph = spec.get("expected_net_graph", {})
    if not isinstance(graph, dict):
        return {}
    nets = graph.get("nets", graph)
    return nets if isinstance(nets, dict) else {}


def graph_nets_view(graph: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(graph, dict):
        return {}
    nets = graph.get("nets", graph)
    return nets if isinstance(nets, dict) else {}


def pin_set_from_rule(rule: Any, field: str) -> set[str]:
    if not isinstance(rule, dict):
        return set()
    value = rule.get(field, [])
    return {str(item) for item in value if string_value(item)} if isinstance(value, list) else set()


def unique_append(existing: list[Any], additions: list[Any]) -> list[Any]:
    result = list(existing)
    seen = {json.dumps(item, sort_keys=True, ensure_ascii=False) for item in result}
    for item in additions:
        key = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            result.append(item)
            seen.add(key)
    return result


def named_items(items: list[Any], name_field: str) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for item in items:
        if isinstance(item, dict) and string_value(item.get(name_field)):
            output[str(item[name_field])] = item
    return output


def validate_proposal_shape(spec: dict[str, Any], proposal: dict[str, Any], result: CheckResult) -> None:
    unknown = sorted(set(proposal) - ALLOWED_TOP_LEVEL_KEYS - FORBIDDEN_TOP_LEVEL_KEYS)
    for key in unknown:
        result.issue(f"proposal contains unsupported top-level key: {key}")
    for key in sorted(set(proposal) & FORBIDDEN_TOP_LEVEL_KEYS):
        result.issue(f"proposal uses forbidden mutating key: {key}")

    metadata = proposal.get("proposal", {})
    if metadata is not None and not isinstance(metadata, dict):
        result.issue("proposal.proposal must be a mapping")
    if isinstance(metadata, dict):
        target = metadata.get("target_project")
        current = spec.get("project", {}).get("name") if isinstance(spec.get("project"), dict) else None
        if string_value(target) and string_value(current) and str(target) != str(current):
            result.issue(f"proposal target_project {target} does not match spec project {current}")

    add = proposal.get("add", {})
    extend = proposal.get("extend", {})
    if add is None:
        add = {}
    if extend is None:
        extend = {}
    if not isinstance(add, dict):
        result.issue("proposal.add must be a mapping")
        add = {}
    if not isinstance(extend, dict):
        result.issue("proposal.extend must be a mapping")
        extend = {}
    if not add and not extend:
        result.issue("proposal must contain add or extend changes")

    for key in sorted(set(add) - ALLOWED_ADD_KEYS):
        result.issue(f"proposal.add contains unsupported section: {key}")
    for key in sorted(set(extend) - ALLOWED_EXTEND_KEYS):
        result.issue(f"proposal.extend contains unsupported section: {key}")

    for section in ["nets", "components", "routes", "connectivity_batches"]:
        if section in add and not isinstance(add[section], list):
            result.issue(f"proposal.add.{section} must be a list")

    if "expected_net_graph" in add and not isinstance(add["expected_net_graph"], dict):
        result.issue("proposal.add.expected_net_graph must be a mapping")
    if "expected_net_graph" in extend and not isinstance(extend["expected_net_graph"], dict):
        result.issue("proposal.extend.expected_net_graph must be a mapping")


def validate_additive_collisions(spec: dict[str, Any], proposal: dict[str, Any], result: CheckResult) -> None:
    add = proposal.get("add", {}) if isinstance(proposal.get("add"), dict) else {}
    existing_nets = net_names(spec)
    existing_refs = set(named_items(as_list(spec.get("components")), "ref"))
    existing_batch_names = set(named_items(as_list(spec.get("connectivity_batches")), "name"))
    added_nets: set[str] = set()
    added_refs: set[str] = set()

    for item in as_list(add.get("nets")):
        if not isinstance(item, dict) or not string_value(item.get("name")):
            result.issue("proposal.add.nets entries must contain name")
            continue
        name = str(item["name"])
        if name in existing_nets:
            result.issue(f"proposal attempts to add existing net: {name}")
        if name in added_nets:
            result.issue(f"proposal adds duplicate net: {name}")
        added_nets.add(name)

    for item in as_list(add.get("components")):
        if not isinstance(item, dict) or not string_value(item.get("ref")):
            result.issue("proposal.add.components entries must contain ref")
            continue
        ref = str(item["ref"])
        if ref in existing_refs:
            result.issue(f"proposal attempts to add existing component: {ref}")
        if ref in added_refs:
            result.issue(f"proposal adds duplicate component: {ref}")
        added_refs.add(ref)

    known_nets = existing_nets | added_nets
    for component in as_list(add.get("components")):
        if not isinstance(component, dict):
            continue
        ref = str(component.get("ref", "<component>"))
        pads = component.get("pads")
        if not isinstance(pads, dict) or not pads:
            result.issue(f"proposal component {ref} must contain pads")
            continue
        for pad, net in pads.items():
            if not string_value(str(pad)):
                result.issue(f"proposal component {ref} contains an empty pad key")
            if not string_value(net) or str(net) not in known_nets:
                result.issue(f"proposal component {ref}.{pad} references unknown net: {net}")

    for item in as_list(add.get("connectivity_batches")):
        if not isinstance(item, dict) or not string_value(item.get("name")):
            result.issue("proposal.add.connectivity_batches entries must contain name")
            continue
        name = str(item["name"])
        if name in existing_batch_names:
            result.issue(f"proposal attempts to add existing connectivity batch: {name}")
        existing_batch_names.add(name)

    added_expected = graph_nets_view(add.get("expected_net_graph", {})) if isinstance(add.get("expected_net_graph"), dict) else {}
    existing_expected = expected_nets_view(spec)
    for net in added_expected:
        if str(net) in existing_expected:
            result.issue(f"proposal.add.expected_net_graph attempts to add existing expected net: {net}")


def check_patch(spec: dict[str, Any], proposal: dict[str, Any], result: CheckResult) -> None:
    validate_proposal_shape(spec, proposal, result)
    validate_additive_collisions(spec, proposal, result)
    if result.ok():
        merged = apply_patch_to_spec(spec, proposal)
        guard = CheckResult()
        check_diff_guard(spec, merged, guard)
        for warning in guard.warnings:
            result.warning(warning)
        for issue in guard.issues:
            result.issue(issue)
        check_schema(merged, result)
        check_net_graph(merged, result)
        check_connectivity_batches(merged, result)


def apply_expected_additions(target: dict[str, Any], additions: dict[str, Any]) -> None:
    target_nets = expected_nets_container(target)
    for net, rule in graph_nets_view(additions).items():
        if str(net) not in target_nets:
            target_nets[str(net)] = copy.deepcopy(rule)


def apply_expected_extensions(target: dict[str, Any], extensions: dict[str, Any]) -> None:
    target_nets = expected_nets_container(target)
    for net, rule in graph_nets_view(extensions).items():
        net_name = str(net)
        existing = target_nets.setdefault(net_name, {})
        if not isinstance(existing, dict):
            target_nets[net_name] = {}
            existing = target_nets[net_name]
        if not isinstance(rule, dict):
            continue
        for field in ["pins", "required_pins"]:
            if field not in rule:
                continue
            additions = rule.get(field, [])
            if isinstance(additions, list):
                existing[field] = unique_append(as_list(existing.get(field)), additions)
        for field, value in rule.items():
            if field not in {"pins", "required_pins"} and field not in existing:
                existing[field] = copy.deepcopy(value)


def apply_patch_to_spec(spec: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(spec)
    add = proposal.get("add", {}) if isinstance(proposal.get("add"), dict) else {}
    extend = proposal.get("extend", {}) if isinstance(proposal.get("extend"), dict) else {}

    for section in LIST_SECTIONS:
        if isinstance(add.get(section), list):
            merged.setdefault(section, [])
            merged[section] = as_list(merged.get(section)) + copy.deepcopy(add[section])

    if isinstance(add.get("expected_net_graph"), dict):
        apply_expected_additions(merged, add["expected_net_graph"])
    if isinstance(extend.get("expected_net_graph"), dict):
        apply_expected_extensions(merged, extend["expected_net_graph"])
    return merged


def existing_items_unchanged(before: list[Any], after: list[Any], key: str, result: CheckResult, section: str) -> None:
    before_map = named_items(before, key)
    after_map = named_items(after, key)
    for name, item in before_map.items():
        if name not in after_map:
            result.issue(f"diff guard blocked deletion from {section}: {name}")
        elif after_map[name] != item:
            result.issue(f"diff guard blocked modification in {section}: {name}")


def routes_prefix_unchanged(before: list[Any], after: list[Any], result: CheckResult) -> None:
    if len(after) < len(before):
        result.issue("diff guard blocked route deletion")
        return
    for index, item in enumerate(before):
        if after[index] != item:
            result.issue(f"diff guard blocked modification in routes[{index}]")


def expected_graph_additive(before: dict[str, Any], after: dict[str, Any], result: CheckResult) -> None:
    before_nets = expected_nets_view(before)
    after_nets = expected_nets_view(after)
    for net, before_rule in before_nets.items():
        if net not in after_nets:
            result.issue(f"diff guard blocked expected_net_graph deletion: {net}")
            continue
        after_rule = after_nets[net]
        if not isinstance(before_rule, dict) or not isinstance(after_rule, dict):
            if before_rule != after_rule:
                result.issue(f"diff guard blocked expected_net_graph modification: {net}")
            continue
        for field, value in before_rule.items():
            if field in {"pins", "required_pins"}:
                missing = pin_set_from_rule(before_rule, field) - pin_set_from_rule(after_rule, field)
                if missing:
                    result.issue(f"diff guard blocked expected_net_graph {net}.{field} removal: {', '.join(sorted(missing))}")
            elif after_rule.get(field) != value:
                result.issue(f"diff guard blocked expected_net_graph {net}.{field} modification")


def check_diff_guard(before: dict[str, Any], after: dict[str, Any], result: CheckResult) -> None:
    for key, value in before.items():
        if key in {"nets", "components", "routes", "connectivity_batches", "expected_net_graph"}:
            continue
        if after.get(key) != value:
            result.issue(f"diff guard blocked modification to top-level section: {key}")
    for key in after:
        if key not in before and key not in {"expected_net_graph"}:
            result.issue(f"diff guard blocked new top-level section: {key}")

    existing_items_unchanged(as_list(before.get("nets")), as_list(after.get("nets")), "name", result, "nets")
    existing_items_unchanged(as_list(before.get("components")), as_list(after.get("components")), "ref", result, "components")
    existing_items_unchanged(
        as_list(before.get("connectivity_batches")),
        as_list(after.get("connectivity_batches")),
        "name",
        result,
        "connectivity_batches",
    )
    routes_prefix_unchanged(as_list(before.get("routes")), as_list(after.get("routes")), result)
    expected_graph_additive(before, after, result)


def atomic_write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    os.replace(temp, path)


def transaction_temp_root(spec: dict[str, Any], spec_path: Path, patch_id: str) -> Path:
    project = spec.get("project", {}) if isinstance(spec.get("project"), dict) else {}
    artifacts = Path(str(project.get("artifacts_dir", "artifacts")))
    project_name = safe_name(str(project.get("name", spec_path.stem)))
    return artifacts / "spec_patch_transactions" / project_name / patch_id


def validation_spec_for_transaction(final_spec: dict[str, Any], temp_root: Path, patch_id: str) -> dict[str, Any]:
    validation_spec = copy.deepcopy(final_spec)
    project = validation_spec.setdefault("project", {})
    base_name = safe_name(str(project.get("name", "pcb")))
    project["name"] = f"{base_name}__txn_{patch_id}"
    project["output_dir"] = str(temp_root / "project")
    project["artifacts_dir"] = str(temp_root / "artifacts")
    return validation_spec


def transaction_report_path(spec: dict[str, Any], spec_path: Path, patch_id: str, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    root = transaction_temp_root(spec, spec_path, patch_id)
    return root / "transaction_report.json"


def run_transaction(
    spec_path: Path,
    proposal_path: Path,
    generator: Path,
    apply: bool = False,
    keep_temp: bool = False,
    report_path: Path | None = None,
) -> tuple[CheckResult, dict[str, Any]]:
    result = CheckResult()
    spec = load_spec(spec_path)
    proposal = load_yaml(proposal_path)
    patch_id = proposal_id(proposal, proposal_path)
    final_spec = apply_patch_to_spec(spec, proposal)
    temp_root = transaction_temp_root(spec, spec_path, patch_id)
    validation_spec = validation_spec_for_transaction(final_spec, temp_root, patch_id)
    validation_spec_path = temp_root / "candidate.spec.yaml"

    report: dict[str, Any] = {
        "spec": str(spec_path),
        "proposal": str(proposal_path),
        "proposal_id": patch_id,
        "apply": apply,
        "temp_root": str(temp_root),
        "candidate_spec": str(validation_spec_path),
        "ok": False,
        "issues": [],
        "warnings": [],
    }

    check_patch(spec, proposal, result)
    if result.ok():
        temp_root.mkdir(parents=True, exist_ok=True)
        atomic_write_yaml(validation_spec_path, validation_spec)
        check_schema(validation_spec, result)
        check_net_graph(validation_spec, result)
        check_connectivity_batches(validation_spec, result, require_batches=True)
        if result.ok():
            check_generated_connectivity_batches(validation_spec_path, validation_spec, result, generator, keep_temp=keep_temp)
    if result.ok() and apply:
        atomic_write_yaml(spec_path, final_spec)
        report["applied_spec"] = str(spec_path)
    report["ok"] = result.ok()
    report["issues"] = result.issues
    report["warnings"] = result.warnings
    target_report = transaction_report_path(spec, spec_path, patch_id, report_path)
    target_report.parent.mkdir(parents=True, exist_ok=True)
    target_report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["report"] = str(target_report)
    return result, report


def print_payload_result(name: str, result: CheckResult, payload: dict[str, Any], json_output: bool = False) -> int:
    if json_output:
        data = dict(payload)
        data.update({"check": name, "ok": result.ok(), "issues": result.issues, "warnings": result.warnings})
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0 if result.ok() else 1
    code = print_result(name, result)
    if "report" in payload:
        print(payload["report"])
    return code


def patch_check_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Validate an additive specs.yaml proposal.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("proposal", type=Path)
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])
    result = CheckResult()
    try:
        check_patch(load_spec(args.spec), load_yaml(args.proposal), result)
    except Exception as error:
        result.issue(str(error))
    return print_result("spec_patch_check", result, args.json_output)


def patch_apply_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Apply an additive specs.yaml proposal to a temp output or in-place.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("proposal", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--in-place", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])
    result = CheckResult()
    payload: dict[str, Any] = {}
    try:
        spec = load_spec(args.spec)
        proposal = load_yaml(args.proposal)
        check_patch(spec, proposal, result)
        if result.ok():
            merged = apply_patch_to_spec(spec, proposal)
            if args.in_place:
                atomic_write_yaml(args.spec, merged)
                payload["output"] = str(args.spec)
            elif args.output:
                atomic_write_yaml(args.output, merged)
                payload["output"] = str(args.output)
            else:
                result.issue("spec_patch_apply requires --output or --in-place")
    except Exception as error:
        result.issue(str(error))
    return print_payload_result("spec_patch_apply", result, payload, args.json_output)


def diff_guard_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Verify a specs.yaml diff is additive-only.")
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])
    result = CheckResult()
    try:
        check_diff_guard(load_spec(args.before), load_spec(args.after), result)
    except Exception as error:
        result.issue(str(error))
    return print_result("spec_diff_guard", result, args.json_output)


def transaction_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run a verified proposal transaction before merging into specs.yaml.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("proposal", type=Path)
    parser.add_argument("--generator", type=Path, default=Path("scripts/generate_project.py"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--keep-temp", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])
    try:
        spec = load_spec(args.spec)
        generator = resolve_spec_project_path(args.spec, spec, args.generator)
        result, report = run_transaction(
            args.spec,
            args.proposal,
            generator,
            apply=args.apply,
            keep_temp=args.keep_temp,
            report_path=args.report,
        )
    except Exception as error:
        result = CheckResult()
        result.issue(str(error))
        report = {}
    return print_payload_result("batch_transaction_runner", result, report, args.json_output)
