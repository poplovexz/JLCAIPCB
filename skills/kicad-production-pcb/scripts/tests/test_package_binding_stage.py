#!/usr/bin/env python3
"""Exercise package binding semantics, geometry, provenance, and orientation."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
from pathlib import Path

import yaml

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
from _package_binding_stage import validate_contract  # noqa: E402
from _pcb_skill_checks import CheckResult, sha256_file  # noqa: E402


SYMBOL = """(kicad_symbol_lib (version 20231120) (generator "fixture")
  (symbol "Device" (in_bom yes) (on_board yes)
    (symbol "Device_1_1"
      (pin power_in line (at -2.54 0 0) (length 2.54) (name "SUPPLY") (number "1"))
      (pin power_in line (at 2.54 0 180) (length 2.54) (name "RETURN") (number "2"))
    )
  )
)\n"""

FOOTPRINT = """(footprint "Device_2Pad"
  (version 20240108)
  (generator "fixture")
  (layer "F.Cu")
  (fp_rect (start -1.6 -1.1) (end 1.6 1.1) (stroke (width 0.05) (type default)) (fill none) (layer "F.CrtYd"))
  (fp_rect (start -1.5 -1) (end 1.5 1) (stroke (width 0.1) (type default)) (fill none) (layer "F.Fab"))
  (fp_circle (center -1.2 -0.7) (end -1.1 -0.7) (stroke (width 0.1) (type default)) (fill solid) (layer "F.SilkS"))
  (pad "1" smd rect (at -1 0) (size 1 1.5) (layers "F.Cu" "F.Paste" "F.Mask"))
  (pad "2" smd rect (at 1 0 180) (size 1 1.5) (layers "F.Cu" "F.Paste" "F.Mask"))
  (model "${KICAD9_3DMODEL_DIR}/Fixture.3dshapes/Device.step")
)\n"""


def write_json(path: Path, data: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return sha256_file(path)


def build_fixture(root: Path) -> tuple[dict, Path, Path, Path, Path]:
    symbol_path = root / "lib" / "fixture.kicad_sym"
    footprint_path = root / "lib" / "Fixture.pretty" / "Device_2Pad.kicad_mod"
    symbol_path.parent.mkdir(parents=True, exist_ok=True)
    footprint_path.parent.mkdir(parents=True, exist_ok=True)
    symbol_path.write_text(SYMBOL, encoding="utf-8")
    footprint_path.write_text(FOOTPRINT, encoding="utf-8")
    artifacts = root / "artifacts" / "binding"
    artifacts.mkdir(parents=True, exist_ok=True)
    pinmap_path = artifacts / "pinmap.json"
    geometry_path = artifacts / "geometry.json"
    datasheet_path = artifacts / "manufacturer-datasheet.json"
    datasheet_path.write_text('{"fixture":"manufacturer datasheet snapshot"}\n', encoding="utf-8")
    datasheet_sha = sha256_file(datasheet_path)
    assembly_sha = "b" * 64
    pinmap = {
        "schema_version": 2,
        "status": "verified",
        "ref": "U1",
        "symbol_id": "Fixture:Device",
        "footprint": "Fixture:Device_2Pad",
        "source_evidence_ids": ["DATASHEET"],
        "source_evidence_sha256": [datasheet_sha],
        "symbol_file_sha256": sha256_file(symbol_path),
        "footprint_file_sha256": sha256_file(footprint_path),
        "mappings": [
            {
                "datasheet_pin": "1", "datasheet_pin_name": "VCC", "function": "supply input",
                "symbol_pin": "1", "symbol_pin_name": "SUPPLY", "symbol_electrical_type": "power_in",
                "footprint_pad": "1", "disposition": "connected", "net": "SUPPLY", "source_locator": "pin table p.2",
            },
            {
                "datasheet_pin": "2", "datasheet_pin_name": "GND", "function": "return",
                "symbol_pin": "2", "symbol_pin_name": "RETURN", "symbol_electrical_type": "power_in",
                "footprint_pad": "2", "disposition": "connected", "net": "RETURN", "source_locator": "pin table p.2",
            },
        ],
    }
    geometry = {
        "schema_version": 1,
        "status": "verified",
        "source_evidence_ids": ["DATASHEET"],
        "source_evidence_sha256": [datasheet_sha],
        "source_locator": "package drawing p.8",
        "body_dimensions_mm": {"length": 3.0, "width": 2.0, "height": 1.0},
        "pads": [
            {"number": "1", "type": "smd", "shape": "rect", "at_mm": [-1.0, 0.0], "size_mm": [1.0, 1.5], "layers": ["F.Cu", "F.Paste", "F.Mask"]},
            {"number": "2", "type": "smd", "shape": "rect", "at_mm": [1.0, 0.0, 180.0], "size_mm": [1.0, 1.5], "layers": ["F.Cu", "F.Paste", "F.Mask"]},
        ],
        "features": {
            "graphics_by_layer": {"F.CrtYd": 1, "F.Fab": 1, "F.SilkS": 1},
            "models": ["${KICAD9_3DMODEL_DIR}/Fixture.3dshapes/Device.step"],
            "paste_pad_count": 2,
            "custom_pad_count": 0,
            "through_hole_pad_count": 0,
        },
        "fabrication_contract": {
            "courtyard": "verified",
            "fab_outline": "verified",
            "silkscreen_pin1_marker": {"status": "verified", "rationale": "Pin 1 dot matches the top-view drawing."},
            "paste_strategy": {"status": "verified", "rationale": "Each SMT pad has a paste aperture."},
            "exposed_pad_strategy": {"status": "not_applicable", "rationale": "The package has no exposed pad."},
            "model_disposition": "present",
            "placement_anchor": {
                "cpl_anchor_mm": {"x": 0.0, "y": 0.0},
                "body_center_offset_mm": {"x": 0.0, "y": 0.0},
            },
        },
    }
    pinmap_sha = write_json(pinmap_path, pinmap)
    geometry_sha = write_json(geometry_path, geometry)
    pin_table_path = artifacts / "datasheet-pin-table.json"
    pin_table = {
        "schema_version": 1,
        "status": "verified",
        "manufacturer": "Fixture Semiconductor",
        "mpn": "FIX-2",
        "package": "PKG-2",
        "source": {
            "evidence_id": "DATASHEET", "evidence_sha256": datasheet_sha,
            "file": str(datasheet_path), "file_sha256": datasheet_sha,
        },
        "extractor": {"name": "fixture-extractor", "version": "1", "method": "structured-table"},
        "records": [
            {"pin": "1", "name": "VCC", "function": "supply input", "source_locator": "pin table p.2"},
            {"pin": "2", "name": "GND", "function": "return", "source_locator": "pin table p.2"},
        ],
    }
    pin_table_sha = write_json(pin_table_path, pin_table)
    manifest_path = artifacts / "manifest.yaml"
    part_lock_path = artifacts / "part-lock.yaml"
    part_lock = {
        "schema_version": 1,
        "selections": [{
            "component_refs": ["U1"], "disposition": "pcba",
            "manufacturer": "Fixture Semiconductor", "mpn": "FIX-2",
            "package": {"name": "PKG-2", "body_length_mm": 3.0, "body_width_mm": 2.0},
            "evidence_records": [
                {"id": "DATASHEET", "kind": "manufacturer-datasheet", "sha256": datasheet_sha},
                {"id": "ASSEMBLY", "kind": "assembly-snapshot", "sha256": assembly_sha},
            ],
        }],
    }
    part_lock_path.write_text(yaml.safe_dump(part_lock, sort_keys=False), encoding="utf-8")
    part_lock_sha = sha256_file(part_lock_path)
    manifest = {
        "schema_version": 2,
        "part_lock_sha256": part_lock_sha,
        "bindings": [{
            "ref": "U1",
            "symbol_id": "Fixture:Device",
            "footprint": "Fixture:Device_2Pad",
            "symbol_file_evidence": {"file": str(symbol_path), "sha256": sha256_file(symbol_path)},
            "footprint_file_evidence": {"file": str(footprint_path), "sha256": sha256_file(footprint_path)},
            "library_origin": "trusted",
            "pinmap_evidence": {"file": str(pinmap_path), "sha256": pinmap_sha},
            "datasheet_pin_table_evidence": {"file": str(pin_table_path), "sha256": pin_table_sha},
            "footprint_geometry_evidence": {"file": str(geometry_path), "sha256": geometry_sha},
            "orientation_contract": {
                "datasheet_view": "top", "pin1_reference": "dot at pin 1", "footprint_zero_axis": "+X",
                "placement_origin": "body_center", "cpl_rotation_offset_deg": 0,
                "bottom_side_transform": "mirror_then_rotate", "polarity_kind": "pin1",
                "source_evidence_id": "DATASHEET",
                "source_evidence_sha256": datasheet_sha,
                "source_locator": "package drawing p.8",
                "assembly_source_evidence_id": "ASSEMBLY",
                "assembly_source_evidence_sha256": assembly_sha,
                "assembly_source_locator": "assembly orientation snapshot",
            },
            "status": "verified",
        }],
    }
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    spec = {
        "project": {"name": "fixture", "root_dir": ".", "artifacts_dir": "artifacts"},
        "sourcing": {
            "part_lock": {"status": "locked", "path": str(part_lock_path), "sha256": part_lock_sha},
            "downstream_binding": {"status": "ready", "manifest": {"path": str(manifest_path), "sha256": sha256_file(manifest_path)}},
        },
        "schematic": {"no_connects": []},
        "components": [{"ref": "U1", "symbol": "Fixture:Device", "footprint": "Fixture:Device_2Pad", "pads": {"1": "SUPPLY", "2": "RETURN"}}],
    }
    spec_path = root / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    return spec, spec_path, manifest_path, pinmap_path, geometry_path


def issues(spec: dict, spec_path: Path) -> list[str]:
    result = CheckResult()
    validate_contract(spec, spec_path, result)
    return result.issues


def expect(failures: list[str], label: str, found: list[str], fragment: str) -> None:
    if not any(fragment in item for item in found):
        failures.append(f"{label} did not report {fragment!r}: {found}")


def refresh_manifest(spec: dict, manifest_path: Path) -> None:
    spec["sourcing"]["downstream_binding"]["manifest"]["sha256"] = sha256_file(manifest_path)


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="package-binding-stage-") as temporary:
        root = Path(temporary)
        spec, spec_path, manifest_path, pinmap_path, geometry_path = build_fixture(root)
        if found := issues(spec, spec_path):
            failures.append(f"valid package binding failed: {found}")

        pinmap = json.loads(pinmap_path.read_text(encoding="utf-8"))
        pinmap["mappings"][0]["symbol_pin_name"] = "WRONG"
        write_json(pinmap_path, pinmap)
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        manifest["bindings"][0]["pinmap_evidence"]["sha256"] = sha256_file(pinmap_path)
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        refresh_manifest(spec, manifest_path)
        expect(failures, "semantic pin mismatch", issues(spec, spec_path), "symbol_pin_name")

        spec, spec_path, manifest_path, pinmap_path, geometry_path = build_fixture(root)
        geometry = json.loads(geometry_path.read_text(encoding="utf-8"))
        geometry["pads"][0]["size_mm"] = [9.0, 9.0]
        write_json(geometry_path, geometry)
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        manifest["bindings"][0]["footprint_geometry_evidence"]["sha256"] = sha256_file(geometry_path)
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        refresh_manifest(spec, manifest_path)
        expect(failures, "geometry mismatch", issues(spec, spec_path), "pads do not exactly match")

        spec, spec_path, manifest_path, _, _ = build_fixture(root)
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        pin_table_path = Path(manifest["bindings"][0]["datasheet_pin_table_evidence"]["file"])
        pin_table = json.loads(pin_table_path.read_text(encoding="utf-8"))
        pin_table["records"][0]["name"] = "WRONG"
        write_json(pin_table_path, pin_table)
        manifest["bindings"][0]["datasheet_pin_table_evidence"]["sha256"] = sha256_file(pin_table_path)
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        refresh_manifest(spec, manifest_path)
        expect(failures, "independent datasheet table", issues(spec, spec_path), "does not match the semantic pin map")

        spec, spec_path, manifest_path, _, geometry_path = build_fixture(root)
        geometry = json.loads(geometry_path.read_text(encoding="utf-8"))
        geometry["fabrication_contract"]["courtyard"] = "not_applicable"
        write_json(geometry_path, geometry)
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        manifest["bindings"][0]["footprint_geometry_evidence"]["sha256"] = sha256_file(geometry_path)
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        refresh_manifest(spec, manifest_path)
        expect(failures, "courtyard required", issues(spec, spec_path), "verified Courtyard")

        spec, spec_path, manifest_path, _, _ = build_fixture(root)
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        orientation = manifest["bindings"][0]["orientation_contract"]
        orientation["assembly_source_evidence_id"] = orientation["source_evidence_id"]
        orientation["assembly_source_evidence_sha256"] = orientation["source_evidence_sha256"]
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        refresh_manifest(spec, manifest_path)
        expect(failures, "independent PCBA orientation", issues(spec, spec_path), "must be independent")

        spec, spec_path, manifest_path, _, geometry_path = build_fixture(root)
        geometry = json.loads(geometry_path.read_text(encoding="utf-8"))
        geometry["source_evidence_ids"] = ["UNBOUND_SOURCE"]
        write_json(geometry_path, geometry)
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        manifest["bindings"][0]["footprint_geometry_evidence"]["sha256"] = sha256_file(geometry_path)
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        refresh_manifest(spec, manifest_path)
        expect(failures, "unbound geometry source", issues(spec, spec_path), "source evidence IDs do not match")

        spec, spec_path, manifest_path, _, _ = build_fixture(root)
        part_lock_path = Path(spec["sourcing"]["part_lock"]["path"])
        part_lock = yaml.safe_load(part_lock_path.read_text(encoding="utf-8"))
        part_lock["selections"][0]["package"]["body_length_mm"] = 4.0
        part_lock_path.write_text(yaml.safe_dump(part_lock, sort_keys=False), encoding="utf-8")
        part_lock_sha = sha256_file(part_lock_path)
        spec["sourcing"]["part_lock"]["sha256"] = part_lock_sha
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        manifest["part_lock_sha256"] = part_lock_sha
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        refresh_manifest(spec, manifest_path)
        expect(failures, "locked body dimensions", issues(spec, spec_path), "does not match locked package")

        spec, spec_path, manifest_path, _, _ = build_fixture(root)
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        del manifest["bindings"][0]["orientation_contract"]["pin1_reference"]
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        refresh_manifest(spec, manifest_path)
        expect(failures, "orientation omission", issues(spec, spec_path), "pin1_reference is required")

        spec, spec_path, manifest_path, _, _ = build_fixture(root)
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        manifest["bindings"][0]["library_origin"] = "imported"
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        refresh_manifest(spec, manifest_path)
        expect(failures, "missing import provenance", issues(spec, spec_path), "import_provenance requires")

    if failures:
        print("package binding stage tests: FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("package binding stage tests: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
