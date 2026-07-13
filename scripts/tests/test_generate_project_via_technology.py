#!/usr/bin/env python3
"""Verify policy-driven route-lock via technology replay with real pcbnew objects."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pcbnew
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "configs" / "via_technology_regression.yaml"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from generate_project import configure_locked_via  # noqa: E402
from pcb_technology import load_routing_stage_policy, validate_copper_layer_count  # noqa: E402


def load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return data


def configure_case(
    board,
    case: dict[str, Any],
    policy: dict[str, Any],
    via_drill_mm: float,
):
    via = pcbnew.PCB_VIA(board)
    via.SetDrill(pcbnew.FromMM(via_drill_mm))
    configure_locked_via(board, pcbnew, via, case, policy)
    return via


def run_regression(config_path: Path) -> None:
    regression = load_yaml(config_path).get("regression")
    if not isinstance(regression, dict):
        raise ValueError("regression configuration must be a mapping")
    policy = load_routing_stage_policy()
    copper_layers = validate_copper_layer_count(regression.get("copper_layers"))
    via_drill_mm = float(regression["via_drill_mm"])
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(copper_layers)
    via_definitions = policy.get("via_types", {}).get("definitions", {})
    backdrill_enums = policy.get("backdrill", {}).get("mode_enums", {})

    for case in regression.get("cases", []):
        if not isinstance(case, dict):
            raise ValueError("via regression case must be a mapping")
        via = configure_case(board, case, policy, via_drill_mm)
        via_type = str(case["via_type"])
        expected_enum = getattr(pcbnew, str(via_definitions[via_type]["pcbnew_enum"]))
        actual_layers = [str(board.GetLayerName(via.TopLayer())), str(board.GetLayerName(via.BottomLayer()))]
        if int(via.GetViaType()) != int(expected_enum) or actual_layers != case["layers"]:
            raise AssertionError(f"{case['id']} via technology was not preserved")
        mode = str(case["backdrill"]["mode"])
        expected_mode = getattr(pcbnew, str(backdrill_enums[mode]))
        if int(via.GetBackdrillMode()) != int(expected_mode):
            raise AssertionError(f"{case['id']} backdrill mode was not preserved")
        for side, detail in case["backdrill"].items():
            if side == "mode":
                continue
            suffix = side[:1].upper() + side[1:]
            layer = getattr(via, f"Get{suffix}BackdrillLayer")()
            size = getattr(via, f"Get{suffix}BackdrillSize")()
            if str(board.GetLayerName(layer)) != detail["stop_layer"]:
                raise AssertionError(f"{case['id']} {side} backdrill layer was not preserved")
            if int(size) != int(pcbnew.FromMM(float(detail["drill_mm"]))):
                raise AssertionError(f"{case['id']} {side} backdrill size was not preserved")

    for case in regression.get("invalid_cases", []):
        if not isinstance(case, dict):
            raise ValueError("invalid via regression case must be a mapping")
        try:
            configure_case(board, case, policy, via_drill_mm)
        except SystemExit:
            continue
        raise AssertionError(f"{case['id']} unexpectedly preserved invalid via technology")


def main(argv: list[str]) -> int:
    config = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_CONFIG
    try:
        run_regression(config)
    except Exception as error:
        print("route-lock via technology regression: FAIL")
        print(f"- {error}")
        return 1
    print("route-lock via technology regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
