#!/usr/bin/env python3
"""Exercise SES via-technology mapping and unavailable-layer rejection."""

from __future__ import annotations

import sys
from pathlib import Path

import pcbnew


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))
from import_routing_batch_ses import add_path, add_via, load_routing_policy  # noqa: E402


def fixture_board():
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(4)
    net = pcbnew.NETINFO_ITEM(board, "SIG")
    board.Add(net)
    return board, net


def via_definition(layers: list[str]) -> dict:
    return {"diameter_mm": 0.6, "drill_mm": 0.3, "layers": layers}


def add_fixture_via(board, net, policy: dict, layers: list[str], constraint: dict | None = None):
    identifier = "Fixture_600:300_um"
    add_via(
        board,
        net,
        ["via", identifier, "1000", "1000"],
        {identifier: via_definition(layers)},
        0.001,
        policy,
        constraint,
    )
    return next(item for item in board.GetTracks() if isinstance(item, pcbnew.PCB_VIA))


def main() -> int:
    failures: list[str] = []
    policy = load_routing_policy()
    cases = [
        ("blind", ["F.Cu", "In1.Cu"], pcbnew.VIATYPE_BLIND),
        ("buried", ["In1.Cu", "In2.Cu"], pcbnew.VIATYPE_BURIED),
        ("microvia", ["F.Cu", "In1.Cu"], pcbnew.VIATYPE_MICROVIA),
    ]
    for via_type, layers, expected_enum in cases:
        board, net = fixture_board()
        constraint = {
            "allowed_via_types": [via_type],
            "via_type_by_layer_pair": [{"layers": layers, "via_type": via_type}],
        }
        try:
            via = add_fixture_via(board, net, policy, layers, constraint)
            actual_layers = [board.GetLayerName(via.TopLayer()), board.GetLayerName(via.BottomLayer())]
            if int(via.GetViaType()) != int(expected_enum) or actual_layers != layers:
                failures.append(f"SES import did not preserve {via_type} technology: enum={int(via.GetViaType())}, layers={actual_layers}")
        except Exception as error:
            failures.append(f"valid {via_type} SES via failed: {error}")

    board, net = fixture_board()
    try:
        via = add_fixture_via(board, net, policy, ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
        if int(via.GetViaType()) != int(pcbnew.VIATYPE_THROUGH):
            failures.append("legacy full-span SES via was not imported as the policy default")
    except Exception as error:
        failures.append(f"legacy full-span SES via failed: {error}")

    board, net = fixture_board()
    try:
        add_fixture_via(board, net, policy, ["F.Cu", "In1.Cu"])
        failures.append("non-full-span SES via without technology mapping was accepted")
    except ValueError as error:
        if "technology mapping" not in str(error):
            failures.append(f"non-full-span mapping rejection was unclear: {error}")

    board, net = fixture_board()
    required_backdrill = {"backdrill": {"allowed": True, "required": True}}
    try:
        add_fixture_via(board, net, policy, ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"], required_backdrill)
        failures.append("SES import accepted a required backdrill it cannot represent")
    except ValueError as error:
        if "required backdrill" not in str(error):
            failures.append(f"required backdrill rejection was unclear: {error}")

    board, net = fixture_board()
    disabled_layer_id = board.GetLayerID("In14.Cu")
    if disabled_layer_id == pcbnew.UNDEFINED_LAYER or board.IsLayerEnabled(disabled_layer_id):
        failures.append("disabled-layer fixture did not resolve to a known disabled KiCad layer")
    else:
        try:
            add_path(board, net, ["path", "In14.Cu", "250", "0", "0", "1000", "0"], 0.001)
            failures.append("SES path accepted a known but disabled copper layer")
        except ValueError as error:
            if "unavailable layer" not in str(error):
                failures.append(f"disabled path-layer rejection was unclear: {error}")

    if failures:
        print("SES import tests: FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("SES import tests: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
