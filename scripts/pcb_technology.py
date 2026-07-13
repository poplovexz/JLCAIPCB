#!/usr/bin/env python3
"""Shared, policy-driven PCB layer and physical-stackup helpers."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml


def skill_root() -> Path:
    configured_root = os.environ.get("KICAD_PRODUCTION_PCB_SKILL_ROOT")
    if configured_root:
        return Path(configured_root)
    configured_scripts = os.environ.get("KICAD_PRODUCTION_PCB_SKILL_SCRIPTS")
    if configured_scripts:
        return Path(configured_scripts).parent
    repository_root = Path(__file__).resolve().parents[1]
    candidates = [
        repository_root / container / "kicad-production-pcb"
        for container in ("codex-skills", "skills")
    ]
    required_assets = (
        Path("assets/fabrication-capability-policy.yaml"),
        Path("assets/routing-stage-policy.yaml"),
    )
    for candidate in candidates:
        if all((candidate / asset).is_file() for asset in required_assets):
            return candidate
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(
        "Cannot locate kicad-production-pcb; configure "
        f"KICAD_PRODUCTION_PCB_SKILL_ROOT or KICAD_PRODUCTION_PCB_SKILL_SCRIPTS (searched: {searched})"
    )


def fabrication_policy_path() -> Path:
    return skill_root() / "assets" / "fabrication-capability-policy.yaml"


def routing_stage_policy_path() -> Path:
    return skill_root() / "assets" / "routing-stage-policy.yaml"


def load_fabrication_policy() -> dict[str, Any]:
    path = fabrication_policy_path()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Fabrication capability policy must be a mapping: {path}")
    return data


def load_routing_stage_policy() -> dict[str, Any]:
    path = routing_stage_policy_path()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Routing stage policy must be a mapping: {path}")
    return data


def validate_copper_layer_count(value: Any, policy: dict[str, Any] | None = None) -> int:
    rules = (policy or load_fabrication_policy()).get("copper_layers", {})
    if not isinstance(rules, dict):
        raise ValueError("fabrication capability policy copper_layers must be a mapping")
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("board.layers.copper must be an integer")
    minimum = rules.get("min_count")
    maximum = rules.get("max_count")
    if not isinstance(minimum, int) or isinstance(minimum, bool):
        raise ValueError("fabrication capability policy copper_layers.min_count must be an integer")
    if not isinstance(maximum, int) or isinstance(maximum, bool):
        raise ValueError("fabrication capability policy copper_layers.max_count must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"board.layers.copper must be from {minimum} through {maximum}")
    if bool(rules.get("require_even")) and value % 2:
        raise ValueError("board.layers.copper must be even")
    return value


def copper_layer_names(copper_count: int, policy: dict[str, Any] | None = None) -> list[str]:
    """Return the KiCad copper-layer order for an already validated count."""
    rules = (policy or load_fabrication_policy()).get("copper_layers", {})
    if not isinstance(rules, dict):
        raise ValueError("fabrication capability policy copper_layers must be a mapping")
    front = rules.get("front_name")
    back = rules.get("back_name")
    template = rules.get("inner_name_template")
    if not all(isinstance(value, str) and value.strip() for value in [front, back, template]):
        raise ValueError("fabrication capability policy copper layer naming is incomplete")
    try:
        inner = [str(template).format(index=index) for index in range(1, copper_count - 1)]
    except (KeyError, ValueError) as error:
        raise ValueError(f"fabrication capability policy inner layer template is invalid: {error}") from error
    return [str(front), *inner, str(back)]


def _number(value: Any, field: str) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    return f"{float(value):.12g}"


def _quoted(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("KiCad stackup string values must be non-empty strings")
    return json.dumps(value, ensure_ascii=False)


def _token(value: Any, field: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_.-]+", value.strip()) is None:
        raise ValueError(f"board.stackup.{field} must be a non-empty KiCad token")
    return value.strip()


def _stackup_layer_lines(entry: dict[str, Any], indent: str) -> list[str]:
    name = _quoted(entry.get("name"))
    entry_type = str(entry.get("type", "")).strip()
    if entry_type == "copper":
        kicad_type = "copper"
    elif entry_type == "dielectric":
        kicad_type = str(entry.get("dielectric_type", "")).strip()
    elif entry_type == "solder_mask":
        rules = load_fabrication_policy().get("physical_stackup", {})
        layer_types = rules.get("solder_mask_layer_kicad_types") if isinstance(rules, dict) else None
        kicad_type = (
            str(layer_types.get(entry.get("name"), "")).strip()
            if isinstance(layer_types, dict)
            else ""
        )
    else:
        kicad_type = str(entry.get("kicad_type", "")).strip()
    if not kicad_type:
        raise ValueError(f"Physical stackup layer {entry.get('name')} has no KiCad type")

    lines = [f"{indent}(layer {name}", f"{indent}\t(type {_quoted(kicad_type)})"]
    optional_string_fields = {
        "color": "color",
        "material": "material",
    }
    optional_numeric_fields = {
        "thickness_mm": "thickness",
        "epsilon_r": "epsilon_r",
        "loss_tangent": "loss_tangent",
    }
    for spec_field, kicad_field in optional_string_fields.items():
        if entry.get(spec_field) is not None:
            lines.append(f"{indent}\t({kicad_field} {_quoted(entry[spec_field])})")
    for spec_field, kicad_field in optional_numeric_fields.items():
        if entry.get(spec_field) is not None:
            lines.append(f"{indent}\t({kicad_field} {_number(entry[spec_field], spec_field)})")
    lines.append(f"{indent})")
    return lines


def render_stackup_block(stackup: dict[str, Any], indent: str) -> str:
    entries = stackup.get("layers")
    if not isinstance(entries, list) or not entries:
        raise ValueError("board.stackup.layers must be a non-empty list")
    lines = [f"{indent}(stackup"]
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"board.stackup.layers[{index}] must be a mapping")
        lines.extend(_stackup_layer_lines(entry, indent + "\t"))

    scalar_options = {"copper_finish": "copper_finish"}
    boolean_options = {
        "dielectric_constraints": "dielectric_constraints",
        "castellated_pads": "castellated_pads",
        "edge_plating": "edge_plating",
    }
    for spec_field, kicad_field in scalar_options.items():
        if stackup.get(spec_field) is not None:
            lines.append(f"{indent}\t({kicad_field} {_quoted(stackup[spec_field])})")
    if stackup.get("edge_connector") is not None:
        lines.append(
            f"{indent}\t(edge_connector {_token(stackup['edge_connector'], 'edge_connector')})"
        )
    for spec_field, kicad_field in boolean_options.items():
        if stackup.get(spec_field) is not None:
            value = stackup[spec_field]
            if not isinstance(value, bool):
                raise ValueError(f"board.stackup.{spec_field} must be boolean")
            lines.append(f"{indent}\t({kicad_field} {'yes' if value else 'no'})")
    lines.append(f"{indent})")
    return "\n".join(lines) + "\n"


def inject_physical_stackup(path: Path, stackup: dict[str, Any]) -> None:
    """Insert the spec stackup into the setup block emitted by pcbnew."""
    if not isinstance(stackup.get("layers"), list) or not stackup["layers"]:
        return
    text = path.read_text(encoding="utf-8")
    setup = re.search(r"(?m)^(?P<indent>[ \t]*)\(setup[ \t]*$", text)
    if setup is None:
        raise ValueError(f"Generated KiCad PCB has no setup block: {path}")
    tail = text[setup.end() :]
    if re.match(r"\r?\n[ \t]*\(stackup(?:\s|\()", tail):
        raise ValueError(f"Generated KiCad PCB already contains a stackup block: {path}")
    newline = "\r\n" if "\r\n" in text else "\n"
    indent = setup.group("indent") + "\t"
    block = render_stackup_block(stackup, indent).replace("\n", newline)
    insertion = setup.end() + len(newline)
    path.write_text(text[:insertion] + block + text[insertion:], encoding="utf-8")
