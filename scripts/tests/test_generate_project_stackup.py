#!/usr/bin/env python3
"""Verify that a configured physical stackup survives KiCad generation and reload."""

from __future__ import annotations

import copy
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pcbnew
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "configs" / "physical_stackup_regression.yaml"


def skill_scripts_dir() -> Path:
    candidates = [
        REPO_ROOT / container / "kicad-production-pcb" / "scripts"
        for container in ("codex-skills", "skills")
    ]
    for candidate in candidates:
        if (candidate / "_kicad_sexpr.py").is_file():
            return candidate
    raise FileNotFoundError(f"Cannot locate kicad-production-pcb scripts under {REPO_ROOT}")


sys.path.insert(0, str(skill_scripts_dir()))

from _kicad_sexpr import direct_child, node_head, parse, walk  # noqa: E402


def load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return data


def repo_path(value: Any) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else REPO_ROOT / path


def run_regression(config_path: Path) -> None:
    config = load_yaml(config_path)
    regression = config.get("regression")
    if not isinstance(regression, dict):
        raise ValueError("regression configuration must be a mapping")
    base_spec = load_yaml(repo_path(regression["base_spec"]))
    stackup = regression.get("stackup")
    if not isinstance(stackup, dict) or not isinstance(stackup.get("layers"), list):
        raise ValueError("regression.stackup.layers must be configured")

    with tempfile.TemporaryDirectory(prefix="physical-stackup-") as temporary:
        temporary_root = Path(temporary)
        output_dir = temporary_root / "project"
        spec = copy.deepcopy(base_spec)
        spec["project"]["name"] = str(regression["project_name"])
        spec["project"]["output_dir"] = str(output_dir)
        spec["project"]["artifacts_dir"] = str(temporary_root / "artifacts")
        spec["board"]["layers"]["copper"] = regression["copper_layers"]
        spec["board"]["stackup"] = copy.deepcopy(stackup)
        spec_path = temporary_root / "spec.yaml"
        spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")

        command = [
            sys.executable,
            str(repo_path(regression["generator"])),
            *[str(item) for item in regression.get("generator_args", [])],
            str(spec_path),
        ]
        completed = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True)
        if completed.returncode:
            raise AssertionError(completed.stdout + completed.stderr)

        board_path = output_dir / f"{regression['project_name']}.kicad_pcb"
        board = pcbnew.LoadBoard(str(board_path))
        if board is None:
            raise AssertionError("KiCad could not reload the generated physical stackup board")
        if int(board.GetCopperLayerCount()) != int(regression["copper_layers"]):
            raise AssertionError("generated copper layer count does not match regression configuration")
        settings = board.GetDesignSettings()
        expected_thickness = pcbnew.FromMM(float(stackup["board_thickness_mm"]))
        if int(settings.GetBoardThickness()) != int(expected_thickness):
            raise AssertionError("generated board thickness does not match physical stackup")
        if settings.m_HasStackup is not True:
            raise AssertionError("KiCad did not recognize the generated physical stackup")

        board_text = board_path.read_text(encoding="utf-8")
        for entry in stackup["layers"]:
            if f'(layer "{entry["name"]}"' not in board_text:
                raise AssertionError(f"generated stackup is missing {entry['name']}")
        roundtrip_path = temporary_root / "roundtrip.kicad_pcb"
        board.Save(str(roundtrip_path))
        roundtrip_text = roundtrip_path.read_text(encoding="utf-8")
        edge_connector = stackup.get("edge_connector")
        if edge_connector is not None and f"(edge_connector {edge_connector})" not in roundtrip_text:
            raise AssertionError("KiCad round-trip did not preserve edge_connector")
        roundtrip_root = parse(roundtrip_text)
        stackup_node = next((node for node in walk(roundtrip_root) if node_head(node) == "stackup"), None)
        if stackup_node is None:
            raise AssertionError("KiCad round-trip lost the stackup block")
        roundtrip_layers = {
            str(node[1]): node
            for node in stackup_node[1:]
            if isinstance(node, list) and node_head(node) == "layer" and len(node) > 1
        }
        roundtrip_dielectrics = [
            node
            for node in stackup_node[1:]
            if isinstance(node, list)
            and node_head(node) == "layer"
            and (type_node := direct_child(node, "type")) is not None
            and len(type_node) > 1
            and str(type_node[1]) in {"core", "prepreg"}
        ]
        dielectric_index = 0
        for entry in stackup["layers"]:
            if entry.get("type") == "dielectric":
                layer_node = (
                    roundtrip_dielectrics[dielectric_index]
                    if dielectric_index < len(roundtrip_dielectrics)
                    else None
                )
                dielectric_index += 1
            else:
                layer_node = roundtrip_layers.get(str(entry["name"]))
            if layer_node is None:
                raise AssertionError(f"KiCad round-trip lost stackup layer {entry['name']}")
            for field, kicad_field in [
                ("thickness_mm", "thickness"),
                ("epsilon_r", "epsilon_r"),
                ("loss_tangent", "loss_tangent"),
            ]:
                value_node = direct_child(layer_node, kicad_field)
                if field in entry and (
                    value_node is None
                    or len(value_node) < 2
                    or float(value_node[1]) != float(entry[field])
                ):
                    raise AssertionError(f"KiCad round-trip lost {entry['name']} {field}")
            material_node = direct_child(layer_node, "material")
            if "material" in entry and (
                material_node is None
                or len(material_node) < 2
                or str(material_node[1]) != str(entry["material"])
            ):
                raise AssertionError(f"KiCad round-trip lost {entry['name']} material")


def main(argv: list[str]) -> int:
    config = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_CONFIG
    try:
        run_regression(config)
    except Exception as error:
        print("physical stackup generation regression: FAIL")
        print(f"- {error}")
        return 1
    print("physical stackup generation regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
