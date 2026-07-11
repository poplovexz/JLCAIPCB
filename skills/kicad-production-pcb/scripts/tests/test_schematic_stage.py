#!/usr/bin/env python3
"""Exercise schematic-stage contract and strict generated-net semantics."""

from __future__ import annotations

import copy
import sys
import tempfile
from pathlib import Path

import yaml

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
from _pcb_skill_checks import CheckResult, compare_generated_net_graph, sha256_file  # noqa: E402
from _schematic_stage import check_contract, load_policy  # noqa: E402


def symbol_library() -> str:
    return """(kicad_symbol_lib (version 20231120) (generator "fixture")
  (symbol "TwoPin" (in_bom yes) (on_board yes)
    (symbol "TwoPin_1_1"
      (pin passive line (at -2.54 0 0) (length 2.54) (name "A") (number "1"))
      (pin passive line (at 2.54 0 180) (length 2.54) (name "B") (number "2"))
    )
  )
  (symbol "MultiUnit" (in_bom yes) (on_board yes)
    (symbol "MultiUnit_1_1"
      (pin passive line (at -2.54 0 0) (length 2.54) (name "A") (number "1"))
    )
    (symbol "MultiUnit_2_1"
      (pin passive line (at 2.54 0 180) (length 2.54) (name "B") (number "2"))
    )
  )
)\n"""


def base_spec(root: Path) -> dict:
    components = [
        {"ref": "J1", "symbol": "Fixture:TwoPin", "pads": {"1": "INPUT", "2": "RETURN"}},
        {"ref": "F1", "symbol": "Fixture:TwoPin", "pads": {"1": "INPUT", "2": "PROTECTED"}},
        {"ref": "U1", "symbol": "Fixture:TwoPin", "pads": {"1": "PROTECTED", "2": "RETURN"}},
        {"ref": "D1", "symbol": "Fixture:TwoPin", "pads": {"1": "PROTECTED", "2": "RETURN"}},
    ]
    return {
        "project": {
            "name": "schematic_stage_fixture",
            "stage": "production",
            "root_dir": ".",
            "output_dir": "project",
            "artifacts_dir": "artifacts",
        },
        "kicad": {"symbol_libraries": {"Fixture": str(root / "fixture.kicad_sym")}},
        "schematic": {
            "connectivity": {"mode": "net_labels", "label_scope": "global", "default_label_shape": "passive"},
            "no_connects": [],
            "path_assertions": [
                {
                    "id": "INPUT_SERIES_PROTECTION",
                    "kind": "series",
                    "covers": ["ENTRY_PROTECTION"],
                    "boundaries": ["POWER_ENTRY_BOUNDARY"],
                    "source_pin": "J1.1",
                    "sink_pin": "U1.1",
                    "through": [{"ref": "F1", "input_pin": "1", "output_pin": "2"}],
                },
                {
                    "id": "LINE_SHUNT_PROTECTION",
                    "kind": "shunt",
                    "covers": ["LINE_PROTECTION"],
                    "boundaries": ["PROTECTED_LINE_BOUNDARY"],
                    "ref": "D1",
                    "line_pin": "1",
                    "return_pin": "2",
                    "line_net": "PROTECTED",
                    "return_net": "RETURN",
                },
            ],
        },
        "architecture": {
            "protection_intents": [
                {"id": "ENTRY_PROTECTION", "boundary_id": "POWER_ENTRY_BOUNDARY", "disposition": "required"},
                {"id": "LINE_PROTECTION", "boundary_id": "PROTECTED_LINE_BOUNDARY", "disposition": "required"},
            ]
        },
        "nets": [
            {"name": "INPUT", "class": "power"},
            {"name": "PROTECTED", "class": "power"},
            {"name": "RETURN", "class": "power"},
        ],
        "components": components,
        "expected_net_graph": {
            "nets": {
                "INPUT": {
                    "required_pins": ["J1.1", "F1.1"],
                    "exact": True,
                    "role": "supply",
                    "source_pins": ["J1.1"],
                    "sink_pins": ["F1.1"],
                },
                "PROTECTED": {
                    "required_pins": ["F1.2", "U1.1", "D1.1"],
                    "exact": True,
                    "role": "supply",
                    "source_pins": ["F1.2"],
                    "sink_pins": ["U1.1", "D1.1"],
                },
                "RETURN": {
                    "required_pins": ["J1.2", "U1.2", "D1.2"],
                    "exact": True,
                    "role": "return",
                    "return_pins": ["J1.2", "U1.2", "D1.2"],
                },
            }
        },
    }


def contract_issues(spec: dict, spec_path: Path) -> list[str]:
    result = CheckResult()
    check_contract(spec, spec_path, load_policy(), result)
    return result.issues


