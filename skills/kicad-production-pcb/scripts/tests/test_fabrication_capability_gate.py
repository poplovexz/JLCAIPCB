#!/usr/bin/env python3
"""Exercise fabrication capability triggers, stackup, freshness, and hash binding."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
GATE = SCRIPTS_DIR / "fabrication_capability_gate.py"
sys.path.insert(0, str(SCRIPTS_DIR))
import fabrication_capability_gate as gate  # noqa: E402
from _pcb_skill_checks import CheckResult, sha256_file  # noqa: E402


def write_yaml(path: Path, value: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    return sha256_file(path)


def stackup() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "board_thickness_mm": 1.6,
        "total_thickness_tolerance_mm": 0.01,
        "total_thickness_scope": "copper_and_dielectric",
        "layers": [
            {"type": "solder_mask", "name": "F.Mask", "material": "FIXTURE-MASK", "thickness_mm": 0.01, "epsilon_r": 3.4, "loss_tangent": 0.03},
            {"type": "copper", "name": "F.Cu", "thickness_mm": 0.035},
            {
                "type": "dielectric",
                "name": "PP1",
                "dielectric_type": "prepreg",
                "material": "FIXTURE-PREPREG",
                "thickness_mm": 0.2,
                "epsilon_r": 4.1,
                "loss_tangent": 0.02,
            },
            {"type": "copper", "name": "In1.Cu", "thickness_mm": 0.035},
            {
                "type": "dielectric",
                "name": "CORE1",
                "dielectric_type": "core",
                "material": "FIXTURE-CORE",
                "thickness_mm": 1.06,
                "epsilon_r": 4.2,
                "loss_tangent": 0.018,
            },
            {"type": "copper", "name": "In2.Cu", "thickness_mm": 0.035},
            {
                "type": "dielectric",
                "name": "PP2",
                "dielectric_type": "prepreg",
                "material": "FIXTURE-PREPREG",
                "thickness_mm": 0.2,
                "epsilon_r": 4.1,
                "loss_tangent": 0.02,
            },
            {"type": "copper", "name": "B.Cu", "thickness_mm": 0.035},
            {"type": "solder_mask", "name": "B.Mask", "material": "FIXTURE-MASK", "thickness_mm": 0.01, "epsilon_r": 3.4, "loss_tangent": 0.03},
        ],
    }


def descriptor(path: Path, digest: str, captured_at: str, source_url: str, max_age_hours: float) -> dict[str, Any]:
    return {
        "path": str(path),
        "sha256": digest,
        "captured_at": captured_at,
        "max_age_hours": max_age_hours,
        "source_url": source_url,
    }


def fixture(root: Path, now: datetime) -> tuple[dict[str, Any], Path]:
    captured_at = (now - timedelta(hours=1)).isoformat()
    capability_url = "https://jlcpcb.com/capabilities"
    impedance_url = "https://solver.example/reports/fixture"
    physical_stackup = stackup()
    stackup_digest = gate.canonical_sha256(physical_stackup)
    evidence_dir = root / "evidence"
    capability_path = evidence_dir / "capability.yaml"
    impedance_path = evidence_dir / "impedance.yaml"

    capability = {
        "schema_version": 1,
        "manufacturer_id": "jlcpcb",
        "captured_at": captured_at,
        "source_url": capability_url,
        "capabilities": {
            "supported_copper_layer_counts": [4],
            "supported_stackup_digests": [stackup_digest],
            "supported_process_features": ["multilayer", "controlled_impedance", "microvia"],
        },
    }
    capability_sha = write_yaml(capability_path, capability)

    geometry = {
        "layer": "F.Cu",
        "trace_width_mm": 0.14,
        "gap_mm": 0.16,
        "reference_layers": ["In1.Cu"],
    }
    impedance = {
        "schema_version": 1,
        "captured_at": captured_at,
        "source_url": impedance_url,
        "stackup_digest": stackup_digest,
        "targets": [
            {
                "id": "USB2",
                "nets": ["USB_D+", "USB_D-"],
                "target_ohm": 90,
                "tolerance_ohm": 9,
                "geometry": geometry,
                "model": {"tool": "fixture-field-solver", "version": "1", "method": "2D"},
                "result": {"status": "passed", "calculated_ohm": 91.2},
            }
        ],
    }
    impedance_sha = write_yaml(impedance_path, impedance)
    laser_drill_path = root / "outputs" / "laser" / "microvia.drl"
    laser_drill_path.parent.mkdir(parents=True, exist_ok=True)
    laser_drill_path.write_text("; #@! TF.FileFunction,LaserDrill,F.Cu,In1.Cu\nM30\n", encoding="utf-8")

    spec = {
        "project": {
            "name": "fabrication_fixture",
            "stage": "local-mvp",
            "root_dir": ".",
            "artifacts_dir": "artifacts",
        },
        "board": {
            "layers": {"copper": 4},
            "stackup": physical_stackup,
            "constraint_dispositions": {"controlled_impedance": {"status": "defined"}},
        },
        "vias": [{"net": "USB_D+", "via_type": "microvia"}],
        "manufacturing": {
            "required_outputs": ["gerber", "drill", "laser_drill"],
            "fabrication_capability": {
                "manufacturer_id": "jlcpcb",
                "requested_features": ["multilayer", "controlled_impedance", "microvia"],
                "process_dispositions": {
                    "microvia": {
                        "mode": "dedicated_fabrication_output",
                        "outputs": ["laser_drill"],
                        "artifact_patterns": [{
                            "role": "laser_drill",
                            "path_glob": "outputs/laser/*.drl",
                            "content_regex": "TF\\.FileFunction,LaserDrill,F\\.Cu,In1\\.Cu",
                        }],
                        "rationale": "Fixture laser drill deliverable is explicit.",
                    }
                },
                "evidence": descriptor(capability_path, capability_sha, captured_at, capability_url, 24),
            },
        },
        "verification": {
            "signal_integrity": {
                "differential_pairs": [
                    {
                        "name": "USB2",
                        "nets": ["USB_D+", "USB_D-"],
                        "impedance_ohm": 90,
                        "impedance_tolerance_ohm": 9,
                        "geometry": geometry,
                    }
                ],
                "impedance_evidence": descriptor(impedance_path, impedance_sha, captured_at, impedance_url, 24),
            }
        },
        "routing": {
            "net_constraints": {
                "USB_D+": {"allowed_layers": ["F.Cu"], "preferred_width_mm": 0.14, "max_neckdown_length_mm": 0},
                "USB_D-": {"allowed_layers": ["F.Cu"], "preferred_width_mm": 0.14, "max_neckdown_length_mm": 0},
            },
            "differential_pairs": [{"nets": ["USB_D+", "USB_D-"], "target_gap_mm": 0.16}],
        },
    }
    spec_path = root / "spec.yaml"
    return spec, spec_path


def run_check(spec: dict[str, Any], spec_path: Path, now: datetime) -> tuple[CheckResult, dict[str, Any]]:
    result = CheckResult()
    details = gate.check_fabrication_capability(spec, spec_path, result, now=now)
    return result, details


def main() -> int:
    failures: list[str] = []
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix="fabrication-capability-") as temporary:
        root = Path(temporary)
        spec, spec_path = fixture(root, now)

        result, details = run_check(spec, spec_path, now)
        if result.issues or details.get("status") != "passed":
            failures.append(f"valid capability and impedance evidence failed: {result.issues}")

        result = CheckResult()
        details = gate.check_fabrication_capability(spec, spec_path, result, now=now, check_outputs=True)
        if result.issues or not details.get("process_output_artifacts", {}).get("microvia"):
            failures.append(f"valid special-process fabrication output failed: {result.issues}")

        missing_artifact = copy.deepcopy(spec)
        missing_artifact["manufacturing"]["fabrication_capability"]["process_dispositions"]["microvia"]["artifact_patterns"][0]["path_glob"] = "outputs/laser/missing-*.drl"
        result = CheckResult()
        gate.check_fabrication_capability(missing_artifact, spec_path, result, now=now, check_outputs=True)
        if not any("matched no fabrication output" in issue for issue in result.issues):
            failures.append(f"missing special-process fabrication output was not rejected: {result.issues}")

        routing_mismatch = copy.deepcopy(spec)
        routing_mismatch["routing"]["net_constraints"]["USB_D+"]["allowed_layers"] = ["B.Cu"]
        routing_mismatch["routing"]["net_constraints"]["USB_D+"]["preferred_width_mm"] = 0.5
        routing_mismatch["routing"]["differential_pairs"][0]["target_gap_mm"] = 0.5
        result, _details = run_check(routing_mismatch, spec_path, now)
        if not any("does not match impedance" in issue or "does not include impedance" in issue for issue in result.issues):
            failures.append(f"routing/impedance geometry mismatch was not rejected: {result.issues}")

        legacy = {
            "project": {"name": "legacy", "stage": "production", "root_dir": ".", "artifacts_dir": "artifacts"},
            "board": {
                "layers": {"copper": 2},
                "stackup": {"board_thickness_mm": 1.6, "copper_oz": 1},
                "constraint_dispositions": {"controlled_impedance": {"status": "not_applicable"}},
            },
        }
        result, details = run_check(legacy, root / "legacy.yaml", now)
        if result.issues or details.get("required") is not False or details.get("status") != "not_required":
            failures.append(f"ordinary two-layer production fixture did not remain a no-op: {result.issues}")

        policy = gate.load_policy()
        blind = copy.deepcopy(legacy)
        blind["routing"] = {
            "net_constraints": {
                "USB_D+": {
                    "allowed_via_types": ["through", "blind"],
                    "via_type_by_layer_pair": [{"layers": ["F.Cu", "B.Cu"], "via_type": "blind"}],
                }
            }
        }
        requirement = gate.determine_requirement(blind, policy, 2)
        if not requirement.get("required") or requirement.get("special_via_types") != ["blind"]:
            failures.append(f"routing.net_constraints blind via did not trigger the gate: {requirement}")

        backdrill = copy.deepcopy(legacy)
        backdrill["routing"] = {
            "net_constraints": {"PCIE_TX": {"backdrill": {"allowed": True, "required": True}}}
        }
        requirement = gate.determine_requirement(backdrill, policy, 2)
        if not requirement.get("required") or requirement.get("special_via_types") != ["backdrill"]:
            failures.append(f"routing.net_constraints backdrill did not trigger the gate: {requirement}")

        incomplete = copy.deepcopy(spec)
        first_dielectric = next(
            layer
            for layer in incomplete["board"]["stackup"]["layers"]
            if layer.get("type") == "dielectric"
        )
        del first_dielectric["epsilon_r"]
        result, _details = run_check(incomplete, spec_path, now)
        if not any("epsilon_r is required" in issue for issue in result.issues):
            failures.append(f"incomplete dielectric data was not rejected: {result.issues}")

        missing_output = copy.deepcopy(spec)
        missing_output["manufacturing"]["required_outputs"].remove("laser_drill")
        result, _details = run_check(missing_output, spec_path, now)
        if not any("absent from manufacturing.required_outputs" in issue for issue in result.issues):
            failures.append(f"special process without a declared fabrication output passed: {result.issues}")

        stale = copy.deepcopy(spec)
        stale_at = (now - timedelta(hours=48)).isoformat()
        stale_path = root / "evidence" / "capability-stale.yaml"
        stale_payload = yaml.safe_load(Path(spec["manufacturing"]["fabrication_capability"]["evidence"]["path"]).read_text(encoding="utf-8"))
        stale_payload["captured_at"] = stale_at
        stale_sha = write_yaml(stale_path, stale_payload)
        stale["manufacturing"]["fabrication_capability"]["evidence"] = descriptor(
            stale_path,
            stale_sha,
            stale_at,
            stale_payload["source_url"],
            1,
        )
        result, _details = run_check(stale, spec_path, now)
        if not any("is stale" in issue for issue in result.issues):
            failures.append(f"stale manufacturer capability evidence was not rejected: {result.issues}")

        excessive_age_window = copy.deepcopy(spec)
        excessive_age_window["manufacturing"]["fabrication_capability"]["evidence"]["max_age_hours"] = 1_000_000
        result, _details = run_check(excessive_age_window, spec_path, now)
        if not any("exceeds the trusted policy maximum" in issue for issue in result.issues):
            failures.append(f"spec-controlled excessive evidence age window was not rejected: {result.issues}")

        bad_hash = copy.deepcopy(spec)
        bad_hash["manufacturing"]["fabrication_capability"]["evidence"]["sha256"] = "0" * 64
        result, _details = run_check(bad_hash, spec_path, now)
        if not any("sha256 does not match" in issue for issue in result.issues):
            failures.append(f"manufacturer capability hash mismatch was not rejected: {result.issues}")

        bad_impedance_hash = copy.deepcopy(spec)
        bad_impedance_hash["verification"]["signal_integrity"]["impedance_evidence"]["sha256"] = "f" * 64
        result, _details = run_check(bad_impedance_hash, spec_path, now)
        if not any("sha256 does not match" in issue for issue in result.issues):
            failures.append(f"impedance evidence hash mismatch was not rejected: {result.issues}")

        fake_manufacturer_source = copy.deepcopy(spec)
        fake_source_url = "https://example.com/capabilities"
        fake_source_path = root / "evidence" / "capability-fake-source.yaml"
        fake_source_payload = yaml.safe_load(
            Path(spec["manufacturing"]["fabrication_capability"]["evidence"]["path"]).read_text(encoding="utf-8")
        )
        fake_source_payload["source_url"] = fake_source_url
        fake_source_sha = write_yaml(fake_source_path, fake_source_payload)
        fake_manufacturer_source["manufacturing"]["fabrication_capability"]["evidence"] = descriptor(
            fake_source_path,
            fake_source_sha,
            fake_source_payload["captured_at"],
            fake_source_url,
            24,
        )
        result, _details = run_check(fake_manufacturer_source, spec_path, now)
        if not any("host is not trusted for manufacturer_id jlcpcb" in issue for issue in result.issues):
            failures.append(f"non-manufacturer capability source host was not rejected: {result.issues}")

        unknown_manufacturer = copy.deepcopy(spec)
        unknown_manufacturer["manufacturing"]["fabrication_capability"]["manufacturer_id"] = "unknown-fabricator"
        result, _details = run_check(unknown_manufacturer, spec_path, now)
        if not any("is not present in the trusted manufacturer source registry" in issue for issue in result.issues):
            failures.append(f"unknown manufacturer did not fail closed: {result.issues}")

        single_ended = copy.deepcopy(spec)
        single_geometry = {
            "layer": "F.Cu",
            "trace_width_mm": 0.18,
            "reference_layers": ["In1.Cu"],
        }
        signal_integrity = single_ended["verification"]["signal_integrity"]
        signal_integrity["differential_pairs"] = []
        signal_integrity["single_ended_nets"] = [
            {
                "name": "CLK",
                "net": "CLK",
                "impedance_ohm": 50,
                "impedance_tolerance_ohm": 5,
                "geometry": single_geometry,
            }
        ]
        single_ended["routing"]["net_constraints"] = {
            "CLK": {"allowed_layers": ["F.Cu"], "preferred_width_mm": 0.18, "max_neckdown_length_mm": 0}
        }
        single_ended["routing"]["differential_pairs"] = []
        single_impedance_path = root / "evidence" / "impedance-single-ended.yaml"
        single_impedance_payload = {
            "schema_version": 1,
            "captured_at": signal_integrity["impedance_evidence"]["captured_at"],
            "source_url": signal_integrity["impedance_evidence"]["source_url"],
            "stackup_digest": gate.canonical_sha256(single_ended["board"]["stackup"]),
            "targets": [
                {
                    "id": "CLK",
                    "nets": ["CLK"],
                    "target_ohm": 50,
                    "tolerance_ohm": 5,
                    "geometry": single_geometry,
                    "model": {"tool": "fixture-field-solver", "version": "1", "method": "2D"},
                    "result": {"status": "passed", "calculated_ohm": 49.5},
                }
            ],
        }
        single_impedance_sha = write_yaml(single_impedance_path, single_impedance_payload)
        signal_integrity["impedance_evidence"] = descriptor(
            single_impedance_path,
            single_impedance_sha,
            single_impedance_payload["captured_at"],
            single_impedance_payload["source_url"],
            24,
        )
        result, details = run_check(single_ended, spec_path, now)
        if result.issues or details.get("status") != "passed":
            failures.append(f"single-ended impedance target without differential gap failed: {result.issues}")

        write_yaml(spec_path, spec)
        report = root / "audit" / "fabrication-capability.json"
        completed = subprocess.run(
            [sys.executable, str(GATE), "--before-generation", "--json", "--report-output", str(report), str(spec_path)],
            cwd=root,
            text=True,
            capture_output=True,
        )
        try:
            cli_payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            cli_payload = {}
        if completed.returncode or cli_payload.get("ok") is not True or not report.is_file():
            failures.append(
                "auditable JSON CLI/report failed: "
                f"exit={completed.returncode} stdout={completed.stdout!r} stderr={completed.stderr!r}"
            )
        elif json.loads(report.read_text(encoding="utf-8")).get("details", {}).get("status") != "passed":
            failures.append("auditable JSON report did not record a passing status")

    if failures:
        print("fabrication capability gate tests: FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("fabrication capability gate tests: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
