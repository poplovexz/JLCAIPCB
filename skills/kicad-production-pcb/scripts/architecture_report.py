#!/usr/bin/env python3
"""Render a human-readable Mermaid block diagram from architecture YAML."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pcb_skill_checks import CheckResult, load_spec  # noqa: E402
from architecture_gate import (  # noqa: E402
    architecture_confirmation_digest,
    architecture_digest,
    architecture_report_path,
    check_architecture,
    load_policy,
)


def list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def mapping_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def mermaid_id(value: Any) -> str:
    identifier = re.sub(r"[^A-Za-z0-9_]", "_", str(value))
    if not identifier or identifier[0].isdigit():
        identifier = f"block_{identifier}"
    return identifier


def cell(value: Any) -> str:
    if isinstance(value, list):
        value = ", ".join(str(item) for item in value)
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def quoted_label(value: Any) -> str:
    return str(value).replace('"', "'").replace("\n", " ").strip()


def render_report(spec: dict[str, Any], spec_path: Path, policy: dict[str, Any]) -> str:
    architecture = mapping_value(spec.get("architecture"))
    project = mapping_value(spec.get("project"))
    project_name = str(project.get("name") or spec_path.stem)
    blocks = [item for item in list_value(architecture.get("blocks")) if isinstance(item, dict)]
    edges = [item for item in list_value(architecture.get("block_edges")) if isinstance(item, dict)]

    lines = [
        f"# {project_name} Block Architecture",
        "",
        f"- Architecture revision: {architecture.get('revision')}",
        f"- Source intake revision: {architecture.get('source_intake_revision')}",
        f"- State: {architecture.get('state')}",
        f"- Current target: {architecture.get('current_target')}",
        f"- Confirmation SHA256: {architecture_confirmation_digest(architecture, policy)}",
        f"- Architecture SHA256: {architecture_digest(architecture)}",
        f"- Summary: {architecture.get('summary')}",
        "",
        "## Block Diagram",
        "",
        "```mermaid",
        "flowchart LR",
    ]
    for block in blocks:
        identifier = mermaid_id(block.get("id"))
        label = quoted_label(f"{block.get('id')}\n{block.get('role')}")
        lines.append(f'  {identifier}["{label}"]')
    for edge in edges:
        source = mermaid_id(edge.get("from"))
        target = mermaid_id(edge.get("to"))
        label = quoted_label(edge.get("kind"))
        lines.append(f"  {source} -->|{label}| {target}")
    lines.extend(["```", ""])

    sections = [
        ("Power Domains", "power_domains", ["id", "source_block", "consumer_blocks", "voltage_class", "current_class"]),
        ("External Connectors", "external_connectors", ["id", "block_id", "exposure", "hot_plug", "protection_intent"]),
        ("Risk Paths", "risk_paths", ["id", "kind", "block_ids", "interface_ids", "constraints"]),
        ("Open Decisions", "open_decisions", ["id", "kind", "owner", "status", "blocks", "next_action"]),
    ]
    for title, key, fields in sections:
        items = [item for item in list_value(architecture.get(key)) if isinstance(item, dict)]
        lines.extend([f"## {title}", ""])
        if not items:
            lines.extend(["None.", ""])
            continue
        lines.append("| " + " | ".join(fields) + " |")
        lines.append("| " + " | ".join("---" for _ in fields) + " |")
        for item in items:
            lines.append("| " + " | ".join(cell(item.get(field, "")) for field in fields) + " |")
        lines.append("")

    constraint_field = str(policy.get("selection_constraints_field", "selection_constraints"))
    constraints: list[dict[str, Any]] = []
    for block in blocks:
        for constraint in list_value(block.get(constraint_field)):
            if isinstance(constraint, dict):
                constraints.append({"block_id": block.get("id"), **constraint})
    lines.extend(["## Sourcing Constraints", ""])
    constraint_fields = ["block_id", "id", "kind", "statement", "criteria", "required_before"]
    if not constraints:
        lines.extend(["None.", ""])
    else:
        lines.append("| " + " | ".join(constraint_fields) + " |")
        lines.append("| " + " | ".join("---" for _ in constraint_fields) + " |")
        for constraint in constraints:
            lines.append("| " + " | ".join(cell(constraint.get(field, "")) for field in constraint_fields) + " |")
        lines.append("")
    lines.append(f"Source spec: `{spec_path}`")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Render a block-level architecture report from specs.yaml.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--if-required", action="store_true")
    args = parser.parse_args(argv[1:])

    try:
        spec = load_spec(args.spec)
        result = CheckResult()
        details = check_architecture(spec, result, force=not args.if_required, spec_path=args.spec)
        if args.if_required and not details.get("enabled"):
            print("architecture report not required for this legacy/simple spec")
            return 0
        if not result.ok():
            for issue in result.issues:
                print(f"ISSUE: {issue}", file=sys.stderr)
            return 1
        policy = load_policy(spec)
        output_path = architecture_report_path(spec, policy, args.spec)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(render_report(spec, args.spec, policy), encoding="utf-8")
        print(output_path)
        return 0
    except Exception as error:
        print(f"ISSUE: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
