#!/usr/bin/env python3
"""Atomically promote validated project-local KiCad library import candidates."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _package_binding_stage import (  # noqa: E402
    artifacts_root,
    ensure_artifact,
    load_data,
    load_policy,
    mapping,
    project_root,
    require_fields,
    resolve,
    sequence,
    strings,
)
from _part_lock import replace_files_transactionally  # noqa: E402
from _pcb_skill_checks import CheckResult, get_path, load_spec, print_result, sha256_file, string_value  # noqa: E402


def prepare(
    spec: dict[str, Any], spec_path: Path, transaction_path: Path, result: CheckResult
) -> tuple[list[tuple[Path, bytes]], Path | None, dict[str, Any]]:
    policy = load_policy()
    config = mapping(policy.get("library_import_transaction"))
    root = project_root(spec, spec_path)
    artifacts = artifacts_root(spec, root)
    transaction_path = transaction_path.resolve()
    ensure_artifact(transaction_path, artifacts, result, "library import transaction manifest")
    if not transaction_path.is_file():
        result.issue(f"library import transaction manifest does not exist: {transaction_path}")
        return [], None, {}
    transaction = load_data(transaction_path)
    require_fields(transaction, strings(config.get("required_fields")), result, "library_import_transaction")
    if transaction.get("schema_version") not in sequence(config.get("schema_versions")):
        result.issue("library_import_transaction.schema_version is unsupported")
    if transaction.get("project_name") != get_path(spec, "project.name"):
        result.issue("library_import_transaction.project_name does not match the Spec")

    source = mapping(transaction.get("source"))
    require_fields(source, strings(config.get("required_source_fields")), result, "library_import_transaction.source")
    source_path = resolve(root, source.get("file"))
    ensure_artifact(source_path, artifacts, result, "library_import_transaction.source")
    if not source_path.is_file() or source.get("sha256") != sha256_file(source_path):
        result.issue("library_import_transaction source snapshot is missing or stale")

    tool = mapping(transaction.get("tool"))
    require_fields(tool, strings(config.get("required_tool_fields")), result, "library_import_transaction.tool")
    candidate_directory = resolve(root, transaction.get("candidate_directory"))
    ensure_artifact(candidate_directory, artifacts, result, "library_import_transaction.candidate_directory")
    if not candidate_directory.is_dir():
        result.issue("library_import_transaction.candidate_directory does not exist")

    roles: dict[str, dict[str, Any]] = {}
    replacements: list[tuple[Path, bytes]] = []
    destinations: set[Path] = set()
    allowed_roles = set(strings(config.get("allowed_roles")))
    suffixes = mapping(config.get("role_suffixes"))
    for index, raw in enumerate(sequence(transaction.get("outputs"))):
        output = mapping(raw)
        label = f"library_import_transaction.outputs[{index}]"
        require_fields(output, strings(config.get("required_output_fields")), result, label)
        role = str(output.get("role", ""))
        if role not in allowed_roles:
            result.issue(f"{label}.role is unsupported")
            continue
        if role in roles:
            result.issue(f"{label}.role duplicates {role}")
        roles[role] = output
        candidate = resolve(root, output.get("candidate_file"))
        destination = resolve(root, output.get("destination_file"))
        try:
            candidate.relative_to(candidate_directory)
        except ValueError:
            result.issue(f"{label}.candidate_file must stay under candidate_directory")
        try:
            destination.relative_to(root)
        except ValueError:
            result.issue(f"{label}.destination_file must stay under project.root_dir")
        try:
            destination.relative_to(artifacts)
            result.issue(f"{label}.destination_file must be an active project library path, not artifacts")
        except ValueError:
            pass
        if destination in destinations:
            result.issue(f"{label}.destination_file is duplicated")
        destinations.add(destination)
        allowed_suffixes = strings(suffixes.get(role))
        if candidate.suffix.lower() not in allowed_suffixes or destination.suffix.lower() not in allowed_suffixes:
            result.issue(f"{label} file suffix does not match role {role}")
        if not candidate.is_file() or output.get("sha256") != sha256_file(candidate):
            result.issue(f"{label}.candidate_file is missing or stale")
            continue
        replacements.append((destination, candidate.read_bytes()))

    missing_roles = set(strings(config.get("required_roles"))) - set(roles)
    if missing_roles:
        result.issue(f"library_import_transaction missing required output roles: {', '.join(sorted(missing_roles))}")
    provenance_path = resolve(root, transaction.get("provenance_output"))
    journal_path = resolve(root, transaction.get("journal"))
    ensure_artifact(provenance_path, artifacts, result, "library_import_transaction.provenance_output")
    ensure_artifact(journal_path, artifacts, result, "library_import_transaction.journal")
    provenance = {
        "schema_version": 1,
        "status": "verified",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_name": get_path(spec, "project.name"),
        "executor_sha256": sha256_file(Path(__file__).resolve()),
        "tool": tool,
        "source": source,
        "outputs": {role: output.get("sha256") for role, output in roles.items()},
        "transaction": {
            "candidate_directory": str(candidate_directory),
            "manifest": str(transaction_path),
            "manifest_sha256": sha256_file(transaction_path),
            "promoted": True,
        },
    }
    replacements.append((provenance_path, (json.dumps(provenance, indent=2, sort_keys=True) + "\n").encode("utf-8")))
    return replacements, journal_path, provenance


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Dry-run or atomically promote KiCad import candidates.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("transaction", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])
    result = CheckResult()
    applied = False
    provenance: dict[str, Any] = {}
    try:
        spec = load_spec(args.spec)
        replacements, journal, provenance = prepare(spec, args.spec, args.transaction, result)
        if result.ok() and args.apply and journal is not None:
            replace_files_transactionally(replacements, journal)
            for destination, data in replacements:
                if not destination.is_file() or destination.read_bytes() != data:
                    result.issue(f"promoted library output verification failed: {destination}")
            applied = result.ok()
    except Exception as error:
        result.issue(str(error))
    payload = {
        "check": "library_import_transaction",
        "ok": result.ok(),
        "applied": applied,
        "provenance": provenance,
        "issues": result.issues,
        "warnings": result.warnings,
    }
    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if result.ok() else 1
    code = print_result("library_import_transaction", result, False)
    if result.ok():
        print("library import promoted" if applied else "library import transaction validated (dry run)")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
