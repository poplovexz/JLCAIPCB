#!/usr/bin/env python3
"""Exercise routing contract, actual-copper evidence, and stale-route rejection."""

from __future__ import annotations

import copy
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _layout_stage import run_after_generation as accept_layout  # noqa: E402
from _pcb_skill_checks import CheckResult  # noqa: E402
from _routing_stage import check_contract, check_differential_pair_geometry, check_route_constraints, check_route_topology, check_stage_evidence, load_policy, route_snapshot, run_after_generation  # noqa: E402
from routing_candidate_transaction import unexpected_unconnected_nets  # noqa: E402
from test_layout_stage import GENERATOR, base_spec  # noqa: E402


def routed_spec(root: Path) -> dict:
    spec = base_spec(root)
    spec["board"]["copper_zones"][0]["layer"] = "F.Cu"
    spec["routing"] = {
        "schema_version": 1,
        "state": "ready",
        "revision": 1,
        "strategy": "generated",
        "batches": [{"id": "SIGNALS", "order": 1, "nets": ["SIG"], "method": "generated", "state": "routed", "rationale": "Route complete shared signal net."}],
        "net_constraints": {"SIG": {"net_class": "Default", "connection_mode": "tracks", "topology": "point-to-point", "allowed_layers": ["F.Cu"], "min_width_mm": 0.25, "max_vias": 0, "max_length_mm": 30, "rationale": "Short local signal."}},
    }
    return spec


