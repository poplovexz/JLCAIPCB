#!/usr/bin/env python3
"""Exercise the local Spec Freeze transaction and stale-input rejection."""

from __future__ import annotations

import copy
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
from _pcb_skill_checks import CheckResult, load_spec  # noqa: E402
from _spec_freeze import check_freeze_contract, check_frozen_spec, load_policy as load_freeze_policy  # noqa: E402
from architecture_gate import architecture_confirmation_digest, load_policy as load_architecture_policy  # noqa: E402


def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True)


def base_spec(root: Path) -> dict:
    architecture = {
        "revision": 1,
        "source_intake_revision": 1,
        "current_target": "local-mvp",
        "state": "ready",
        "summary": "A protected entry feeds a generic function block and one external interface.",
        "practical_choices_confirmed": True,
        "practical_choice_confirmation": {
            "status": "confirmed",
            "confirmed_by": "user",
            "architecture_revision": 1,
            "source_intake_revision": 1,
            "architecture_sha256": "pending",
            "user_response_summary": "Confirmed the generic power, function, and connector boundary choices.",
        },
        "technical_decision_owner": "codex",
        "outputs": {"report_path": "artifacts/architecture/spec_freeze_integration.md"},
        "blocks": [
            {
                "id": "POWER_ENTRY",
                "category": "power_entry",
                "role": "Receive protected low-voltage power.",
                "scope": "onboard",
                "power_domains": ["BOARD_POWER"],
                "required": True,
                "selection_constraints": [
                    {
                        "id": "POWER_ENTRY_CAPABILITY",
                        "kind": "power",
                        "statement": "Provide the declared board rail and entry protection.",
                        "required": True,
                        "required_before": "component-sourcing",
                    }
                ],
            },
            {
                "id": "FUNCTION_CORE",
                "category": "controller",
                "role": "Implement the confirmed generic behavior.",
                "scope": "onboard",
                "power_domains": ["BOARD_POWER"],
                "required": True,
                "selection_constraints": [
                    {
                        "id": "FUNCTION_CAPABILITY",
                        "kind": "function",
                        "statement": "Implement the confirmed behavior and recovery state.",
                        "required": True,
                        "required_before": "component-sourcing",
                    }
                ],
            },
            {
                "id": "EXTERNAL_INTERFACE",
                "category": "interface",
                "role": "Expose one protected low-speed interface.",
                "scope": "onboard",
                "power_domains": ["BOARD_POWER"],
                "required": True,
                "selection_constraints": [
                    {
                        "id": "INTERFACE_CAPABILITY",
                        "kind": "interface",
                        "statement": "Expose the declared boundary interface.",
                        "required": True,
                        "required_before": "component-sourcing",
                    }
                ],
            },
            {
                "id": "EXTERNAL_DEVICE",
                "category": "external_device",
                "role": "Represent the off-board device.",
                "scope": "external",
                "power_domains": [],
                "required": True,
            },
        ],
        "block_edges": [
            {"id": "POWER_TO_CORE", "from": "POWER_ENTRY", "to": "FUNCTION_CORE", "kind": "power", "direction": "source_to_sink"},
            {"id": "POWER_TO_INTERFACE", "from": "POWER_ENTRY", "to": "EXTERNAL_INTERFACE", "kind": "power", "direction": "source_to_sink"},
            {"id": "CORE_TO_INTERFACE", "from": "FUNCTION_CORE", "to": "EXTERNAL_INTERFACE", "kind": "data", "direction": "bidirectional"},
            {"id": "INTERFACE_TO_DEVICE", "from": "EXTERNAL_INTERFACE", "to": "EXTERNAL_DEVICE", "kind": "data", "direction": "bidirectional"},
        ],
        "power_domains": [
            {
                "id": "BOARD_POWER",
                "source_block": "POWER_ENTRY",
                "consumer_blocks": ["FUNCTION_CORE", "EXTERNAL_INTERFACE"],
                "voltage_class": "regulated_low_voltage",
                "current_class": "low",
                "sharing": "dedicated",
                "backfeed_policy": "blocked",
                "protection_intent": "required",
                "required_before": "local-mvp",
            }
        ],
        "interfaces": [
            {
                "id": "EXTERNAL_LINK",
                "from": "EXTERNAL_INTERFACE",
                "to": "EXTERNAL_DEVICE",
                "kind": "data",
                "direction": "bidirectional",
                "external": True,
                "speed_class": "low",
                "voltage_domain": "BOARD_POWER",
                "risk_tags": ["external_cable", "user_accessible"],
                "required_before": "local-mvp",
            }
        ],
        "external_connectors": [
            {
                "id": "BOUNDARY_CONNECTOR",
                "block_id": "EXTERNAL_INTERFACE",
                "interface_ids": ["EXTERNAL_LINK"],
                "exposure": "user_accessible",
                "hot_plug": "no",
                "protection_intent": "required",
                "rationale": "The interface crosses the board boundary.",
                "required_before": "local-mvp",
            }
        ],
        "risk_paths": [
            {
                "id": "EXTERNAL_CABLE_RISK",
                "kind": "external_cable",
                "block_ids": ["EXTERNAL_INTERFACE", "EXTERNAL_DEVICE"],
                "interface_ids": ["EXTERNAL_LINK"],
                "power_domain_ids": [],
                "connector_ids": ["BOUNDARY_CONNECTOR"],
                "reason": "The signal leaves the board.",
                "constraints": ["Place boundary protection near the connector."],
                "required_before": "local-mvp",
            }
        ],
        "protection_intents": [
            {
                "id": "POWER_PROTECTION",
                "boundary_id": "BOARD_POWER",
                "threats": ["overcurrent", "reverse_polarity"],
                "disposition": "required",
                "strategy_classes": ["current_limit", "polarity_control"],
                "rationale": "Protect the power entry.",
                "required_before": "local-mvp",
            },
            {
                "id": "INTERFACE_PROTECTION",
                "boundary_id": "BOUNDARY_CONNECTOR",
                "threats": ["electrostatic_discharge"],
                "disposition": "required",
                "strategy_classes": ["clamp"],
                "rationale": "Protect the exposed interface.",
                "required_before": "local-mvp",
            },
        ],
        "failure_states": [
            {
                "id": "DEVICE_DISCONNECTED",
                "trigger": "The external device is disconnected.",
                "affected_blocks": ["EXTERNAL_INTERFACE", "FUNCTION_CORE"],
                "safe_state": "Keep the output inactive.",
                "required_before": "local-mvp",
            }
        ],
        "test_and_debug": [
            {
                "id": "FUNCTION_TEST",
                "kind": "programming",
                "target_blocks": ["FUNCTION_CORE"],
                "disposition": "required",
                "rationale": "The function needs an observable recovery path.",
                "required_before": "local-mvp",
            }
        ],
        "requirement_coverage": [
            {"requirement_path": "intake.user_intent", "block_ids": ["FUNCTION_CORE", "EXTERNAL_INTERFACE"], "rationale": "These blocks implement the behavior."},
            {"requirement_path": "intake.success_criteria", "block_ids": ["FUNCTION_CORE", "EXTERNAL_DEVICE"], "rationale": "These blocks produce the observable outcome."},
            {"requirement_path": "intake.system_boundary", "block_ids": ["EXTERNAL_INTERFACE", "EXTERNAL_DEVICE"], "rationale": "These blocks define the boundary."},
            {"requirement_path": "budget_intent", "block_ids": ["POWER_ENTRY", "FUNCTION_CORE", "EXTERNAL_INTERFACE"], "rationale": "All on-board blocks share the prototype budget."},
        ],
        "hazard_coverage": [],
        "open_decisions": [],
    }
    architecture["practical_choice_confirmation"]["architecture_sha256"] = architecture_confirmation_digest(
        architecture, load_architecture_policy({})
    )

    return {
        "project": {
            "name": "spec_freeze_integration",
            "stage": "local-mvp",
            "root_dir": ".",
            "output_dir": "projects/spec_freeze_integration",
            "artifacts_dir": "artifacts",
            "kicad_major_required": 10,
        },
        "requirement_intake": {
            "intake": {
                "project_name": "spec_freeze_integration",
                "user_intent": "Build a generic low-risk indoor control prototype.",
                "input_style": "use_case_only",
                "use_environment": "Indoor bench.",
                "connected_devices": "One generic low-speed external device.",
                "power_source": "Protected low-voltage bench source.",
                "size_or_mechanical": "No enclosure constraint for the MVP.",
                "manufacturing_intent": "Local MVP bare PCB.",
                "success_criteria": "The external device receives the requested state.",
                "system_boundary": "Power entry, function, and connector are on-board; the device is external.",
                "failure_consequence": "The prototype stops without a known safety hazard.",
                "desired_end_target": "local-mvp",
            },
            "budget_intent": {
                "status": "rough-range",
                "scope": "prototype-batch",
                "currency": "CNY",
                "target_amount": 100,
                "maximum_amount": 200,
                "quantity_basis": 2,
                "includes": ["components", "pcb-fabrication"],
                "priority": "balanced",
                "user_statement": "Use a conservative estimate for a two-board prototype batch.",
                "allow_mvp_without_limit": False,
            },
            "safety_screening": {
                "level": "standard",
                "hazards": [],
                "rationale": "Low-voltage indoor bench use.",
                "blocks_automatic_assumptions": False,
            },
            "evidence_inputs": {"available": ["confirmed use-case description"], "requested": []},
            "beginner": {
                "input_style": "use_case_only",
                "question_strategy": "practical_minimum",
                "round": 1,
                "max_rounds": 2,
                "max_questions": 5,
                "resolution_mode": "proceed_with_safe_assumptions",
                "questions": [],
                "deferred_professional_topics": ["Production sourcing evidence."],
            },
            "missing_information": {
                "must_ask_now": [],
                "can_defer": ["Final enclosure dimensions."],
                "blocks_mvp": [],
                "blocks_production": ["Production sourcing evidence."],
                "blocks_order_ready": ["External manufacturer review evidence."],
            },
            "safe_assumptions": [
                {
                    "id": "local-mvp-only",
                    "assumption": "Keep the result at local MVP.",
                    "reason": "Production evidence is intentionally deferred.",
                    "risk": "The output is not production-ready.",
                    "stage_allowed": "local-mvp",
                }
            ],
            "unsafe_assumptions": [
                {
                    "id": "production-release",
                    "unknown": "Final production sourcing and enclosure evidence.",
                    "why_not_guess": "They affect production and mechanical release.",
                    "required_before": "production-package",
                }
            ],
            "confirmation": {
                "status": "confirmed",
                "confirmed_by": "user",
                "intake_revision": 1,
                "confirmed_revision": 1,
                "user_response_summary": "Confirmed the use case, budget estimate, power style, and MVP target.",
            },
            "decision": {
                "current_target": "local-mvp",
                "can_create_spec": True,
                "can_generate_kicad": True,
                "can_run_erc_drc": True,
                "next_action": "Freeze the current MVP inputs before KiCad generation.",
            },
        },
        "user_profile": {
            "experience": "beginner",
            "input_style": "use_case_only",
            "use_case": "Create a generic low-risk indoor control prototype.",
        },
        "assumption_profile": {
            "level": "conservative_beginner_mvp",
            "production_claim_allowed": False,
            "order_ready_allowed": False,
            "professional_review_required": True,
            "unresolved_professional_decisions": ["Production sourcing evidence."],
        },
        "architecture": architecture,
        "requirements": {
            area: {"status": "confirmed", "summary": summary}
            for area, summary in {
                "function": "The generic behavior and success state are closed for the MVP.",
                "power": "The protected low-voltage source and conservative load estimate are closed.",
                "key_parts": "Generic library parts are sufficient for this local MVP fixture.",
                "interfaces": "One exposed low-speed interface is closed.",
                "mechanical": "No enclosure or mounting constraint applies to this MVP.",
                "manufacturing": "Two bare prototype boards are targeted.",
                "risk_review": "Boundary, power, and post-production risks are classified.",
            }.items()
        },
        "power_domains": [
            {
                "name": "BOARD_POWER",
                "nets": ["PWR", "GND"],
                "voltage": {"nominal_v": 5.0, "min_v": 4.5, "max_v": 5.5},
                "source": {"name": "J1", "net": "PWR", "current_limit_a": 1.0},
                "loads": [{"name": "FUNCTION_LOAD", "net": "PWR", "peak_current_a": 0.1, "quantity": 1}],
                "required_margin_percent": 20,
                "conductors": [{"name": "ENTRY_PATH", "current_rating_a": 1.0}],
                "protection": {"current_limit": {"current_rating_a": 0.5}},
            }
        ],
        "kicad": {
            "symbol_libraries": {"Device": "/usr/share/kicad/symbols/Device.kicad_sym", "Connector_Generic": "/usr/share/kicad/symbols/Connector_Generic.kicad_sym"},
            "footprint_libraries": {"Resistor_SMD": "/usr/share/kicad/footprints/Resistor_SMD.pretty", "Connector_PinHeader_2.54mm": "/usr/share/kicad/footprints/Connector_PinHeader_2.54mm.pretty"},
        },
        "schematic": {
            "connectivity": {"mode": "net_labels", "label_scope": "global", "default_label_shape": "passive"},
            "no_connects": [],
            "layout": {
                "origin_x_mm": 30.48,
                "origin_y_mm": 35.56,
                "column_spacing_mm": 38.1,
                "row_spacing_mm": 25.4,
                "columns": 3,
                "note_origin_x_mm": 25.4,
                "note_origin_y_mm": 120.65,
                "note_spacing_mm": 5.08,
                "font_size_mm": 1.27,
                "label_stub_mm": 2.54,
            },
            "property_offsets_mm": {
                "reference_y": -2.54,
                "value_y": 2.54,
                "footprint_y": 5.08,
                "datasheet_y": 7.62,
            },
        },
        "board": {
            "size_mm": {"width": 30.0, "height": 20.0},
            "origin_mm": {"x": 100.0, "y": 100.0},
            "layers": {"copper": 2},
            "stackup": {"board_thickness_mm": 1.6, "copper_oz": 1.0},
            "fabrication": {
                "min_track_width_mm": 0.2,
                "default_signal_width_mm": 0.25,
                "default_power_width_mm": 0.5,
                "min_clearance_mm": 0.2,
                "min_via_diameter_mm": 0.6,
                "min_via_drill_mm": 0.3,
            },
            "net_classes": [
                {"name": "Default", "clearance_mm": 0.2, "track_width_mm": 0.25, "via_diameter_mm": 0.6, "via_drill_mm": 0.3},
                {"name": "power", "clearance_mm": 0.2, "track_width_mm": 0.5, "via_diameter_mm": 0.6, "via_drill_mm": 0.3},
            ],
            "constraint_dispositions": {
                "outline": {"status": "defined", "rationale": "The MVP outline is bounded.", "references": ["board.size_mm"]},
                "stackup": {"status": "defined", "rationale": "A standard two-layer stack is declared.", "references": ["board.stackup", "board.layers"]},
                "fabrication": {"status": "defined", "rationale": "Conservative fabrication rules are declared.", "references": ["board.fabrication", "board.net_classes"]},
                "placement": {"status": "defined", "rationale": "The external connector boundary constrains placement.", "references": ["architecture.external_connectors"]},
                "mechanical": {"status": "not_applicable", "rationale": "The local MVP has no enclosure or mounting constraint."},
                "keepouts": {"status": "not_applicable", "rationale": "No antenna, isolation, or enclosure keepout applies."},
                "external_connectors": {"status": "defined", "rationale": "The exposed connector is declared in architecture.", "references": ["architecture.external_connectors"]},
                "high_current": {"status": "not_applicable", "rationale": "The declared peak load is below the high-current classification."},
                "controlled_impedance": {"status": "not_applicable", "rationale": "No high-speed or controlled-impedance interface is present."},
            },
        },
        "layout": {
            "schema_version": 1,
            "state": "ready",
            "revision": 1,
            "coordinate_system": {"units": "mm", "origin": "board.origin_mm", "x_axis": "right", "y_axis": "down"},
            "constraints": {"outline": {"status": "defined"}, "placement": {"status": "defined"}},
            "placement_batches": [{"id": "ALL", "order": 1, "refs": ["J1", "R1", "J2"], "rationale": "Freeze fixture placement batch."}],
        },
        "routing": {
            "schema_version": 1,
            "state": "ready",
            "revision": 1,
            "strategy": "generated",
            "batches": [{"id": "ALL_NETS", "order": 1, "nets": ["PWR", "SIGNAL", "GND"], "method": "generated", "state": "routed", "rationale": "Freeze fixture routing batch."}],
            "net_constraints": {
                "PWR": {"net_class": "power", "connection_mode": "tracks", "topology": "point-to-point", "allowed_layers": ["F.Cu"], "min_width_mm": 0.5, "max_vias": 0, "rationale": "Fixture power route."},
                "SIGNAL": {"net_class": "Default", "connection_mode": "tracks", "topology": "point-to-point", "allowed_layers": ["F.Cu"], "min_width_mm": 0.25, "max_vias": 0, "rationale": "Fixture signal route."},
                "GND": {"net_class": "power", "connection_mode": "tracks", "topology": "point-to-point", "allowed_layers": ["F.Cu"], "min_width_mm": 0.5, "max_vias": 0, "rationale": "Fixture return route."},
            },
        },
        "manufacturing": {
            "target": "generic-prototype-fabricator",
            "mode": "bare-pcb",
            "quantity": 2,
            "required_outputs": ["gerber", "drill"],
        },
        "nets": [
            {"name": "PWR", "class": "power"},
            {"name": "SIGNAL", "class": "signal"},
            {"name": "GND", "class": "power"},
        ],
        "components": [
            {"ref": "J1", "value": "POWER_ENTRY", "symbol": "Connector_Generic:Conn_01x02", "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical", "pads": {"1": "PWR", "2": "GND"}},
            {"ref": "R1", "value": "FUNCTION_PATH", "symbol": "Device:R", "footprint": "Resistor_SMD:R_0603_1608Metric", "pads": {"1": "PWR", "2": "SIGNAL"}},
            {"ref": "J2", "value": "EXTERNAL_INTERFACE", "symbol": "Connector_Generic:Conn_01x02", "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical", "pads": {"1": "SIGNAL", "2": "GND"}},
        ],
        "expected_net_graph": {
            "nets": {
                "PWR": {
                    "required_pins": ["J1.1", "R1.1"],
                    "exact": True,
                    "role": "supply",
                    "source_pins": ["J1.1"],
                    "sink_pins": ["R1.1"],
                },
                "SIGNAL": {"required_pins": ["R1.2", "J2.1"], "exact": True, "role": "signal"},
                "GND": {
                    "required_pins": ["J1.2", "J2.2"],
                    "exact": True,
                    "role": "return",
                    "return_pins": ["J1.2", "J2.2"],
                },
            }
        },
        "connectivity_batches": [
            {
                "name": "power_entry",
                "upstream_nets": [],
                "provided_nets": ["PWR", "GND"],
                "consumed_nets": [],
                "required_nets": ["PWR", "GND"],
                "components": ["J1", "R1", "J2"],
                "connections": [
                    {"net": "PWR", "pins": ["J1.1", "R1.1"]},
                    {"net": "GND", "pins": ["J1.2", "J2.2"]},
                ],
            },
            {
                "name": "functional_interface",
                "upstream_nets": ["PWR", "GND"],
                "provided_nets": ["SIGNAL"],
                "consumed_nets": ["PWR", "GND"],
                "required_nets": ["SIGNAL"],
                "components": ["R1", "J2"],
                "connections": [{"net": "SIGNAL", "pins": ["R1.2", "J2.1"]}],
            },
        ],
        "verification": {
            "signal_integrity": {"status": "not_applicable", "rationale": "No high-speed signal is present."},
            "power_integrity": {"status": "not_applicable", "rationale": "The low-current MVP uses a direct protected source."},
            "thermal": {"status": "not_applicable", "rationale": "No meaningful heat source is present in the fixture."},
            "emc": {"status": "not_applicable", "rationale": "The low-speed local fixture has no switching or wireless source."},
        },
        "todo": [
            {
                "category": "user_input",
                "status": "open",
                "blocking": True,
                "owner": "codex",
                "next_action": "Collect production sourcing evidence before advancing beyond local MVP.",
                "text": "Production sourcing evidence is intentionally deferred.",
            }
        ],
        "validation": {
            "spec_closure": {"required": True},
            "requirements": {"required": True},
            "power_budget": {"required": True, "default_margin_percent": 20},
            "verification": {"required": True},
            "connectivity_batches": {"required": True},
        },
    }


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="kicad-spec-freeze-") as temporary:
        root = Path(temporary)
        spec_path = root / "spec.yaml"
        candidate = base_spec(root)
        spec_path.write_text(yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True), encoding="utf-8")

        duplicate_source = copy.deepcopy(candidate)
        duplicate_source["selected_parts"] = []
        duplicate_result = CheckResult()
        check_freeze_contract(duplicate_source, spec_path, load_freeze_policy(), duplicate_result)
        if not any("duplicate source of truth" in issue for issue in duplicate_result.issues):
            failures.append("duplicate selected_parts source was not rejected")

        downgraded_todo = copy.deepcopy(candidate)
        downgraded_todo["todo"][0].update({"blocking": False, "status": "non_blocking"})
        downgraded_result = CheckResult()
        check_freeze_contract(downgraded_todo, spec_path, load_freeze_policy(), downgraded_result)
        if not any("may be non-blocking only" in issue for issue in downgraded_result.issues):
            failures.append("unjustified pre-fabrication TODO downgrade was not rejected")

        weakened_production = copy.deepcopy(candidate)
        weakened_production["project"]["stage"] = "production-package"
        weakened_production["validation"]["spec_closure"]["required_areas"] = ["stage"]
        weakened_result = CheckResult()
        check_freeze_contract(
            weakened_production,
            spec_path,
            load_freeze_policy(),
            weakened_result,
            production=True,
        )
        if not any("must not remove trusted areas" in issue for issue in weakened_result.issues):
            failures.append("production closure-policy reduction was not rejected")

        report = run([sys.executable, str(SCRIPTS_DIR / "architecture_report.py"), str(spec_path)], root)
        if report.returncode != 0:
            failures.append("architecture report setup failed: " + (report.stderr or report.stdout).strip())

        preview = run(
            [sys.executable, str(SCRIPTS_DIR / "product_baseline.py"), "--preview", str(spec_path)],
            root,
        )
        preview_path = root / "artifacts" / "spec-freeze" / "spec_freeze_integration" / "product-baseline.preview.md"
        if preview.returncode != 0:
            failures.append("product baseline preview failed: " + (preview.stdout + preview.stderr).strip())
        elif not preview_path.is_file():
            failures.append("product baseline preview was not written")
        else:
            preview_text = preview_path.read_text(encoding="utf-8")
            if "## PCB Build Brief" not in preview_text or "preview-not-frozen" not in preview_text:
                failures.append("product baseline preview is missing the brief or preview state")

        transaction = run(
            [sys.executable, str(SCRIPTS_DIR / "spec_freeze_transaction.py"), "--apply", "--require", str(spec_path)],
            root,
        )
        if transaction.returncode != 0:
            failures.append("freeze transaction failed: " + (transaction.stdout + transaction.stderr).strip())
        else:
            frozen_spec = load_spec(spec_path)
            baseline_metadata = frozen_spec.get("spec_freeze", {}).get("product_baseline", {})
            baseline_path = root / str(baseline_metadata.get("path", ""))
            if not baseline_path.is_file():
                failures.append("frozen product baseline was not written")
            else:
                baseline_text = baseline_path.read_text(encoding="utf-8")
                if "## PCB Build Brief" not in baseline_text:
                    failures.append("frozen product baseline is missing PCB Build Brief")
                if str(frozen_spec.get("spec_freeze", {}).get("spec_sha256", "")) not in baseline_text:
                    failures.append("frozen product baseline does not expose the bound Spec digest")

            check = run(
                [sys.executable, str(SCRIPTS_DIR / "spec_freeze_check.py"), "--before-generation", str(spec_path)],
                root,
            )
            if check.returncode != 0:
                failures.append("fresh freeze check failed: " + (check.stdout + check.stderr).strip())

            baseline_check = run(
                [sys.executable, str(SCRIPTS_DIR / "product_baseline.py"), "--check", "--require", str(spec_path)],
                root,
            )
            if baseline_check.returncode != 0:
                failures.append("product baseline check failed: " + (baseline_check.stdout + baseline_check.stderr).strip())

            if baseline_path.is_file():
                original_baseline = baseline_path.read_bytes()
                baseline_path.write_bytes(original_baseline + b"\nmanual change\n")
                changed_baseline = run(
                    [sys.executable, str(SCRIPTS_DIR / "spec_freeze_check.py"), "--before-generation", str(spec_path)],
                    root,
                )
                if changed_baseline.returncode == 0 or "Product baseline changed after freeze" not in (
                    changed_baseline.stdout + changed_baseline.stderr
                ):
                    failures.append("manually changed product baseline was not rejected")
                baseline_path.write_bytes(original_baseline)

            output_dir = root / "projects" / "spec_freeze_integration"
            output_dir.mkdir(parents=True, exist_ok=True)
            for filename in [
                "spec_freeze_integration.kicad_pro",
                "spec_freeze_integration.kicad_sch",
                "spec_freeze_integration.kicad_pcb",
                "sym-lib-table",
                "fp-lib-table",
                "library_audit.json",
                "library_audit.md",
                "bom.csv",
                "TODO.md",
            ]:
                (output_dir / filename).write_text(f"fixture {filename}\n", encoding="utf-8")
            schematic_checks_dir = root / "artifacts" / "checks" / "spec_freeze_integration"
            schematic_checks_dir.mkdir(parents=True, exist_ok=True)
            for filename in [
                "schematic-stage-manifest.yaml",
                "generated_netlist.xml",
                "generated_net_graph.json",
                "erc.rpt",
                "schematic-review.pdf",
            ]:
                (schematic_checks_dir / filename).write_text(f"fixture {filename}\n", encoding="utf-8")
            for directory, filename in [
                ("package-binding", "package-binding-stage.yaml"),
                ("layout-stage", "layout-stage.yaml"),
                ("routing-stage", "routing-stage.yaml"),
            ]:
                path = root / "artifacts" / directory / "spec_freeze_integration" / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"fixture {filename}\n", encoding="utf-8")
            local_dir = root / "artifacts" / "local-validation" / "spec_freeze_integration"
            local_dir.mkdir(parents=True, exist_ok=True)
            for filename in ["local-validation-manifest.yaml", "erc.json", "drc.json"]:
                (local_dir / filename).write_text(f"fixture {filename}\n", encoding="utf-8")
            generated_binding = run(
                [sys.executable, str(SCRIPTS_DIR / "spec_output_binding.py"), "--write-generated", str(spec_path)],
                root,
            )
            if generated_binding.returncode != 0:
                failures.append("generated output binding failed: " + (generated_binding.stdout + generated_binding.stderr).strip())

            checks_dir = root / "artifacts" / "checks" / "spec_freeze_integration"
            checks_dir.mkdir(parents=True, exist_ok=True)
            if not (checks_dir / "erc.rpt").is_file():
                (checks_dir / "erc.rpt").write_text("ERC fixture\n", encoding="utf-8")
            (checks_dir / "drc.rpt").write_text("DRC fixture\n", encoding="utf-8")
            fab_file = root / "artifacts" / "fab" / "spec_freeze_integration" / "gerbers" / "board.gbr"
            fab_file.parent.mkdir(parents=True, exist_ok=True)
            fab_file.write_text("Gerber fixture\n", encoding="utf-8")
            release_binding = run(
                [sys.executable, str(SCRIPTS_DIR / "spec_output_binding.py"), "--write-release", str(spec_path)],
                root,
            )
            if release_binding.returncode != 0:
                failures.append("release output binding failed: " + (release_binding.stdout + release_binding.stderr).strip())
            release_check = run(
                [sys.executable, str(SCRIPTS_DIR / "spec_output_binding.py"), "--check-release", str(spec_path)],
                root,
            )
            if release_check.returncode != 0:
                failures.append("release output check failed: " + (release_check.stdout + release_check.stderr).strip())
            fab_file.write_text("Changed Gerber fixture\n", encoding="utf-8")
            stale_output = run(
                [sys.executable, str(SCRIPTS_DIR / "spec_output_binding.py"), "--check-release", str(spec_path)],
                root,
            )
            if stale_output.returncode == 0 or "phase is stale: release" not in (stale_output.stdout + stale_output.stderr):
                failures.append("changed fabrication output was not rejected as stale")

            changed = load_spec(spec_path)
            changed["requirements"]["function"]["summary"] = "Changed after the local freeze."
            spec_path.write_text(yaml.safe_dump(changed, sort_keys=False, allow_unicode=True), encoding="utf-8")
            stale_result = CheckResult()
            check_frozen_spec(load_spec(spec_path), spec_path, stale_result, require=True)
            if stale_result.ok() or not any("changed after freeze" in issue.lower() for issue in stale_result.issues):
                failures.append("changed Spec was not rejected as stale")

            override = copy.deepcopy(changed)
            override.setdefault("validation", {}).setdefault("spec_freeze", {})["policy_file"] = "weaker.yaml"
            spec_path.write_text(yaml.safe_dump(override, sort_keys=False, allow_unicode=True), encoding="utf-8")
            override_result = run(
                [sys.executable, str(SCRIPTS_DIR / "spec_freeze_transaction.py"), "--require", str(spec_path)],
                root,
            )
            if override_result.returncode == 0 or "policy_file is forbidden" not in (override_result.stdout + override_result.stderr):
                failures.append("Spec Freeze policy override was not rejected")

    if failures:
        print("test_spec_freeze: FAIL")
        for failure in failures:
            print(f"ISSUE: {failure}")
        return 1
    print("test_spec_freeze: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
