#!/usr/bin/env python3
"""Exercise layout-only generation, actual-board checks, and evidence stability."""

from __future__ import annotations

import copy
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]
GENERATOR = REPO_ROOT / "scripts" / "generate_project.py"
sys.path.insert(0, str(SCRIPTS_DIR))
from _layout_stage import check_contract, check_evidence, run_after_generation  # noqa: E402
from _pcb_skill_checks import CheckResult  # noqa: E402


def base_spec(root: Path) -> dict:
    constraints = {
        "outline": {"status": "defined", "rationale": "Closed production outline.", "items": ["BOARD_OUTLINE"]},
        "mechanical": {"status": "not_applicable", "rationale": "Fixture has no mounting hardware."},
        "placement": {"status": "defined", "rationale": "One placement batch.", "items": ["CORE"]},
        "external_connectors": {"status": "defined", "rationale": "One fixed edge-facing part.", "items": ["EXT_R1"]},
        "keepouts": {"status": "defined", "rationale": "Reserved right-side area.", "items": ["KO_RIGHT"]},
        "critical_proximity": {"status": "defined", "rationale": "Critical pair remains close.", "items": ["PAIR"]},
        "high_current": {"status": "defined", "rationale": "Power intent is declared.", "items": ["POWER_PATH"]},
        "high_speed": {"status": "not_applicable", "rationale": "No high-speed interface."},
        "rf_antenna": {"status": "not_applicable", "rationale": "No RF function."},
        "power_copper": {"status": "defined", "rationale": "Ground zone is declared.", "items": ["GROUND_COPPER"]},
        "return_paths": {"status": "defined", "rationale": "Signal references ground.", "items": ["SIGNAL_RETURN"]},
        "thermal": {"status": "not_applicable", "rationale": "No meaningful heat source."},
        "assembly": {"status": "not_applicable", "rationale": "No extra assembly constraint."},
    }
    return {
        "project": {
            "name": "layout_fixture", "title": "Layout Fixture", "stage": "local-mvp", "root_dir": ".",
            "output_dir": "project", "artifacts_dir": "artifacts", "kicad_major_required": 10,
            "generated_by": "fixture", "schematic_version": 20230121, "pcb_page_size": "A4",
        },
        "kicad": {
            "symbol_root": "/usr/share/kicad/symbols", "footprint_root": "/usr/share/kicad/footprints",
            "symbol_libraries": {"Device": "/usr/share/kicad/symbols/Device.kicad_sym"},
            "footprint_libraries": {"Resistor_SMD": "/usr/share/kicad/footprints/Resistor_SMD.pretty"},
        },
        "board": {
            "size_mm": {"width": 30.0, "height": 20.0}, "origin_mm": {"x": 0.0, "y": 0.0},
            "outline": {"loops": [[{"x": 0, "y": 0}, {"x": 30, "y": 0}, {"x": 30, "y": 20}, {"x": 0, "y": 20}]]},
            "layers": {"copper": 2},
            "fabrication": {
                "min_track_width_mm": 0.2, "default_signal_width_mm": 0.25, "default_power_width_mm": 0.5,
                "min_clearance_mm": 0.2, "min_via_diameter_mm": 0.6, "min_via_drill_mm": 0.3,
                "edge_cut_width_mm": 0.1, "silk_line_width_mm": 0.15, "silk_text_size_mm": 1.0,
                "silk_text_thickness_mm": 0.15,
            },
            "net_classes": [{"name": "Default", "clearance_mm": 0.2, "track_width_mm": 0.25, "via_diameter_mm": 0.6, "via_drill_mm": 0.3}],
            "copper_zones": [{
                "id": "Z_GND", "net": "GND", "layer": "B.Cu", "purpose": "return plane",
                "polygon": [{"x": 1, "y": 1}, {"x": 29, "y": 1}, {"x": 29, "y": 19}, {"x": 1, "y": 19}],
            }],
            "mechanical_features": [],
        },
        "layout": {
            "schema_version": 1, "state": "ready", "revision": 1,
            "coordinate_system": {"units": "mm", "origin": "board.origin_mm", "x_axis": "right", "y_axis": "down"},
            "constraints": constraints,
            "placement_batches": [{"id": "CORE", "order": 1, "refs": ["R1", "R2"], "rationale": "Place the complete functional pair."}],
            "fixed_placements": [{"ref": "R1", "side": "top", "rotation_deg": 0, "tolerance_mm": 0.001, "rationale": "Edge anchor."}],
            "external_connectors": [{
                "id": "EXT_R1", "ref": "R1", "board_edge": "left", "footprint_outward_axis_deg": 0,
                "outward_direction_deg": 0, "orientation_tolerance_deg": 0.1, "max_edge_distance_mm": 10,
                "allow_body_overhang": False,
            }],
            "keepouts": [{
                "id": "KO_RIGHT", "polygon": [{"x": 25, "y": 6}, {"x": 29, "y": 6}, {"x": 29, "y": 14}, {"x": 25, "y": 14}],
                "layers": ["F.Cu", "B.Cu"], "restrictions": ["tracks", "vias", "copper", "footprints"],
                "allowed_refs": [], "rationale": "Reserve a mechanical area.",
            }],
            "proximity_constraints": [{"id": "PAIR", "refs": ["R1", "R2"], "max_distance_mm": 15, "rationale": "Bound loop length."}],
            "separation_constraints": [], "regions": [], "overlap_waivers": [],
            "high_current_paths": [{
                "id": "POWER_PATH", "nets": ["PWR"], "source_refs": ["R1"], "sink_refs": ["R2"],
                "current_a": 1.0, "min_corridor_width_mm": 2.0, "copper_layers": ["F.Cu"], "zone_ids": ["Z_GND"],
                "corridor_polygon": [{"x": 6, "y": 8}, {"x": 23, "y": 8}, {"x": 23, "y": 12}, {"x": 6, "y": 12}],
                "rationale": "Power path corridor.",
            }],
            "high_speed_paths": [], "antenna_constraints": [],
            "power_copper": [{"id": "GROUND_COPPER", "nets": ["GND"], "zone_ids": ["Z_GND"], "rationale": "Ground copper."}],
            "return_paths": [{"id": "SIGNAL_RETURN", "signal_nets": ["SIG"], "reference_net": "GND", "zone_ids": ["Z_GND"], "rationale": "Continuous return intent."}],
            "thermal_paths": [], "assembly_constraints": [],
        },
        "nets": [{"name": "PWR", "class": "power"}, {"name": "SIG", "class": "signal"}, {"name": "GND", "class": "power"}],
        "components": [
            {"ref": "R1", "value": "R", "symbol": "Device:R", "footprint": "Resistor_SMD:R_0603_1608Metric", "datasheet": "fixture", "status": "locked", "position_mm": {"x": 8, "y": 10, "rotation": 0, "side": "top"}, "pads": {"1": "PWR", "2": "SIG"}},
            {"ref": "R2", "value": "R", "symbol": "Device:R", "footprint": "Resistor_SMD:R_0603_1608Metric", "datasheet": "fixture", "status": "locked", "position_mm": {"x": 20, "y": 10, "rotation": 0, "side": "top"}, "pads": {"1": "SIG", "2": "GND"}},
        ],
        "routes": [{"net": "SIG", "layer": "F.Cu", "width_mm": 0.25, "from": {"ref": "R1", "pad": "2"}, "to": {"ref": "R2", "pad": "1"}}],
        "todo": [],
    }