def generate(root: Path, spec_path: Path, mode: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(GENERATOR), mode, str(spec_path)], cwd=root, text=True, capture_output=True)


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="routing-stage-") as temporary:
        root = Path(temporary)
        spec = routed_spec(root)
        spec_path = root / "spec.yaml"
        spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
        result = CheckResult()
        check_contract(spec, spec_path, result)
        if result.issues:
            failures.append(f"valid routing contract failed: {result.issues}")
        if generate(root, spec_path, "--layout-only").returncode:
            failures.append("layout-only fixture generation failed")
        else:
            result = CheckResult()
            accept_layout(spec, spec_path, GENERATOR, result)
            if result.issues:
                failures.append(f"layout evidence failed: {result.issues}")
        if generate(root, spec_path, "--board-only").returncode:
            failures.append("routed fixture generation failed")
        else:
            result = CheckResult()
            run_after_generation(spec, spec_path, result)
            if result.issues:
                failures.append(f"valid routing failed: {result.issues}")
            result = CheckResult()
            check_stage_evidence(spec, spec_path, result)
            if result.issues:
                failures.append(f"fresh routing evidence failed: {result.issues}")
            board = root / "project" / "layout_fixture.kicad_pcb"
            board.write_text(board.read_text(encoding="utf-8").replace("(width 0.25)", "(width 0.2)", 1), encoding="utf-8")
            result = CheckResult()
            check_stage_evidence(spec, spec_path, result)
            if not result.issues:
                failures.append("routing mutation did not invalidate evidence")

        partial = copy.deepcopy(spec)
        partial["routing"]["batches"][0]["nets"] = []
        result = CheckResult()
        check_contract(partial, spec_path, result)
        if not any("cover every" in issue for issue in result.issues):
            failures.append(f"partial routing batches were not rejected: {result.issues}")

        pair = {"id": "PAIR", "nets": ["P", "N"], "target_gap_mm": 0.2, "gap_tolerance_mm": 0.02, "max_skew_mm": 0.1, "min_coupled_ratio": 0.9, "rationale": "Fixture pair."}
        pair_snapshot = {"tracks": [
            {"type": "segment", "net": "P", "start": [0, 0], "end": [10, 0], "width_mm": 0.2, "layer": "F.Cu"},
            {"type": "segment", "net": "N", "start": [0, 0.4], "end": [10, 0.4], "width_mm": 0.2, "layer": "F.Cu"},
        ]}
        result = CheckResult()
        check_differential_pair_geometry(pair, pair_snapshot, load_policy(), result)
        if result.issues:
            failures.append(f"valid differential pair geometry failed: {result.issues}")
        pair_snapshot["tracks"][1]["start"][1] = 1.0
        pair_snapshot["tracks"][1]["end"][1] = 1.0
        result = CheckResult()
        check_differential_pair_geometry(pair, pair_snapshot, load_policy(), result)
        if not any("coupled ratio" in issue for issue in result.issues):
            failures.append("invalid differential pair gap was not rejected")

        via_spec = copy.deepcopy(spec)
        via_spec["routing"]["net_constraints"]["SIG"].update({"min_via_diameter_mm": 0.6, "min_via_drill_mm": 0.3})
        result = CheckResult()
        check_route_constraints(via_spec, {"metrics_by_net": {"SIG": {"segments": 1, "vias": 1, "length_mm": 5, "min_width_mm": 0.25, "min_via_diameter_mm": 0.4, "min_via_drill_mm": 0.2, "layers": ["F.Cu"]}}, "tracks": []}, result)
        if not any("min_via" in issue for issue in result.issues):
            failures.append("undersized routing via was not rejected")

        impedance_spec = copy.deepcopy(spec)
        impedance_spec["routing"]["net_constraints"]["SIG"].update({
            "allowed_layers": ["F.Cu"],
            "preferred_width_mm": 0.25,
            "max_neckdown_length_mm": 0,
        })
        impedance_spec["verification"] = {
            "signal_integrity": {
                "single_ended_nets": [{
                    "name": "SIG_Z0",
                    "net": "SIG",
                    "impedance_ohm": 50,
                    "impedance_tolerance_ohm": 5,
                    "geometry": {"layer": "F.Cu", "trace_width_mm": 0.25, "reference_layers": ["B.Cu"]},
                }]
            }
        }
        impedance_snapshot = {
            "metrics_by_net": {"SIG": {"segments": 1, "vias": 0, "length_mm": 5, "min_width_mm": 0.25, "layers": ["F.Cu"]}},
            "tracks": [{"type": "segment", "net": "SIG", "start": [0, 0], "end": [5, 0], "width_mm": 0.25, "layer": "F.Cu"}],
        }
        result = CheckResult()
        check_route_constraints(impedance_spec, impedance_snapshot, result)
        if result.issues:
            failures.append(f"valid actual impedance routing geometry failed: {result.issues}")
        invalid_impedance_snapshot = copy.deepcopy(impedance_snapshot)
        invalid_impedance_snapshot["tracks"][0].update({"width_mm": 0.5, "layer": "B.Cu"})
        invalid_impedance_snapshot["metrics_by_net"]["SIG"].update({"min_width_mm": 0.5, "layers": ["B.Cu"]})
        result = CheckResult()
        check_route_constraints(impedance_spec, invalid_impedance_snapshot, result)
        if not any("impedance geometry" in issue for issue in result.issues):
            failures.append("actual routing mismatch from impedance evidence geometry was not rejected")

        unconnected = [{"items": [{"description": "pad [CURRENT_BATCH]"}, {"description": "pad [CURRENT_BATCH]"}]}]
        if unexpected_unconnected_nets(unconnected, {"FUTURE_BATCH"}) != {"CURRENT_BATCH"}:
            failures.append("current-batch unconnected net was not separated from future planned nets")

        branched = {"tracks": [
            {"type": "segment", "net": "SIG", "start": [0, 0], "end": [1, 0]},
            {"type": "segment", "net": "SIG", "start": [0, 0], "end": [0, 1]},
            {"type": "segment", "net": "SIG", "start": [0, 0], "end": [-1, 0]},
        ]}
        result = CheckResult()
        check_route_topology("SIG", {"topology": "point-to-point"}, branched, load_policy(), result)
        if not any("branch point" in issue for issue in result.issues):
            failures.append("point-to-point topology accepted an actual branch")

        special_spec = copy.deepcopy(spec)
        special_spec["board"]["layers"]["copper"] = 4
        special_rule = special_spec["routing"]["net_constraints"]["SIG"]
        special_rule.update({
            "allowed_layers": ["F.Cu", "In1.Cu"],
            "max_vias": 1,
            "allowed_via_types": ["blind"],
            "via_type_by_layer_pair": [{"layers": ["F.Cu", "In1.Cu"], "via_type": "blind"}],
        })
        result = CheckResult()
        check_contract(special_spec, spec_path, result)
        if result.issues:
            failures.append(f"valid blind-via contract failed: {result.issues}")
        special_snapshot = {
            "metrics_by_net": {"SIG": {"segments": 1, "vias": 1, "length_mm": 5, "min_width_mm": 0.25, "min_via_diameter_mm": 0.6, "min_via_drill_mm": 0.3, "layers": ["F.Cu", "In1.Cu"]}},
            "tracks": [
                {"type": "segment", "net": "SIG", "start": [0, 0], "end": [5, 0], "width_mm": 0.25, "layer": "F.Cu"},
                {"type": "via", "net": "SIG", "at": [5, 0], "size_mm": 0.6, "drill_mm": 0.3, "layers": ["F.Cu", "In1.Cu"], "via_type": "blind", "backdrill": {"mode": "none"}},
            ],
        }
        result = CheckResult()
        check_route_constraints(special_spec, special_snapshot, result)
        if result.issues:
            failures.append(f"valid blind-via snapshot failed: {result.issues}")
        special_snapshot["tracks"][1]["via_type"] = "through"
        result = CheckResult()
        check_route_constraints(special_spec, special_snapshot, result)
        if not any("via_type" in issue or "full copper stack" in issue for issue in result.issues):
            failures.append("non-full-span through via was not rejected")

        backdrill_spec = copy.deepcopy(spec)
        backdrill_spec["board"]["layers"]["copper"] = 4
        backdrill_rule = backdrill_spec["routing"]["net_constraints"]["SIG"]
        backdrill_rule.update({
            "allowed_layers": ["F.Cu", "B.Cu"],
            "max_vias": 1,
            "allowed_via_types": ["through"],
            "backdrill": {"allowed": True, "required": True, "allowed_sides": ["top", "bottom"]},
        })
        backdrill_snapshot = {
            "metrics_by_net": {"SIG": {"segments": 1, "vias": 1, "length_mm": 5, "min_width_mm": 0.25, "min_via_diameter_mm": 0.6, "min_via_drill_mm": 0.3, "layers": ["F.Cu", "B.Cu"]}},
            "tracks": [
                {"type": "segment", "net": "SIG", "start": [0, 0], "end": [5, 0], "width_mm": 0.25, "layer": "F.Cu"},
                {"type": "via", "net": "SIG", "at": [5, 0], "size_mm": 0.6, "drill_mm": 0.3, "layers": ["F.Cu", "B.Cu"], "via_type": "through", "backdrill": {"mode": "top", "top": {"stop_layer": "In1.Cu", "drill_mm": 0.4}}},
            ],
        }
        result = CheckResult()
        check_route_constraints(backdrill_spec, backdrill_snapshot, result)
        if result.issues:
            failures.append(f"valid backdrill snapshot failed: {result.issues}")
        invalid_stop = copy.deepcopy(backdrill_snapshot)
        invalid_stop["tracks"][1]["backdrill"]["top"]["stop_layer"] = "F.Cu"
        result = CheckResult()
        check_route_constraints(backdrill_spec, invalid_stop, result)
        if not any("between the via endpoints" in issue for issue in result.issues):
            failures.append("backdrill outer stop layer was not rejected")
        invalid_drill = copy.deepcopy(backdrill_snapshot)
        invalid_drill["tracks"][1]["backdrill"]["top"]["drill_mm"] = 0.3
        result = CheckResult()
        check_route_constraints(backdrill_spec, invalid_drill, result)
        if not any("exceed the via drill" in issue for issue in result.issues):
            failures.append("non-oversized backdrill was not rejected")
        crossed_stops = copy.deepcopy(backdrill_snapshot)
        crossed_stops["tracks"][1]["backdrill"] = {
            "mode": "both",
            "top": {"stop_layer": "In2.Cu", "drill_mm": 0.4},
            "bottom": {"stop_layer": "In1.Cu", "drill_mm": 0.4},
        }
        result = CheckResult()
        check_route_constraints(backdrill_spec, crossed_stops, result)
        if not any("overlap or cross" in issue for issue in result.issues):
            failures.append("crossed top/bottom backdrill stops were not rejected")

        try:
            import pcbnew

            board = pcbnew.BOARD()
            board.SetCopperLayerCount(4)
            net = pcbnew.NETINFO_ITEM(board, "SIG")
            board.Add(net)
            via_cases = [
                (pcbnew.VIATYPE_BLIND, pcbnew.F_Cu, pcbnew.In1_Cu),
                (pcbnew.VIATYPE_BURIED, pcbnew.In1_Cu, pcbnew.In2_Cu),
                (pcbnew.VIATYPE_MICROVIA, pcbnew.F_Cu, pcbnew.In1_Cu),
            ]
            for index, (via_enum, top_layer, bottom_layer) in enumerate(via_cases):
                via = pcbnew.PCB_VIA(board)
                via.SetPosition(pcbnew.VECTOR2I_MM(index + 1, 1))
                via.SetWidth(pcbnew.FromMM(0.6))
                via.SetDrill(pcbnew.FromMM(0.3))
                via.SetViaType(via_enum)
                via.SetLayerPair(top_layer, bottom_layer)
                via.SetNet(net)
                board.Add(via)
            backdrilled = pcbnew.PCB_VIA(board)
            backdrilled.SetPosition(pcbnew.VECTOR2I_MM(len(via_cases) + 1, 1))
            backdrilled.SetWidth(pcbnew.FromMM(0.6))
            backdrilled.SetDrill(pcbnew.FromMM(0.3))
            backdrilled.SetViaType(pcbnew.VIATYPE_THROUGH)
            backdrilled.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            backdrilled.SetBackdrillMode(pcbnew.BACKDRILL_MODE_BACKDRILL_TOP)
            backdrilled.SetTopBackdrillLayer(pcbnew.In2_Cu)
            backdrilled.SetTopBackdrillSize(pcbnew.FromMM(0.4))
            backdrilled.SetNet(net)
            board.Add(backdrilled)
            special_board_path = root / "special-vias.kicad_pcb"
            board.Save(str(special_board_path))
            result = CheckResult()
            actual_snapshot = route_snapshot(special_board_path, result)
            if result.issues:
                failures.append(f"special-via route snapshot failed: {result.issues}")
            actual_vias = [item for item in actual_snapshot.get("tracks", []) if item.get("type") == "via"]
            if {item.get("via_type") for item in actual_vias} != {"through", "blind", "buried", "microvia"}:
                failures.append(f"route snapshot lost via types: {actual_vias}")
            through_record = next((item for item in actual_vias if item.get("via_type") == "through"), {})
            if through_record.get("backdrill") != {"mode": "top", "top": {"stop_layer": "In2.Cu", "drill_mm": 0.4}}:
                failures.append(f"route snapshot lost backdrill details: {through_record}")
        except Exception as error:
            failures.append(f"special-via pcbnew fixture failed: {error}")

    if failures:
        print("routing stage tests: FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("routing stage tests: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
