#!/usr/bin/env python3
"""Strict local ERC/DRC execution and current-input evidence binding."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from _layout_stage import check_evidence as check_layout_evidence
from _package_binding_stage import artifacts_root, check_stage_evidence as check_package_evidence, mapping, project_root, resolve, sequence, strings
from _part_lock import replace_files_transactionally
from _pcb_skill_checks import CheckResult, get_path, sha256_file, string_value
from _routing_stage import check_stage_evidence as check_routing_evidence
from _schematic_stage import check_evidence as check_schematic_evidence, load_policy as load_schematic_policy


SKILL_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = SKILL_ROOT / "assets" / "local-validation-policy.yaml"


def load_policy() -> dict[str, Any]:
    data = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("local validation policy must be a mapping")
    return data


def normalized(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def stage_required(spec: dict[str, Any], force: bool = False) -> bool:
    if force or get_path(spec, "validation.local_validation.required") is True:
        return True
    required = {normalized(value) for value in strings(load_policy().get("required_project_stages"))}
    return normalized(get_path(spec, "project.stage")) in required


def paths(spec: dict[str, Any], spec_path: Path) -> dict[str, Path]:
    policy = load_policy()
    root = project_root(spec, spec_path)
    artifacts = artifacts_root(spec, root)
    project_name = str(get_path(spec, "project.name"))
    project_dir = resolve(root, get_path(spec, "project.output_dir"))
    target = artifacts / str(policy["artifact_subdir"]) / project_name
    checks = artifacts / "checks" / project_name
    return {
        "root": root,
        "artifacts": artifacts,
        "target": target,
        "manifest": target / str(policy["manifest_filename"]),
        "erc_json": target / str(policy["erc_json_filename"]),
        "drc_json": target / str(policy["drc_json_filename"]),
        "erc_report": checks / str(policy["erc_report_filename"]),
        "drc_report": checks / str(policy["drc_report_filename"]),
        "schematic": project_dir / f"{project_name}.kicad_sch",
        "pcb": project_dir / f"{project_name}.kicad_pcb",
    }


def file_record(path: Path, root: Path) -> dict[str, Any]:
    try:
        display = str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        display = str(path.resolve())
    return {"path": display, "sha256": sha256_file(path), "size_bytes": path.stat().st_size}


def executor_dependencies(policy: dict[str, Any]) -> list[dict[str, str]]:
    records = []
    for configured in strings(policy.get("executor_dependencies")):
        path = SKILL_ROOT / configured
        records.append({"path": configured, "sha256": sha256_file(path)})
    return records


def prerequisite_checks(spec: dict[str, Any], spec_path: Path, result: CheckResult) -> None:
    schematic_result = CheckResult()
    check_schematic_evidence(spec, spec_path, load_schematic_policy(), schematic_result, force=True)
    package_result = CheckResult()
    check_package_evidence(spec, spec_path, package_result, force=True)
    layout_result = CheckResult()
    check_layout_evidence(spec, spec_path, layout_result, force=True)
    routing_result = CheckResult()
    check_routing_evidence(spec, spec_path, routing_result, force=True)
    for label, checked in [("schematic", schematic_result), ("package binding", package_result), ("layout", layout_result), ("routing", routing_result)]:
        result.issues.extend(f"{label} prerequisite: {issue}" for issue in checked.issues)
        result.warnings.extend(f"{label} prerequisite: {warning}" for warning in checked.warnings)


def run_command(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(command, text=True, capture_output=True)
    return {"command": command, "exit_code": completed.returncode, "stdout": completed.stdout.strip(), "stderr": completed.stderr.strip()}


def erc_violations(payload: dict[str, Any]) -> list[Any]:
    return [violation for sheet in sequence(payload.get("sheets")) if isinstance(sheet, dict) for violation in sequence(sheet.get("violations"))]


def counts(erc: dict[str, Any], drc: dict[str, Any]) -> dict[str, int]:
    return {
        "erc_violations": len(erc_violations(erc)),
        "drc_violations": len(sequence(drc.get("violations"))),
        "unconnected_items": len(sequence(drc.get("unconnected_items"))),
        "schematic_parity": len(sequence(drc.get("schematic_parity"))),
    }


def report_bytes(label: str, values: dict[str, int], kicad_version: str) -> bytes:
    if label == "ERC":
        text = f"ERC messages: {values['erc_violations']} Errors 0 Warnings 0\n"
    else:
        text = f"Found {values['drc_violations']} DRC violations\nFound {values['unconnected_items']} unconnected items\nFound {values['schematic_parity']} schematic parity violations\n"
    return (f"{label} strict local validation\nKiCad: {kicad_version}\n{text}").encode("utf-8")


def build_id(spec_path: Path, schematic: Path, pcb: Path, kicad_version: str) -> str:
    payload = "\n".join([sha256_file(spec_path), sha256_file(schematic), sha256_file(pcb), kicad_version]).encode()
    return hashlib.sha256(payload).hexdigest()


def validate_counts(values: dict[str, int], policy: dict[str, Any], result: CheckResult) -> None:
    for key, expected in mapping(policy.get("required_results")).items():
        if values.get(key) != expected:
            result.issue(f"local validation {key}={values.get(key)}; required {expected}")


def run_validation(spec: dict[str, Any], spec_path: Path, result: CheckResult, force: bool = False) -> dict[str, Any]:
    required = stage_required(spec, force)
    details: dict[str, Any] = {"required": required}
    if not required:
        result.warning("Local validation stage is not required for this legacy/draft spec")
        return details
    policy = load_policy()
    target_paths = paths(spec, spec_path)
    details["manifest"] = str(target_paths["manifest"])
    target_paths["target"].mkdir(parents=True, exist_ok=True)
    target_paths["manifest"].unlink(missing_ok=True)
    for key in ["schematic", "pcb"]:
        if not target_paths[key].is_file():
            result.issue(f"local validation input is missing: {target_paths[key]}")
    prerequisite_checks(spec, spec_path, result)
    version_run = run_command(["kicad-cli", "version"])
    kicad_version = str(version_run["stdout"])
    required_major = get_path(spec, "project.kicad_major_required")
    if version_run["exit_code"] or not kicad_version.startswith(f"{required_major}."):
        result.issue(f"local validation requires KiCad major {required_major}, found {kicad_version or '<unknown>'}")

    with tempfile.TemporaryDirectory(prefix="candidate-", dir=target_paths["target"]) as temporary:
        candidate = Path(temporary)
        erc_json = candidate / str(policy["erc_json_filename"])
        drc_json = candidate / str(policy["drc_json_filename"])
        commands: dict[str, Any] = {"kicad_version": version_run}
        if result.ok():
            commands["erc"] = run_command(["kicad-cli", "sch", "erc", "--format", "json", "--severity-all", "--exit-code-violations", "--output", str(erc_json), str(target_paths["schematic"])])
            commands["drc"] = run_command(["kicad-cli", "pcb", "drc", "--format", "json", "--severity-all", "--refill-zones", "--exit-code-violations", "--output", str(drc_json), str(target_paths["pcb"])])
            for label in ["erc", "drc"]:
                if commands[label]["exit_code"]:
                    result.issue(f"strict {label.upper()} command failed with exit {commands[label]['exit_code']}")
        erc = json.loads(erc_json.read_text(encoding="utf-8")) if erc_json.is_file() else {}
        drc = json.loads(drc_json.read_text(encoding="utf-8")) if drc_json.is_file() else {}
        values = counts(erc, drc)
        validate_counts(values, policy, result)
        erc_report = candidate / str(policy["erc_report_filename"])
        drc_report = candidate / str(policy["drc_report_filename"])
        erc_report.write_bytes(report_bytes("ERC", values, kicad_version))
        drc_report.write_bytes(report_bytes("DRC", values, kicad_version))
        input_records = {}
        if all(target_paths[key].is_file() for key in ["schematic", "pcb"]):
            input_records = {key: file_record(target_paths[key], target_paths["root"]) for key in ["schematic", "pcb"]}
            input_records["spec"] = file_record(spec_path, target_paths["root"])
        output_sources = {"erc_json": erc_json, "drc_json": drc_json, "erc_report": erc_report, "drc_report": drc_report}
        outputs = {key: {"sha256": sha256_file(path), "size_bytes": path.stat().st_size} for key, path in output_sources.items() if path.is_file()}
        manifest = {
            "schema_version": policy["manifest_schema_version"], "status": "passed" if result.ok() else "failed",
            "project_name": get_path(spec, "project.name"), "generated_at": datetime.now(timezone.utc).isoformat(),
            "build_id": build_id(spec_path, target_paths["schematic"], target_paths["pcb"], kicad_version) if input_records else None,
            "kicad_cli_version": kicad_version, "policy_sha256": sha256_file(POLICY_PATH),
            "executor_sha256": sha256_file(Path(__file__).resolve()), "executor_dependencies": executor_dependencies(policy),
            "inputs": input_records, "commands": commands, "results": values, "outputs": outputs,
            "issues": list(result.issues), "warnings": list(result.warnings),
        }
        manifest_file = candidate / str(policy["manifest_filename"])
        manifest_file.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        replacements = [(target_paths["manifest"], manifest_file.read_bytes()), (target_paths["erc_report"], erc_report.read_bytes()), (target_paths["drc_report"], drc_report.read_bytes())]
        if erc_json.is_file():
            replacements.append((target_paths["erc_json"], erc_json.read_bytes()))
        if drc_json.is_file():
            replacements.append((target_paths["drc_json"], drc_json.read_bytes()))
        replace_files_transactionally(replacements, target_paths["manifest"].with_suffix(".txn.json"))
    details.update({"build_id": manifest.get("build_id"), "results": values, "status": manifest["status"]})
    return details


def check_evidence(spec: dict[str, Any], spec_path: Path, result: CheckResult, force: bool = False) -> dict[str, Any]:
    required = stage_required(spec, force)
    details: dict[str, Any] = {"required": required}
    if not required:
        result.warning("Local validation stage is not required for this legacy/draft spec")
        return details
    policy = load_policy()
    target_paths = paths(spec, spec_path)
    manifest_path = target_paths["manifest"]
    details["manifest"] = str(manifest_path)
    if not manifest_path.is_file():
        result.issue(f"local validation manifest is missing: {manifest_path}")
        return details
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    if manifest.get("schema_version") != policy["manifest_schema_version"] or manifest.get("status") != "passed":
        result.issue("local validation manifest status/schema is not passing")
    if manifest.get("project_name") != get_path(spec, "project.name") or manifest.get("policy_sha256") != sha256_file(POLICY_PATH) or manifest.get("executor_sha256") != sha256_file(Path(__file__).resolve()):
        result.issue("local validation project, policy, or executor binding is stale")
    if sequence(manifest.get("executor_dependencies")) != executor_dependencies(policy):
        result.issue("local validation executor dependencies changed")
    expected_inputs = {key: file_record(target_paths[key], target_paths["root"]) for key in ["schematic", "pcb"] if target_paths[key].is_file()}
    expected_inputs["spec"] = file_record(spec_path, target_paths["root"])
    if mapping(manifest.get("inputs")) != expected_inputs:
        result.issue("local validation inputs do not match current Spec/schematic/PCB")
    validate_counts(mapping(manifest.get("results")), policy, result)
    for key in ["erc_json", "drc_json", "erc_report", "drc_report"]:
        path = target_paths[key]
        record = mapping(get_path(manifest, f"outputs.{key}"))
        if not path.is_file() or record.get("sha256") != sha256_file(path) or record.get("size_bytes") != path.stat().st_size:
            result.issue(f"local validation output is missing or stale: {key}")
    for key in ["erc", "drc"]:
        if get_path(manifest, f"commands.{key}.exit_code") != 0:
            result.issue(f"local validation {key} command did not pass")
    prerequisite_checks(spec, spec_path, result)
    details.update({"build_id": manifest.get("build_id"), "results": manifest.get("results"), "status": manifest.get("status")})
    return details
