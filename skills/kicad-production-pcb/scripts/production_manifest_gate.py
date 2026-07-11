#!/usr/bin/env python3
"""Write or verify one exact-inventory local production manifest."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _local_validation import check_evidence as check_local_evidence  # noqa: E402
from _package_binding_stage import artifacts_root, project_root, resolve, sequence, strings  # noqa: E402
from _part_lock import replace_files_transactionally  # noqa: E402
from _pcb_skill_checks import CheckResult, get_path, load_spec, print_result, sha256_file  # noqa: E402


SKILL_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = SKILL_ROOT / "assets" / "production-manifest-policy.yaml"


def load_policy() -> dict[str, Any]:
    data = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("production manifest policy must be a mapping")
    return data


def normalized(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def required(spec: dict[str, Any], force: bool = False) -> bool:
    return force or normalized(get_path(spec, "project.stage")) in {normalized(item) for item in strings(load_policy().get("required_project_stages"))}


def display(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path.resolve())


def record(path: Path, root: Path, role: str) -> dict[str, Any]:
    return {"role": role, "path": display(path, root), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}


def executor_dependencies(policy: dict[str, Any]) -> list[dict[str, str]]:
    return [{"path": item, "sha256": sha256_file(SKILL_ROOT / item)} for item in strings(policy.get("executor_dependencies"))]


def manifest_path(spec: dict[str, Any], spec_path: Path) -> Path:
    policy = load_policy()
    root = project_root(spec, spec_path)
    return artifacts_root(spec, root) / str(policy["artifact_subdir"]) / str(get_path(spec, "project.name")) / str(policy["manifest_filename"])


def collect(spec: dict[str, Any], spec_path: Path, result: CheckResult) -> list[dict[str, Any]]:
    root = project_root(spec, spec_path)
    artifacts = artifacts_root(spec, root)
    name = str(get_path(spec, "project.name"))
    project_dir = resolve(root, get_path(spec, "project.output_dir"))
    files: list[tuple[Path, str]] = [
        (spec_path.resolve(), "spec"),
        (project_dir / f"{name}.kicad_sch", "schematic"),
        (project_dir / f"{name}.kicad_pcb", "pcb"),
        (artifacts / "local-validation" / name / "local-validation-manifest.yaml", "local_validation_manifest"),
    ]
    for filename in strings(load_policy().get("required_gate_reports")):
        files.append((artifacts / "local-validation" / name / filename, Path(filename).stem.replace("-", "_")))
    fab = artifacts / "fab" / name
    if not fab.is_dir():
        result.issue(f"production manifest fabrication directory is missing: {fab}")
    else:
        files.extend((path, "fabrication") for path in sorted(fab.rglob("*")) if path.is_file())
    package = get_path(spec, "manufacturing.jlcpcb.package")
    release = get_path(spec, "manufacturing.jlcpcb.release")
    if not isinstance(package, dict) or not isinstance(release, dict):
        result.issue("production manifest requires manufacturing.jlcpcb.package and release")
    else:
        package_dir = resolve(root, package.get("output_dir"))
        release_dir = resolve(root, release.get("output_dir"))
        for directory, role in [(package_dir, "jlcpcb_package"), (release_dir, "jlcpcb_release")]:
            if not directory.is_dir():
                result.issue(f"production manifest directory is missing: {directory}")
            else:
                files.extend((path, role) for path in sorted(directory.rglob("*")) if path.is_file())
        for path, label in [(package_dir / str(package.get("manifest", "")), "package"), (release_dir / str(release.get("manifest", "")), "release")]:
            if not path.is_file():
                result.issue(f"production manifest requires {label} manifest: {path}")
    records = []
    seen: set[str] = set()
    for path, role in files:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        if not path.is_file():
            result.issue(f"production manifest input is missing: {path}")
            continue
        records.append(record(path, root, role))
    return sorted(records, key=lambda item: (item["role"], item["path"]))


def validate_gate_reports(records: list[dict[str, Any]], root: Path, result: CheckResult) -> None:
    by_role = {str(item["role"]): item for item in records}
    expected_inputs = {
        role: {"path": entry["path"], "sha256": entry["sha256"]}
        for role, entry in by_role.items() if role in {"spec", "schematic", "pcb"}
    }
    for role in ["final_gate", "jlcpcb_gate"]:
        entry = by_role.get(role)
        if not entry:
            result.issue(f"production manifest gate report role is missing: {role}")
            continue
        path = resolve(root, entry["path"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("ok") is not True or sequence(payload.get("issues")):
            result.issue(f"production manifest gate report is not passing: {role}")
        inputs = payload.get("inputs")
        if not isinstance(inputs, dict):
            result.issue(f"production manifest gate report has no input hashes: {role}")
            continue
        for input_role, expected in expected_inputs.items():
            actual = inputs.get(input_role)
            if not isinstance(actual, dict) or actual.get("sha256") != expected["sha256"]:
                result.issue(f"production manifest gate report has stale {input_role} hash: {role}")


def write_manifest(spec: dict[str, Any], spec_path: Path, result: CheckResult, force: bool = False) -> dict[str, Any]:
    active = required(spec, force)
    details: dict[str, Any] = {"required": active}
    if not active:
        result.warning("Production manifest is not required for this project stage")
        return details
    local_result = CheckResult()
    check_local_evidence(spec, spec_path, local_result, True)
    result.issues.extend(f"local validation prerequisite: {item}" for item in local_result.issues)
    result.warnings.extend(local_result.warnings)
    records = collect(spec, spec_path, result)
    root = project_root(spec, spec_path)
    validate_gate_reports(records, root, result)
    policy = load_policy()
    payload = {
        "schema_version": policy["manifest_schema_version"], "status": "passed" if result.ok() else "failed",
        "project_name": get_path(spec, "project.name"), "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy_sha256": sha256_file(POLICY_PATH), "executor_sha256": sha256_file(Path(__file__).resolve()),
        "executor_dependencies": executor_dependencies(policy), "files": records,
        "issues": list(result.issues), "warnings": list(result.warnings),
    }
    target = manifest_path(spec, spec_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    replace_files_transactionally([(target, yaml.safe_dump(payload, sort_keys=False).encode())], target.with_suffix(".txn.json"))
    details.update({"manifest": str(target), "status": payload["status"], "file_count": len(records)})
    return details


def check_manifest(spec: dict[str, Any], spec_path: Path, result: CheckResult, force: bool = False) -> dict[str, Any]:
    active = required(spec, force)
    details: dict[str, Any] = {"required": active}
    if not active:
        result.warning("Production manifest is not required for this project stage")
        return details
    target = manifest_path(spec, spec_path)
    details["manifest"] = str(target)
    if not target.is_file():
        result.issue(f"production manifest is missing: {target}")
        return details
    payload = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    policy = load_policy()
    if payload.get("schema_version") != policy["manifest_schema_version"] or payload.get("status") != "passed":
        result.issue("production manifest status/schema is not passing")
    if payload.get("project_name") != get_path(spec, "project.name") or payload.get("policy_sha256") != sha256_file(POLICY_PATH) or payload.get("executor_sha256") != sha256_file(Path(__file__).resolve()):
        result.issue("production manifest project/policy/executor binding is stale")
    if sequence(payload.get("executor_dependencies")) != executor_dependencies(policy):
        result.issue("production manifest executor dependencies changed")
    expected = collect(spec, spec_path, result)
    if sequence(payload.get("files")) != expected:
        result.issue("production manifest exact file inventory is stale")
    validate_gate_reports(expected, project_root(spec, spec_path), result)
    details.update({"status": payload.get("status"), "file_count": len(expected)})
    return details


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Write or verify exact local production file evidence.")
    parser.add_argument("spec", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    parser.add_argument("--require", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])
    result = CheckResult()
    details = {}
    try:
        spec = load_spec(args.spec)
        details = write_manifest(spec, args.spec, result, args.require) if args.write else check_manifest(spec, args.spec, result, args.require)
    except Exception as error:
        result.issue(str(error))
    payload = {"check": "production_manifest_gate", "ok": result.ok(), "issues": result.issues, "warnings": result.warnings, "details": details}
    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if result.ok() else 1
    return print_result("production_manifest_gate", result, False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