def expect_issue(failures: list[str], label: str, issues: list[str], text: str) -> None:
    if not any(text in issue for issue in issues):
        failures.append(f"{label} did not report {text!r}: {issues}")


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="kicad-schematic-stage-") as temporary:
        root = Path(temporary)
        (root / "fixture.kicad_sym").write_text(symbol_library(), encoding="utf-8")
        spec_path = root / "spec.yaml"
        candidate = base_spec(root)
        spec_path.write_text(yaml.safe_dump(candidate, sort_keys=False), encoding="utf-8")

        issues = contract_issues(candidate, spec_path)
        if issues:
            failures.append(f"valid schematic contract failed: {issues}")

        missing_pin = copy.deepcopy(candidate)
        del missing_pin["components"][2]["pads"]["2"]
        expect_issue(failures, "missing pin disposition", contract_issues(missing_pin, spec_path), "lack connected/no-connect")

        multi_unit = copy.deepcopy(candidate)
        multi_unit["components"][2]["symbol"] = "Fixture:MultiUnit"
        expect_issue(failures, "multi-unit symbol", contract_issues(multi_unit, spec_path), "uses 2 symbol units")

        missing_source = copy.deepcopy(candidate)
        del missing_source["expected_net_graph"]["nets"]["INPUT"]["source_pins"]
        expect_issue(failures, "missing supply source", contract_issues(missing_source, spec_path), "declare source pin")

        bypassed_series = copy.deepcopy(candidate)
        bypassed_series["components"][1]["pads"]["2"] = "INPUT"
        expect_issue(failures, "series bypass", contract_issues(bypassed_series, spec_path), "does not create a series boundary")

        uncovered = copy.deepcopy(candidate)
        uncovered["schematic"]["path_assertions"][0]["covers"] = []
        expect_issue(failures, "protection coverage", contract_issues(uncovered, spec_path), "not covered")

        wrong_boundary = copy.deepcopy(candidate)
        wrong_boundary["schematic"]["path_assertions"][0]["boundaries"] = ["OTHER_BOUNDARY"]
        expect_issue(failures, "protection boundary", contract_issues(wrong_boundary, spec_path), "does not match")

        unbound_flag = copy.deepcopy(candidate)
        unbound_flag["schematic"]["power_flags"] = [
            {
                "ref": "#FLG01",
                "symbol": "Fixture:TwoPin",
                "net": "INPUT",
                "position_mm": {"x": 10, "y": 10},
            }
        ]
        expect_issue(failures, "power flag provenance", contract_issues(unbound_flag, spec_path), "physical source pin")

        override = copy.deepcopy(candidate)
        override["kicad"]["symbol_pin_types"] = {"Fixture:TwoPin": {"1": "power_out"}}
        expect_issue(failures, "pin override evidence", contract_issues(override, spec_path), "lacks production evidence")

        evidence_path = root / "artifacts" / "evidence" / "pin-types.txt"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text("verified pin type source\n", encoding="utf-8")
        evidenced_override = copy.deepcopy(override)
        evidenced_override["kicad"]["symbol_pin_type_override_evidence"] = {
            "Fixture:TwoPin": {
                "source": "artifacts/evidence/pin-types.txt",
                "sha256": sha256_file(evidence_path),
                "rationale": "The local evidence defines the electrical pin type.",
            }
        }
        if contract_issues(evidenced_override, spec_path):
            failures.append("valid local pin-type override evidence was rejected")
        evidenced_override["kicad"]["symbol_pin_type_override_evidence"]["Fixture:TwoPin"]["sha256"] = "0" * 64
        expect_issue(failures, "stale override evidence", contract_issues(evidenced_override, spec_path), "sha256 is stale")

        expected_graph = {
            "INPUT": ["F1.1", "J1.1"],
            "PROTECTED": ["D1.1", "F1.2", "U1.1"],
            "RETURN": ["D1.2", "J1.2", "U1.2"],
        }
        strict_result = CheckResult()
        compare_generated_net_graph(
            candidate,
            {f"/{name}": pins for name, pins in expected_graph.items()},
            strict_result,
            strict_names=True,
        )
        expect_issue(failures, "strict generated names", strict_result.issues, "net name is missing")

        exact_result = CheckResult()
        exact_with_virtual = copy.deepcopy(expected_graph)
        exact_with_virtual["INPUT"].append("#FLG01.1")
        compare_generated_net_graph(candidate, exact_with_virtual, exact_result, strict_names=True)
        if exact_result.issues:
            failures.append(f"strict generated graph rejected allowed virtual source pin: {exact_result.issues}")

    if failures:
        print("test_schematic_stage: FAIL")
        for failure in failures:
            print(f"ISSUE: {failure}")
        return 1
    print("test_schematic_stage: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
