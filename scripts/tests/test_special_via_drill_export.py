#!/usr/bin/env python3
"""Verify KiCad emits distinct configured blind/buried/backdrill fabrication files."""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pcbnew
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "configs" / "via_technology_regression.yaml"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from pcb_technology import load_routing_stage_policy, validate_copper_layer_count  # noqa: E402


def load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return data


def enum_value(definition: dict[str, Any], field: str):
    name = definition.get(field)
    value = getattr(pcbnew, str(name), None) if isinstance(name, str) else None
    if value is None:
        raise ValueError(f"pcbnew enum is unavailable: {name}")
    return value


def run_regression(config_path: Path) -> None:
    regression = load_yaml(config_path).get("regression")
    if not isinstance(regression, dict):
        raise ValueError("regression configuration must be a mapping")
    policy = load_routing_stage_policy()
    via_definitions = policy["via_types"]["definitions"]
    backdrill_enums = policy["backdrill"]["mode_enums"]
    copper_layers = validate_copper_layer_count(regression.get("copper_layers"))

    board = pcbnew.BOARD()
    board.SetCopperLayerCount(copper_layers)
    net = pcbnew.NETINFO_ITEM(board, "SPECIAL_VIA_TEST")
    board.Add(net)
    for index, case in enumerate(regression.get("cases", [])):
        if not isinstance(case, dict):
            raise ValueError("via regression case must be a mapping")
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I_MM(index + 1, 1))
        via.SetWidth(pcbnew.FromMM(float(regression["via_diameter_mm"])))
        via.SetDrill(pcbnew.FromMM(float(regression["via_drill_mm"])))
        via.SetViaType(enum_value(via_definitions[str(case["via_type"])], "pcbnew_enum"))
        via.SetLayerPair(board.GetLayerID(str(case["layers"][0])), board.GetLayerID(str(case["layers"][1])))
        mode = str(case["backdrill"]["mode"])
        via.SetBackdrillMode(enum_value(backdrill_enums, mode))
        for side, detail in case["backdrill"].items():
            if side == "mode":
                continue
            suffix = side[:1].upper() + side[1:]
            getattr(via, f"Set{suffix}BackdrillLayer")(board.GetLayerID(str(detail["stop_layer"])))
            getattr(via, f"Set{suffix}BackdrillSize")(pcbnew.FromMM(float(detail["drill_mm"])))
        via.SetNet(net)
        board.Add(via)

    export_config = regression.get("drill_export")
    if not isinstance(export_config, dict):
        raise ValueError("regression.drill_export must be a mapping")
    with tempfile.TemporaryDirectory(prefix="special-via-drill-") as temporary:
        root = Path(temporary)
        board_path = root / f"{export_config['board_name']}.kicad_pcb"
        output_dir = root / "drill"
        output_dir.mkdir()
        board.Save(str(board_path))
        completed = subprocess.run(
            ["kicad-cli", "pcb", "export", "drill", "--output", f"{output_dir}/", str(board_path)],
            text=True,
            capture_output=True,
        )
        if completed.returncode:
            raise AssertionError(completed.stdout + completed.stderr)
        files = list(output_dir.iterdir())
        for expected in export_config.get("expected_artifacts", []):
            filename_pattern = re.compile(str(expected["filename_regex"]))
            content_pattern = re.compile(str(expected["content_regex"]))
            matches = [path for path in files if filename_pattern.search(path.name)]
            if len(matches) != 1:
                raise AssertionError(f"{expected['id']} expected one drill artifact, got {[path.name for path in matches]}")
            if content_pattern.search(matches[0].read_text(encoding="utf-8", errors="replace")) is None:
                raise AssertionError(f"{expected['id']} drill artifact has the wrong FileFunction")


def main(argv: list[str]) -> int:
    config = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_CONFIG
    try:
        run_regression(config)
    except Exception as error:
        print("special via drill export regression: FAIL")
        print(f"- {error}")
        return 1
    print("special via drill export regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