def run_generator(root: Path, spec_path: Path, mode: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(GENERATOR), mode, str(spec_path)], cwd=root, text=True, capture_output=True)


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="layout-stage-") as temporary:
        root = Path(temporary)
        spec = base_spec(root)
        spec_path = root / "spec.yaml"
        spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
        result = CheckResult()
        check_contract(spec, spec_path, result)
        if result.issues:
            failures.append(f"valid layout contract failed: {result.issues}")
        generated = run_generator(root, spec_path, "--layout-only")
        if generated.returncode:
            failures.append(f"layout-only generation failed: {generated.stdout} {generated.stderr}")
        else:
            result = CheckResult()
            run_after_generation(spec, spec_path, GENERATOR, result)
            if result.issues:
                failures.append(f"valid generated layout failed: {result.issues}")
            routed = run_generator(root, spec_path, "--board-only")
            if routed.returncode:
                failures.append(f"routed generation failed: {routed.stdout} {routed.stderr}")
            result = CheckResult()
            check_evidence(spec, spec_path, result)
            if result.issues:
                failures.append(f"routing invalidated unchanged layout: {result.issues}")

        overlap = copy.deepcopy(spec)
        overlap["components"][1]["position_mm"]["x"] = 8
        overlap_path = root / "overlap.yaml"
        overlap_path.write_text(yaml.safe_dump(overlap, sort_keys=False), encoding="utf-8")
        generated = run_generator(root, overlap_path, "--layout-only")
        if generated.returncode == 0:
            result = CheckResult()
            run_after_generation(overlap, overlap_path, GENERATOR, result)
            if not any("courtyard overlap" in issue for issue in result.issues):
                failures.append(f"courtyard overlap was not rejected: {result.issues}")

        routed_first = run_generator(root, spec_path, "--board-only")
        if routed_first.returncode == 0:
            result = CheckResult()
            run_after_generation(spec, spec_path, GENERATOR, result)
            if not any("contains tracks" in issue for issue in result.issues):
                failures.append(f"routing before layout acceptance was not rejected: {result.issues}")

        bottom = copy.deepcopy(spec)
        bottom["components"][1]["position_mm"].update({"rotation": 90, "side": "bottom"})
        bottom_path = root / "bottom.yaml"
        bottom_path.write_text(yaml.safe_dump(bottom, sort_keys=False), encoding="utf-8")
        generated = run_generator(root, bottom_path, "--layout-only")
        if generated.returncode:
            failures.append(f"bottom-side layout generation failed: {generated.stdout} {generated.stderr}")
        else:
            result = CheckResult()
            run_after_generation(bottom, bottom_path, GENERATOR, result)
            if result.issues:
                failures.append(f"bottom-side placement was not preserved: {result.issues}")

        wrong_zone = copy.deepcopy(spec)
        wrong_zone["layout"]["keepouts"][0]["polygon"][0]["x"] = 24
        wrong_zone_path = root / "wrong-zone.yaml"
        wrong_zone_path.write_text(yaml.safe_dump(wrong_zone, sort_keys=False), encoding="utf-8")
        result = CheckResult()
        run_after_generation(wrong_zone, wrong_zone_path, GENERATOR, result)
        if not any("keepout KO_RIGHT geometry" in issue for issue in result.issues):
            failures.append(f"stale keepout geometry was not rejected: {result.issues}")

    if failures:
        print("layout stage tests: FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("layout stage tests: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
