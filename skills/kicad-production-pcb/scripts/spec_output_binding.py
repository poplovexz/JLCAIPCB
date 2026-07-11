#!/usr/bin/env python3
"""Bind generated KiCad and fabrication outputs to the current Spec Freeze."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _part_lock import replace_files_transactionally  # noqa: E402
from _pcb_skill_checks import CheckResult, get_path, load_spec, print_result, sha256_file, string_value  # noqa: E402
from _spec_freeze import (  # noqa: E402
    artifacts_root,
    check_frozen_spec,
    display_path,
    freeze_required,
    load_policy,
    resolve_from_root,
    utc_timestamp,
)


def mapping_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def template_context(spec: dict[str, Any]) -> dict[str, str]:
    return {
        "project_name": str(get_path(spec, "project.name") or ""),
        "output_dir": str(get_path(spec, "project.output_dir") or ""),
        "artifacts_dir": str(get_path(spec, "project.artifacts_dir") or ""),
    }


def render_path(template: Any, context: dict[str, str], label: str, result: CheckResult) -> str | None:
    if not string_value(template):
        result.issue(f"Spec output policy {label} must be a non-empty path template")
        return None
    try:
        rendered = str(template).format_map(context)
    except (KeyError, ValueError) as error:
        result.issue(f"Spec output policy {label} has an invalid placeholder: {error}")
        return None
    if not rendered.strip():
        result.issue(f"Spec output policy {label} rendered an empty path")
        return None
    return rendered


def output_manifest_path(
    spec: dict[str, Any], spec_path: Path, policy: dict[str, Any], result: CheckResult
) -> tuple[Path, Path]:
    root, artifacts = artifacts_root(spec, spec_path, policy, result)
    directory = str(get_path(policy, "paths.manifest_directory") or "")
    filename = str(get_path(policy, "downstream_evidence.manifest_filename") or "")
    project_name = str(get_path(spec, "project.name") or spec_path.stem)
    safe_project = "".join(
        character if character.isalnum() or character in "._-" else "_" for character in project_name
    ).strip("._-") or "project"
    target = (artifacts / directory / safe_project / filename).resolve()
    try:
        target.relative_to(artifacts)
    except ValueError:
        result.issue(f"Spec output manifest must stay under project.artifacts_dir: {target}")
    return root, target


def file_record(path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": display_path(path, root),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def collect_phase(
    spec: dict[str, Any], spec_path: Path, policy: dict[str, Any], phase: str, result: CheckResult
) -> list[dict[str, Any]]:
    root, _artifacts = artifacts_root(spec, spec_path, policy, result)
    config = mapping_value(get_path(policy, f"downstream_evidence.{phase}"))
    context = template_context(spec)
    records: dict[str, dict[str, Any]] = {}

    for index, template in enumerate(list_value(config.get("required_files"))):
        rendered = render_path(template, context, f"{phase}.required_files[{index}]", result)
        if rendered is None:
            continue
        path = resolve_from_root(rendered, root)
        if not path.is_file():
            result.issue(f"Spec output binding requires file: {path}")
            continue
        record = file_record(path, root)
        records[str(record["path"])] = record

    for index, template in enumerate(list_value(config.get("required_directories"))):
        rendered = render_path(template, context, f"{phase}.required_directories[{index}]", result)
        if rendered is None:
            continue
        directory = resolve_from_root(rendered, root)
        if not directory.is_dir():
            result.issue(f"Spec output binding requires directory: {directory}")
            continue
        files = sorted(path for path in directory.rglob("*") if path.is_file())
        if not files:
            result.issue(f"Spec output binding directory is empty: {directory}")
        for path in files:
            record = file_record(path, root)
            records[str(record["path"])] = record
    return [records[key] for key in sorted(records)]


def freeze_binding(spec: dict[str, Any]) -> dict[str, Any]:
    freeze = mapping_value(spec.get("spec_freeze"))
    manifest = mapping_value(freeze.get("manifest"))
    return {
        "revision": freeze.get("revision"),
        "tier": freeze.get("tier"),
        "spec_sha256": freeze.get("spec_sha256"),
        "policy_sha256": freeze.get("policy_sha256"),
        "manifest_path": manifest.get("path"),
        "manifest_sha256": manifest.get("sha256"),
    }


def phase_map(manifest: dict[str, Any]) -> dict[str, Any]:
    phases = manifest.get("phases")
    return phases if isinstance(phases, dict) else {}


def validate_phase_records(
    spec: dict[str, Any], spec_path: Path, policy: dict[str, Any], phase: str, manifest: dict[str, Any], result: CheckResult
) -> None:
    phase_data = phase_map(manifest).get(phase)
    if not isinstance(phase_data, dict):
        result.issue(f"Spec output manifest is missing phase: {phase}")
        return
    expected = collect_phase(spec, spec_path, policy, phase, result)
    observed = phase_data.get("files")
    if not isinstance(observed, list):
        result.issue(f"Spec output manifest {phase}.files must be a list")
        return
    if observed != expected:
        result.issue(f"Spec output manifest phase is stale: {phase}")


def load_output_manifest(path: Path, result: CheckResult) -> dict[str, Any]:
    if not path.is_file():
        result.issue(f"Spec output manifest is missing: {path}")
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as error:
        result.issue(f"Cannot read Spec output manifest {path}: {error}")
        return {}
    if not isinstance(data, dict):
        result.issue(f"Spec output manifest must be a mapping: {path}")
        return {}
    return data


def validate_manifest_header(
    spec: dict[str, Any], policy: dict[str, Any], manifest: dict[str, Any], result: CheckResult
) -> None:
    expected_schema = get_path(policy, "downstream_evidence.schema_version")
    if manifest.get("schema_version") != expected_schema:
        result.issue(f"Spec output manifest schema_version must be {expected_schema}")
    if manifest.get("project_name") != get_path(spec, "project.name"):
        result.issue("Spec output manifest project_name does not match the spec")
    if mapping_value(manifest.get("spec_freeze")) != freeze_binding(spec):
        result.issue("Spec output manifest is bound to a different Spec Freeze")
    if manifest.get("executor_sha256") != sha256_file(Path(__file__).resolve()):
        result.issue("Spec output binding executor changed; regenerate downstream evidence")


def write_phase(
    spec: dict[str, Any], spec_path: Path, policy: dict[str, Any], phase: str, result: CheckResult
) -> Path | None:
    _root, target = output_manifest_path(spec, spec_path, policy, result)
    if not result.ok():
        return None
    if phase == "generated":
        manifest: dict[str, Any] = {
            "schema_version": get_path(policy, "downstream_evidence.schema_version"),
            "executor_sha256": sha256_file(Path(__file__).resolve()),
            "project_name": get_path(spec, "project.name"),
            "spec_freeze": freeze_binding(spec),
            "phases": {},
        }
    else:
        manifest = load_output_manifest(target, result)
        if result.ok():
            validate_manifest_header(spec, policy, manifest, result)
            validate_phase_records(spec, spec_path, policy, "generated", manifest, result)
    records = collect_phase(spec, spec_path, policy, phase, result) if result.ok() else []
    if not result.ok():
        return None
    phases = manifest.setdefault("phases", {})
    phases[phase] = {"recorded_at": utc_timestamp(), "files": records}
    data = yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True).encode("utf-8")
    replace_files_transactionally(
        [(target, data)],
        target.with_suffix(target.suffix + ".txn.json"),
    )
    return target


def check_phase(
    spec: dict[str, Any], spec_path: Path, policy: dict[str, Any], phase: str, result: CheckResult
) -> Path | None:
    _root, target = output_manifest_path(spec, spec_path, policy, result)
    if not result.ok():
        return None
    manifest = load_output_manifest(target, result)
    if not result.ok():
        return target
    validate_manifest_header(spec, policy, manifest, result)
    validate_phase_records(spec, spec_path, policy, "generated", manifest, result)
    if phase == "release":
        validate_phase_records(spec, spec_path, policy, "release", manifest, result)
    return target


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Write or verify Spec-Freeze-bound downstream output evidence.")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--write-generated", action="store_true")
    actions.add_argument("--write-release", action="store_true")
    actions.add_argument("--check-generated", action="store_true")
    actions.add_argument("--check-release", action="store_true")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])

    result = CheckResult()
    target: Path | None = None
    action = next(
        name
        for name in ["write_generated", "write_release", "check_generated", "check_release"]
        if getattr(args, name)
    )
    phase = "release" if action.endswith("release") else "generated"
    try:
        spec = load_spec(args.spec)
        policy = load_policy()
        if not freeze_required(spec, policy):
            result.warning("Spec output binding is not required for this legacy/draft spec")
        else:
            check_frozen_spec(spec, args.spec, result, require=True)
            if result.ok():
                if action.startswith("write"):
                    target = write_phase(spec, args.spec, policy, phase, result)
                else:
                    target = check_phase(spec, args.spec, policy, phase, result)
    except Exception as error:
        result.issue(str(error))

    payload = {
        "check": "spec_output_binding",
        "action": action,
        "ok": result.ok(),
        "manifest": str(target) if target else None,
        "issues": result.issues,
        "warnings": result.warnings,
    }
    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if result.ok() else 1
    if target:
        print(f"spec_output_binding manifest: {target}")
    return print_result("spec_output_binding", result, False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
