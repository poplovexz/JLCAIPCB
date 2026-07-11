#!/usr/bin/env python3
"""Validate and transactionally bind selected parts to KiCad libraries and pin maps."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _part_lock import (  # noqa: E402
    check_part_lock,
    replace_files_transactionally,
    roundtrip_spec_bytes,
)
from _pcb_skill_checks import CheckResult, get_path, load_spec, print_result, sha256_file, string_value  # noqa: E402
from _sourcing_stage import (  # noqa: E402
    load_data_file,
    load_policy,
    mapping_value,
    project_root,
    resolved_path,
)


def prepare_binding(
    spec: dict,
    spec_path: Path,
    manifest_value: Path,
    result: CheckResult,
    as_of: str | None,
) -> tuple[dict, Path | None]:
    policy = load_policy(spec)
    lock_details = check_part_lock(spec, spec_path, result, force=True, as_of=as_of)
    if not result.ok():
        return spec, None
    root = project_root(spec, spec_path, policy, result)
    manifest_path = resolved_path(manifest_value, root)
    artifacts_root = resolved_path(get_path(spec, "project.artifacts_dir"), root)
    try:
        manifest_path.relative_to(artifacts_root)
    except ValueError:
        result.issue("library binding manifest must stay under project.artifacts_dir")
        return spec, manifest_path
    try:
        manifest = load_data_file(manifest_path)
    except Exception as error:
        result.issue(f"cannot load library binding manifest: {error}")
        return spec, manifest_path

    updated = copy.deepcopy(spec)
    components = {
        str(item.get("ref")): item
        for item in updated.get("components", [])
        if isinstance(updated.get("components"), list)
        and isinstance(item, dict)
        and string_value(item.get("ref"))
    }
    manifest_sha = sha256_file(manifest_path)
    symbol_field = str(get_path(policy, "downstream_binding.component_fields.symbol") or "symbol")
    footprint_field = str(get_path(policy, "downstream_binding.component_fields.footprint") or "footprint")
    kicad = updated.setdefault("kicad", {})
    symbol_libraries = kicad.setdefault("symbol_libraries", {})
    footprint_libraries = kicad.setdefault("footprint_libraries", {})
    for binding in manifest.get("bindings", []) if isinstance(manifest.get("bindings"), list) else []:
        if not isinstance(binding, dict) or not string_value(binding.get("ref")):
            continue
        ref = str(binding["ref"])
        component = components.get(ref)
        if component is None:
            result.issue(f"library binding references unknown component {ref}")
            continue
        component[symbol_field] = binding.get("symbol_id")
        component[footprint_field] = binding.get("footprint")
        component["library_binding"] = {
            "manifest_sha256": manifest_sha,
            "symbol_library": binding.get("symbol_library"),
            "symbol_id": binding.get("symbol_id"),
            "footprint_library": binding.get("footprint_library"),
            "footprint": binding.get("footprint"),
        }
        symbol_file = get_path(binding, "symbol_file_evidence.file")
        footprint_file = get_path(binding, "footprint_file_evidence.file")
        symbol_library = str(binding.get("symbol_library", ""))
        footprint_library = str(binding.get("footprint_library", ""))
        if string_value(symbol_file) and symbol_library:
            previous = symbol_libraries.get(symbol_library)
            if previous is not None and previous != symbol_file:
                result.issue(f"symbol library {symbol_library} maps to multiple files")
            symbol_libraries[symbol_library] = symbol_file
        if string_value(footprint_file) and footprint_library:
            footprint_root = str(Path(str(footprint_file)).parent)
            previous = footprint_libraries.get(footprint_library)
            if previous is not None and previous != footprint_root:
                result.issue(f"footprint library {footprint_library} maps to multiple directories")
            footprint_libraries[footprint_library] = footprint_root
        assembly = component.get("assembly")
        if isinstance(assembly, dict):
            token = get_path(binding, "package_verification.footprint_package_token")
            if string_value(token):
                assembly["footprint_package_token"] = token

    sourcing = updated.setdefault("sourcing", {})
    sourcing["downstream_binding"] = {
        "status": "ready",
        "part_lock_sha256": lock_details.get("part_lock_sha256"),
        "manifest": {"path": str(manifest_path), "sha256": manifest_sha},
    }
    verification = CheckResult()
    check_part_lock(
        updated,
        spec_path,
        verification,
        force=True,
        as_of=as_of,
        before_generation=True,
    )
    result.issues.extend(verification.issues)
    result.warnings.extend(verification.warnings)
    return updated, manifest_path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Bind a part lock to verified KiCad libraries and pin maps.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--as-of")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])
    result = CheckResult()
    manifest_path: Path | None = None
    try:
        updated, manifest_path = prepare_binding(
            load_spec(args.spec), args.spec, args.manifest, result, args.as_of
        )
        if result.ok() and args.apply:
            replace_files_transactionally(
                [(args.spec, roundtrip_spec_bytes(args.spec, updated))],
                args.spec.with_suffix(args.spec.suffix + ".binding-txn.json"),
            )
    except Exception as error:
        result.issue(str(error))
    payload = {
        "check": "library_binding_transaction",
        "ok": result.ok(),
        "applied": bool(args.apply and result.ok()),
        "manifest": str(manifest_path) if manifest_path else None,
        "issues": result.issues,
        "warnings": result.warnings,
    }
    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if result.ok() else 1
    code = print_result("library_binding_transaction", result)
    if result.ok():
        print("library binding applied" if args.apply else "library binding validated (dry run)")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
