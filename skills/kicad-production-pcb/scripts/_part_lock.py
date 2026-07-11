#!/usr/bin/env python3
"""Build, apply, and verify immutable sourcing part locks."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from _kicad_sexpr import footprint_pad_numbers, symbol_pin_numbers

from _pcb_skill_checks import CheckResult, get_path, sha256_file, string_value
from _sourcing_stage import (
    canonical_sha256,
    configured_artifact_path,
    effective_now,
    evaluate_candidate_manifest,
    load_data_file,
    load_policy,
    mapping_value,
    normalized,
    policy_strings,
    project_root,
    ranking_report,
    resolved_path,
    string_list,
    timestamp_issue,
)


def yaml_bytes(data: dict[str, Any]) -> bytes:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True).encode("utf-8")


def roundtrip_spec_bytes(path: Path, data: dict[str, Any]) -> bytes:
    try:
        from ruamel.yaml import YAML
    except ImportError as error:
        raise RuntimeError("ruamel.yaml is required for lossless transactional spec updates") from error

    formatter = YAML(typ="rt")
    formatter.preserve_quotes = True
    formatter.width = 4096
    with path.open("r", encoding="utf-8") as handle:
        current = formatter.load(handle)

    def synchronize(target: Any, source: Any) -> Any:
        if isinstance(target, dict) and isinstance(source, dict):
            for key in list(target):
                if key not in source:
                    del target[key]
            for key, value in source.items():
                target[key] = synchronize(target[key], value) if key in target else copy.deepcopy(value)
            return target
        if isinstance(target, list) and isinstance(source, list):
            shared = min(len(target), len(source))
            for index in range(shared):
                target[index] = synchronize(target[index], source[index])
            if len(target) > len(source):
                del target[len(source):]
            elif len(source) > len(target):
                target.extend(copy.deepcopy(source[len(target):]))
            return target
        return copy.deepcopy(source)

    synchronized = synchronize(current, data)
    output = io.StringIO()
    formatter.dump(synchronized, output)
    return output.getvalue().encode("utf-8")


def bytes_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def candidate_map(manifest: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    output: dict[tuple[str, str], dict[str, Any]] = {}
    for batch in manifest.get("batches", []) if isinstance(manifest.get("batches"), list) else []:
        if not isinstance(batch, dict):
            continue
        requirement_id = str(batch.get("requirement_id", "")).strip()
        for candidate in batch.get("candidates", []) if isinstance(batch.get("candidates"), list) else []:
            if isinstance(candidate, dict) and string_value(candidate.get("id")):
                output[(requirement_id, str(candidate["id"]).strip())] = candidate
    return output


def requirement_map(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    sourcing = mapping_value(spec.get("sourcing"))
    requirements = sourcing.get("requirements")
    requirement_items = requirements if isinstance(requirements, list) else []
    return {
        str(item["id"]).strip(): item
        for item in requirement_items
        if isinstance(item, dict) and string_value(item.get("id"))
    }


def providers_map(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    providers = get_path(spec, "sourcing.context.providers")
    provider_items = providers if isinstance(providers, list) else []
    return {
        str(item["id"]).strip(): item
        for item in provider_items
        if isinstance(item, dict) and string_value(item.get("id"))
    }


def evidence_url(candidate: dict[str, Any], kind: str) -> str | None:
    wanted = normalized(kind)
    for evidence in candidate.get("evidence", []) if isinstance(candidate.get("evidence"), list) else []:
        if isinstance(evidence, dict) and normalized(evidence.get("kind")) == wanted and string_value(evidence.get("url")):
            return str(evidence["url"])
    return None


def evidence_digests(candidate: dict[str, Any]) -> dict[str, str]:
    output: dict[str, str] = {}
    for evidence in candidate.get("evidence", []) if isinstance(candidate.get("evidence"), list) else []:
        if isinstance(evidence, dict) and string_value(evidence.get("id")) and string_value(evidence.get("sha256")):
            output[str(evidence["id"])] = str(evidence["sha256"])
    return output


def evidence_records(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": evidence.get("id"),
            "kind": normalized(evidence.get("kind")),
            "sha256": evidence.get("sha256"),
            "url": evidence.get("url"),
        }
        for evidence in candidate.get("evidence", [])
        if isinstance(candidate.get("evidence"), list)
        and isinstance(evidence, dict)
        and string_value(evidence.get("id"))
    ]


def selection_record(
    requirement: dict[str, Any],
    candidate: dict[str, Any],
    evaluation: dict[str, Any],
    qualified_alternates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    supplier = mapping_value(candidate.get("supplier"))
    package = mapping_value(candidate.get("package"))
    assembly = mapping_value(candidate.get("assembly"))
    return {
        "requirement_id": requirement.get("id"),
        "candidate_id": candidate.get("id"),
        "component_refs": copy.deepcopy(requirement.get("component_refs", [])),
        "quantity_per_board": requirement.get("quantity_per_board"),
        "disposition": requirement.get("disposition"),
        "manufacturer": candidate.get("manufacturer"),
        "mpn": candidate.get("mpn"),
        "supplier": {
            "provider_id": supplier.get("provider_id"),
            "part_number": supplier.get("part_number"),
            "source_url": supplier.get("source_url"),
        },
        "package": {
            "name": package.get("name"),
            "footprint_candidate": package.get("footprint"),
            "pin_count": package.get("pin_count"),
            "body_length_mm": package.get("body_length_mm"),
            "body_width_mm": package.get("body_width_mm"),
            "confidence": package.get("confidence"),
        },
        "inventory": copy.deepcopy(candidate.get("inventory", {})),
        "pricing": copy.deepcopy(candidate.get("pricing", {})),
        "lifecycle": copy.deepcopy(candidate.get("lifecycle", {})),
        "assembly": {
            "provider_id": assembly.get("provider_id"),
            "supported": assembly.get("supported"),
            "in_stock": assembly.get("in_stock"),
            "library_class": assembly.get("library_class"),
            "observed_at": assembly.get("observed_at"),
            "evidence_id": assembly.get("evidence_id"),
        } if normalized(requirement.get("disposition")) == "pcba" else {},
        "architecture_trace": {
            "block_ids": copy.deepcopy(requirement.get("block_ids", [])),
            "constraint_ids": copy.deepcopy(requirement.get("constraint_ids", [])),
        },
        "datasheet_url": evidence_url(candidate, "manufacturer_datasheet"),
        "evidence_sha256": evidence_digests(candidate),
        "evidence_records": evidence_records(candidate),
        "score": evaluation.get("score"),
        "order_quantity": evaluation.get("order_quantity"),
        "needed_quantity": evaluation.get("needed_quantity"),
        "moq_waste_quantity": evaluation.get("moq_waste_quantity"),
        "unit_price": evaluation.get("unit_price"),
        "extended_cost": evaluation.get("extended_cost"),
        "qualified_alternates": copy.deepcopy(qualified_alternates or []),
    }


def alternate_record(candidate: dict[str, Any], evaluation: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": evaluation.get("candidate_id"),
        "score": evaluation.get("score"),
        "manufacturer": candidate.get("manufacturer"),
        "mpn": candidate.get("mpn"),
        "package": {
            "name": get_path(candidate, "package.name"),
            "pin_count": get_path(candidate, "package.pin_count"),
            "body_length_mm": get_path(candidate, "package.body_length_mm"),
            "body_width_mm": get_path(candidate, "package.body_width_mm"),
            "footprint_candidate": get_path(candidate, "package.footprint"),
        },
        "capabilities_sha256": canonical_sha256(mapping_value(candidate.get("capabilities"))),
        "automatic_substitution_allowed": False,
    }


def build_part_lock(
    spec: dict[str, Any], evaluation: dict[str, Any], ranking_sha256: str
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    candidate_path = Path(str(evaluation["candidate_manifest_resolved"]))
    manifest = load_data_file(candidate_path)
    candidates = candidate_map(manifest)
    requirements = requirement_map(spec)
    selections: list[dict[str, Any]] = []
    selected_candidates: dict[str, dict[str, Any]] = {}
    for requirement_result in evaluation.get("requirements", []):
        requirement_id = str(requirement_result.get("requirement_id", ""))
        candidate_id = str(requirement_result.get("selected_candidate_id", ""))
        requirement = requirements[requirement_id]
        candidate = candidates[(requirement_id, candidate_id)]
        evaluation_by_id = {
            str(item.get("candidate_id")): item
            for item in requirement_result.get("candidates", []) if isinstance(item, dict)
        }
        alternates = [
            alternate_record(candidates[(requirement_id, str(item.get("candidate_id")))], item)
            for item in requirement_result.get("candidates", [])
            if isinstance(item, dict)
            and item.get("qualified") is True
            and str(item.get("candidate_id")) != candidate_id
        ]
        selections.append(
            selection_record(requirement, candidate, evaluation_by_id[candidate_id], alternates)
        )
        selected_candidates[requirement_id] = candidate
    ranking_path = get_path(spec, "sourcing.artifacts.ranking")
    lock = {
        "schema_version": 1,
        "project_name": get_path(spec, "project.name"),
        "locked_at": evaluation.get("evaluated_at"),
        "architecture_sha256": evaluation.get("architecture_sha256"),
        "sourcing_context_sha256": evaluation.get("sourcing_context_sha256"),
        "candidate_manifest": {
            "path": evaluation.get("candidate_manifest"),
            "sha256": evaluation.get("candidate_manifest_sha256"),
        },
        "ranking": {
            "path": ranking_path,
            "sha256": ranking_sha256,
        },
        "selected_set": copy.deepcopy(evaluation.get("selected_set", {})),
        "selections": selections,
    }
    return lock, selected_candidates


def apply_selections_to_spec(
    spec: dict[str, Any], lock: dict[str, Any], selected_candidates: dict[str, dict[str, Any]], policy: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    updated = copy.deepcopy(spec)
    components = {
        str(item.get("ref")): item
        for item in updated.get("components", []) if isinstance(updated.get("components"), list)
        if isinstance(item, dict) and string_value(item.get("ref"))
    }
    providers = providers_map(updated)
    previous_selection = {
        str(item.get("requirement_id")): str(item.get("candidate_id"))
        for item in get_path(updated, "sourcing.part_lock.selections") or []
        if isinstance(item, dict)
    }
    current_selection = {
        str(item.get("requirement_id")): str(item.get("candidate_id"))
        for item in lock.get("selections", []) if isinstance(item, dict)
    }
    selection_changed = bool(previous_selection) and previous_selection != current_selection
    allowed_part_fields = policy_strings(policy, "allowed_component_part_fields")
    for selection in lock.get("selections", []):
        requirement_id = str(selection["requirement_id"])
        candidate = selected_candidates[requirement_id]
        supplier = mapping_value(candidate.get("supplier"))
        provider = providers[str(supplier.get("provider_id"))]
        part_field = str(
            provider.get("component_part_field")
            or policy.get("default_component_part_field")
        )
        for ref in selection.get("component_refs", []):
            component = components[str(ref)]
            for field in allowed_part_fields:
                component.pop(field, None)
            component["manufacturer"] = selection.get("manufacturer")
            component["mpn"] = selection.get("mpn")
            component[part_field] = get_path(selection, "supplier.part_number")
            component["source_url"] = get_path(selection, "supplier.source_url")
            component["datasheet"] = selection.get("datasheet_url")
            component["footprint_candidate"] = get_path(selection, "package.footprint_candidate")
            component["package"] = get_path(selection, "package.name")
            component["architecture_trace"] = copy.deepcopy(selection.get("architecture_trace"))
            component["sourcing_lock"] = {
                "requirement_id": requirement_id,
                "candidate_id": selection.get("candidate_id"),
                "candidate_manifest_sha256": get_path(lock, "candidate_manifest.sha256"),
            }
            if normalized(selection.get("disposition")) == "pcba":
                assembly = component.setdefault("assembly", {})
                if not isinstance(assembly, dict):
                    assembly = {}
                    component["assembly"] = assembly
                for field in allowed_part_fields:
                    assembly.pop(field, None)
                assembly[part_field] = get_path(selection, "supplier.part_number")
                assembly["source_url"] = get_path(selection, "supplier.source_url")
                assembly["manufacturer"] = selection.get("manufacturer")
                assembly["mpn"] = selection.get("mpn")
                assembly["lcsc_packaging"] = get_path(selection, "package.name")
                assembly["provider_id"] = get_path(selection, "assembly.provider_id")
                assembly["assembly_available"] = get_path(selection, "assembly.supported")
                assembly["library_class"] = get_path(selection, "assembly.library_class")

    lock_bytes = yaml_bytes(lock)
    sourcing = updated.setdefault("sourcing", {})
    sourcing["part_lock"] = {
        "status": "locked",
        "path": get_path(updated, "sourcing.artifacts.part_lock"),
        "sha256": bytes_sha256(lock_bytes),
        "architecture_sha256": lock.get("architecture_sha256"),
        "sourcing_context_sha256": lock.get("sourcing_context_sha256"),
        "candidate_manifest_sha256": get_path(lock, "candidate_manifest.sha256"),
        "locked_at": lock.get("locked_at"),
        "selections": [
            {"requirement_id": item.get("requirement_id"), "candidate_id": item.get("candidate_id")}
            for item in lock.get("selections", [])
        ],
    }
    downstream = sourcing.setdefault("downstream_binding", {})
    if not isinstance(downstream, dict):
        downstream = {}
        sourcing["downstream_binding"] = downstream
    if selection_changed:
        downstream.clear()
        downstream.update({"status": "invalidated", "reason": "part_lock_changed", "part_lock_sha256": bytes_sha256(lock_bytes)})
    elif not downstream:
        downstream.update({"status": "pending", "part_lock_sha256": bytes_sha256(lock_bytes)})
    else:
        downstream["part_lock_sha256"] = bytes_sha256(lock_bytes)
    return updated, selection_changed


def fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    fsync_directory(path.parent)


def recover_transaction(journal_path: Path) -> None:
    if not journal_path.is_file():
        return
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    entries = journal.get("entries", [])
    for entry in reversed(entries if isinstance(entries, list) else []):
        if not isinstance(entry, dict):
            continue
        target = Path(str(entry.get("target", "")))
        backup = Path(str(entry.get("backup", "")))
        existed = entry.get("existed") is True
        if existed:
            if not backup.is_file():
                raise RuntimeError(f"cannot recover sourcing transaction; backup is missing: {backup}")
            os.replace(backup, target)
            fsync_directory(target.parent)
        elif not existed:
            target.unlink(missing_ok=True)
            fsync_directory(target.parent)
        Path(str(entry.get("temporary", ""))).unlink(missing_ok=True)
        backup.unlink(missing_ok=True)
    journal_path.unlink(missing_ok=True)
    fsync_directory(journal_path.parent)


def replace_files_transactionally(files: list[tuple[Path, bytes]], journal_path: Path) -> None:
    recover_transaction(journal_path)
    transaction_id = uuid4().hex
    entries: list[dict[str, Any]] = []
    for path, data in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{transaction_id}.tmp")
        backup = path.with_name(f".{path.name}.{transaction_id}.bak")
        existed = path.is_file()
        if existed:
            durable_write(backup, path.read_bytes())
        durable_write(temporary, data)
        entries.append(
            {
                "target": str(path),
                "temporary": str(temporary),
                "backup": str(backup),
                "existed": existed,
            }
        )
    journal_temporary = journal_path.with_suffix(journal_path.suffix + ".prepare")
    durable_write(
        journal_temporary,
        json.dumps({"transaction_id": transaction_id, "entries": entries}, sort_keys=True).encode("utf-8"),
    )
    os.replace(journal_temporary, journal_path)
    fsync_directory(journal_path.parent)
    try:
        for entry in entries:
            target = Path(entry["target"])
            os.replace(Path(entry["temporary"]), target)
            fsync_directory(target.parent)
    except BaseException:
        recover_transaction(journal_path)
        raise
    for entry in entries:
        Path(entry["backup"]).unlink(missing_ok=True)
        Path(entry["temporary"]).unlink(missing_ok=True)
    journal_path.unlink(missing_ok=True)
    fsync_directory(journal_path.parent)


def prepare_part_lock_transaction(
    spec: dict[str, Any],
    spec_path: Path,
    result: CheckResult,
    as_of: str | None = None,
) -> dict[str, Any]:
    policy = load_policy(spec)
    evaluation = evaluate_candidate_manifest(spec, spec_path, result, policy=policy, force=True, as_of=as_of)
    if not result.ok():
        return {"evaluation": evaluation}
    ranking = ranking_report(spec, evaluation)
    ranking_data = yaml_bytes(ranking)
    lock, selected_candidates = build_part_lock(spec, evaluation, bytes_sha256(ranking_data))
    lock_data = yaml_bytes(lock)
    updated_spec, selection_changed = apply_selections_to_spec(spec, lock, selected_candidates, policy)
    expected_lock_sha = bytes_sha256(lock_data)
    if get_path(updated_spec, "sourcing.part_lock.sha256") != expected_lock_sha:
        result.issue("internal part-lock digest mismatch")
    root = project_root(updated_spec, spec_path, policy, result)
    ranking_path = configured_artifact_path(updated_spec, policy, "ranking_path_field", result, root=root)
    lock_path = configured_artifact_path(updated_spec, policy, "part_lock_path_field", result, root=root)
    return {
        "evaluation": evaluation,
        "ranking": ranking,
        "ranking_data": ranking_data,
        "ranking_path": ranking_path,
        "lock": lock,
        "lock_data": lock_data,
        "lock_path": lock_path,
        "updated_spec": updated_spec,
        "selection_changed": selection_changed,
    }


def apply_part_lock_transaction(spec_path: Path, transaction: dict[str, Any]) -> None:
    ranking_path = transaction["ranking_path"]
    lock_path = transaction["lock_path"]
    if not isinstance(ranking_path, Path) or not isinstance(lock_path, Path):
        raise ValueError("part-lock transaction is missing output paths")
    replace_files_transactionally(
        [
            (ranking_path, transaction["ranking_data"]),
            (lock_path, transaction["lock_data"]),
            (spec_path, roundtrip_spec_bytes(spec_path, transaction["updated_spec"])),
        ],
        spec_path.with_suffix(spec_path.suffix + ".sourcing-txn.json"),
    )


def compare_component_to_selection(
    component: dict[str, Any], selection: dict[str, Any], providers: dict[str, dict[str, Any]], policy: dict[str, Any]
) -> list[str]:
    failures: list[str] = []
    supplier = mapping_value(selection.get("supplier"))
    provider = providers.get(str(supplier.get("provider_id")), {})
    part_field = str(
        provider.get("component_part_field")
        or policy.get("default_component_part_field")
    )
    expected_fields = {
        "manufacturer": selection.get("manufacturer"),
        "mpn": selection.get("mpn"),
        part_field: supplier.get("part_number"),
        "source_url": supplier.get("source_url"),
        "datasheet": selection.get("datasheet_url"),
        "footprint_candidate": get_path(selection, "package.footprint_candidate"),
        "package": get_path(selection, "package.name"),
    }
    ref = str(component.get("ref", "<component>"))
    for field, expected in expected_fields.items():
        if component.get(field) != expected:
            failures.append(f"{ref}.{field} does not match part lock")
    if mapping_value(component.get("architecture_trace")) != mapping_value(selection.get("architecture_trace")):
        failures.append(f"{ref}.architecture_trace does not match part lock")
    sourcing_lock = mapping_value(component.get("sourcing_lock"))
    if sourcing_lock.get("requirement_id") != selection.get("requirement_id"):
        failures.append(f"{ref}.sourcing_lock.requirement_id does not match part lock")
    if sourcing_lock.get("candidate_id") != selection.get("candidate_id"):
        failures.append(f"{ref}.sourcing_lock.candidate_id does not match part lock")
    if normalized(selection.get("disposition")) == "pcba":
        assembly = mapping_value(component.get("assembly"))
        assembly_expected = {
            part_field: supplier.get("part_number"),
            "source_url": supplier.get("source_url"),
            "manufacturer": selection.get("manufacturer"),
            "mpn": selection.get("mpn"),
            "lcsc_packaging": get_path(selection, "package.name"),
        }
        for field, expected in assembly_expected.items():
            if assembly.get(field) != expected:
                failures.append(f"{ref}.assembly.{field} does not match part lock")
        if assembly.get("assembly_available") is not True:
            failures.append(f"{ref}.assembly.assembly_available must be true")
        if assembly.get("provider_id") != get_path(selection, "assembly.provider_id"):
            failures.append(f"{ref}.assembly.provider_id does not match part lock")
    return failures


def check_downstream_manifest(
    spec: dict[str, Any],
    lock: dict[str, Any],
    lock_sha: str,
    downstream: dict[str, Any],
    policy: dict[str, Any],
    result: CheckResult,
    root: Path,
    now: Any,
) -> dict[str, Any]:
    metadata = mapping_value(downstream.get("manifest"))
    path_value = metadata.get("path")
    if not string_value(path_value):
        result.issue("sourcing.downstream_binding.manifest.path is required before KiCad generation")
        return {}
    manifest_path = resolved_path(path_value, root)
    artifacts_root = resolved_path(get_path(spec, "project.artifacts_dir"), root)
    try:
        manifest_path.relative_to(artifacts_root)
    except ValueError:
        result.issue("downstream binding manifest must stay under project.artifacts_dir")
    if not manifest_path.is_file():
        result.issue(f"downstream binding manifest does not exist: {manifest_path}")
        return {"path": str(manifest_path)}
    manifest_sha = sha256_file(manifest_path)
    if metadata.get("sha256") != manifest_sha:
        result.issue("sourcing.downstream_binding.manifest.sha256 is stale")
    manifest = load_data_file(manifest_path)
    for field in policy_strings(policy, "downstream_binding.required_manifest_fields"):
        if field not in manifest or manifest.get(field) is None:
            result.issue(f"downstream binding manifest missing field: {field}")
    supported_versions = {
        int(value) for value in get_path(policy, "downstream_binding.schema_versions") or [] if isinstance(value, int)
    }
    if manifest.get("schema_version") not in supported_versions:
        result.issue("downstream binding manifest schema_version is unsupported")
    if manifest.get("project_name") != get_path(spec, "project.name"):
        result.issue("downstream binding manifest project_name does not match the spec")
    if manifest.get("part_lock_sha256") != lock_sha:
        result.issue("downstream binding manifest part_lock_sha256 is stale")
    max_age = float(get_path(spec, "sourcing.context.lock_max_age_hours") or 0)
    issue = timestamp_issue(manifest.get("generated_at"), now, max_age, "downstream binding generated_at")
    if issue:
        result.issue(issue)

    selections_by_ref: dict[str, dict[str, Any]] = {}
    for selection in lock.get("selections", []) if isinstance(lock.get("selections"), list) else []:
        if not isinstance(selection, dict):
            continue
        for ref in selection.get("component_refs", []) if isinstance(selection.get("component_refs"), list) else []:
            selections_by_ref[str(ref)] = selection
    bindings = {
        str(item.get("ref")): item
        for item in manifest.get("bindings", []) if isinstance(manifest.get("bindings"), list)
        if isinstance(item, dict) and string_value(item.get("ref"))
    }
    if set(bindings) != set(selections_by_ref):
        result.issue("downstream binding manifest refs do not exactly cover the part lock")
    components = {
        str(item.get("ref")): item
        for item in spec.get("components", []) if isinstance(spec.get("components"), list)
        if isinstance(item, dict) and string_value(item.get("ref"))
    }
    ready_statuses = {
        normalized(value) for value in policy_strings(policy, "downstream_binding.ready_binding_statuses")
    }

    def verify_library_file(
        prefix: str, evidence: dict[str, Any], kind: str
    ) -> tuple[Path | None, str | None]:
        for field in policy_strings(policy, "downstream_binding.library_file_evidence_required_fields"):
            if not string_value(evidence.get(field)):
                result.issue(f"{prefix} {kind}_file_evidence.{field} is required")
        file_value = evidence.get("file")
        if not string_value(file_value):
            return None, None
        path = resolved_path(file_value, root)
        try:
            path.relative_to(root)
        except ValueError:
            result.issue(f"{prefix} {kind} library file must stay under project.root_dir")
        if not path.is_file():
            result.issue(f"{prefix} {kind} library file does not exist")
            return path, None
        digest = sha256_file(path)
        if evidence.get("sha256") != digest:
            result.issue(f"{prefix} {kind} library file sha256 is stale")
        rule = mapping_value(get_path(policy, f"downstream_binding.library_file_rules.{kind}"))
        suffix = str(rule.get("suffix", ""))
        if suffix and path.suffix != suffix:
            result.issue(f"{prefix} {kind} library file must use {suffix}")
        signature = str(rule.get("signature", ""))
        if signature:
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                result.issue(f"{prefix} {kind} library file must be UTF-8 text")
            else:
                if signature not in content:
                    result.issue(f"{prefix} {kind} library file is missing its KiCad format signature")
        return path, digest

    for ref, binding in bindings.items():
        prefix = f"downstream binding {ref}"
        for field in policy_strings(policy, "downstream_binding.required_binding_fields"):
            if field not in binding or binding.get(field) is None:
                result.issue(f"{prefix} missing field: {field}")
        selection = selections_by_ref.get(ref)
        if selection is None:
            continue
        if binding.get("requirement_id") != selection.get("requirement_id"):
            result.issue(f"{prefix} requirement_id does not match part lock")
        if binding.get("candidate_id") != selection.get("candidate_id"):
            result.issue(f"{prefix} candidate_id does not match part lock")
        if binding.get("source_footprint_candidate") != get_path(selection, "package.footprint_candidate"):
            result.issue(f"{prefix} source_footprint_candidate does not match part lock")
        if normalized(binding.get("status")) not in ready_statuses:
            result.issue(f"{prefix} status is not verified")
        symbol_file_path, symbol_file_sha = verify_library_file(
            prefix, mapping_value(binding.get("symbol_file_evidence")), "symbol"
        )
        footprint_file_path, footprint_file_sha = verify_library_file(
            prefix, mapping_value(binding.get("footprint_file_evidence")), "footprint"
        )
        symbol_library = str(binding.get("symbol_library", ""))
        symbol_id = str(binding.get("symbol_id", ""))
        footprint_library = str(binding.get("footprint_library", ""))
        footprint_id = str(binding.get("footprint", ""))
        if not symbol_id.startswith(symbol_library + ":"):
            result.issue(f"{prefix} symbol_id does not belong to symbol_library")
        if not footprint_id.startswith(footprint_library + ":"):
            result.issue(f"{prefix} footprint does not belong to footprint_library")
        if symbol_file_path is not None and symbol_file_path.is_file():
            symbol_name = symbol_id.split(":", 1)[-1]
            if f'"{symbol_name}"' not in symbol_file_path.read_text(encoding="utf-8"):
                result.issue(f"{prefix} symbol_id is not present in the bound symbol file")
        if footprint_file_path is not None and footprint_file_path.is_file():
            footprint_name = footprint_id.split(":", 1)[-1]
            if f'"{footprint_name}"' not in footprint_file_path.read_text(encoding="utf-8"):
                result.issue(f"{prefix} footprint is not present in the bound footprint file")
        parsed_symbol_pins: list[str] = []
        parsed_footprint_pads: list[str] = []
        if symbol_file_path is not None and symbol_file_path.is_file():
            try:
                parsed_symbol_pins = symbol_pin_numbers(symbol_file_path, symbol_id)
            except ValueError as error:
                result.issue(f"{prefix} cannot parse bound symbol pins: {error}")
        if footprint_file_path is not None and footprint_file_path.is_file():
            try:
                parsed_footprint_pads = footprint_pad_numbers(footprint_file_path, footprint_id)
            except ValueError as error:
                result.issue(f"{prefix} cannot parse bound footprint pads: {error}")
        package_verification = mapping_value(binding.get("package_verification"))
        verified_package_statuses = {
            normalized(value)
            for value in policy_strings(policy, "downstream_binding.verified_package_statuses")
        }
        if normalized(package_verification.get("status")) not in verified_package_statuses:
            result.issue(f"{prefix} package_verification.status is not verified")
        if package_verification.get("package_name") != get_path(selection, "package.name"):
            result.issue(f"{prefix} package_verification.package_name does not match part lock")
        if package_verification.get("pin_count") != get_path(selection, "package.pin_count"):
            result.issue(f"{prefix} package_verification.pin_count does not match part lock")
        pinmap = mapping_value(binding.get("pinmap_evidence"))
        pinmap_path_value = pinmap.get("file")
        if not string_value(pinmap_path_value):
            result.issue(f"{prefix} pinmap_evidence.file is required")
        else:
            pinmap_path = resolved_path(pinmap_path_value, root)
            try:
                pinmap_path.relative_to(artifacts_root)
            except ValueError:
                result.issue(f"{prefix} pinmap evidence must stay under project.artifacts_dir")
            if not pinmap_path.is_file():
                result.issue(f"{prefix} pinmap evidence does not exist")
            elif pinmap.get("sha256") != sha256_file(pinmap_path):
                result.issue(f"{prefix} pinmap evidence sha256 is stale")
            else:
                pinmap_data = load_data_file(pinmap_path)
                for field in policy_strings(policy, "downstream_binding.required_pinmap_fields"):
                    if field not in pinmap_data or pinmap_data.get(field) is None:
                        result.issue(f"{prefix} pinmap evidence missing field: {field}")
                supported_pinmap_versions = {
                    int(value)
                    for value in get_path(policy, "downstream_binding.pinmap_schema_versions") or []
                    if isinstance(value, int)
                }
                if pinmap_data.get("schema_version") not in supported_pinmap_versions:
                    result.issue(f"{prefix} pinmap evidence schema_version is unsupported")
                if pinmap_data.get("ref") != ref:
                    result.issue(f"{prefix} pinmap evidence ref does not match")
                if pinmap_data.get("symbol_id") != binding.get("symbol_id"):
                    result.issue(f"{prefix} pinmap evidence symbol_id does not match")
                if pinmap_data.get("footprint") != binding.get("footprint"):
                    result.issue(f"{prefix} pinmap evidence footprint does not match")
                if pinmap_data.get("symbol_file_sha256") != symbol_file_sha:
                    result.issue(f"{prefix} pinmap symbol_file_sha256 does not match bound library file")
                if pinmap_data.get("footprint_file_sha256") != footprint_file_sha:
                    result.issue(f"{prefix} pinmap footprint_file_sha256 does not match bound library file")
                pinmap_statuses = {
                    normalized(value) for value in policy_strings(policy, "downstream_binding.pinmap_ready_statuses")
                }
                if normalized(pinmap_data.get("status")) not in pinmap_statuses:
                    result.issue(f"{prefix} pinmap evidence status is not verified")
                source_ids = set(string_list(pinmap_data.get("source_evidence_ids")))
                source_digests = set(string_list(pinmap_data.get("source_evidence_sha256")))
                locked_records = {
                    str(item.get("id")): item
                    for item in selection.get("evidence_records", [])
                    if isinstance(selection.get("evidence_records"), list)
                    and isinstance(item, dict)
                    and string_value(item.get("id"))
                }
                if not source_ids or not source_ids.issubset(locked_records):
                    result.issue(f"{prefix} pinmap source evidence IDs are not bound to the part lock")
                expected_source_digests = {
                    str(locked_records[evidence_id].get("sha256"))
                    for evidence_id in source_ids
                    if evidence_id in locked_records
                }
                if source_digests != expected_source_digests:
                    result.issue(f"{prefix} pinmap source evidence hashes do not match source evidence IDs")
                required_source_kinds = {
                    normalized(value)
                    for value in policy_strings(policy, "downstream_binding.pinmap_required_source_evidence_kinds")
                }
                selected_source_kinds = {
                    normalized(locked_records[evidence_id].get("kind"))
                    for evidence_id in source_ids
                    if evidence_id in locked_records
                }
                if not required_source_kinds.issubset(selected_source_kinds):
                    result.issue(f"{prefix} pinmap must cite required locked evidence kinds")
                mappings = pinmap_data.get("mappings")
                if not isinstance(mappings, list):
                    result.issue(f"{prefix} pinmap mappings must be a list")
                    mappings = []
                symbol_pins: set[str] = set()
                footprint_pads: set[str] = set()
                for mapping_index, mapping in enumerate(mappings):
                    if not isinstance(mapping, dict):
                        result.issue(f"{prefix} pinmap mappings[{mapping_index}] must be a mapping")
                        continue
                    for field in policy_strings(policy, "downstream_binding.required_pinmap_mapping_fields"):
                        if not string_value(mapping.get(field)):
                            result.issue(f"{prefix} pinmap mappings[{mapping_index}].{field} is required")
                    symbol_pin = str(mapping.get("symbol_pin", ""))
                    footprint_pad = str(mapping.get("footprint_pad", ""))
                    if (
                        get_path(policy, "downstream_binding.require_same_symbol_pin_and_footprint_pad") is True
                        and symbol_pin != footprint_pad
                    ):
                        result.issue(
                            f"{prefix} pinmap symbol_pin must equal footprint_pad; got {symbol_pin}->{footprint_pad}"
                        )
                    if symbol_pin in symbol_pins:
                        result.issue(f"{prefix} pinmap duplicates symbol pin {symbol_pin}")
                    if footprint_pad in footprint_pads:
                        result.issue(f"{prefix} pinmap duplicates footprint pad {footprint_pad}")
                    symbol_pins.add(symbol_pin)
                    footprint_pads.add(footprint_pad)
                expected_pin_count = get_path(selection, "package.pin_count")
                if isinstance(expected_pin_count, int) and len(mappings) != expected_pin_count:
                    result.issue(
                        f"{prefix} pinmap mapping count {len(mappings)} does not match package pin count {expected_pin_count}"
                    )
                if symbol_pins != set(parsed_symbol_pins):
                    result.issue(f"{prefix} pinmap symbol pins do not exactly match the bound symbol file")
                if footprint_pads != set(parsed_footprint_pads):
                    result.issue(f"{prefix} pinmap footprint pads do not exactly match the bound footprint file")
        component = components.get(ref, {})
        if component.get("footprint") != binding.get("footprint"):
            result.issue(f"{prefix} footprint does not match the component's verified footprint")
        component_binding = mapping_value(component.get("library_binding"))
        expected_component_binding = {
            "manifest_sha256": manifest_sha,
            "symbol_library": binding.get("symbol_library"),
            "symbol_id": binding.get("symbol_id"),
            "footprint_library": binding.get("footprint_library"),
            "footprint": binding.get("footprint"),
        }
        if component_binding != expected_component_binding:
            result.issue(f"{prefix} component.library_binding does not match the verified manifest")
    return {"path": str(manifest_path), "sha256": manifest_sha, "binding_count": len(bindings)}


def check_part_lock(
    spec: dict[str, Any],
    spec_path: Path | None,
    result: CheckResult,
    force: bool = False,
    as_of: str | None = None,
    before_generation: bool = False,
) -> dict[str, Any]:
    policy = load_policy(spec)
    evaluation = evaluate_candidate_manifest(spec, spec_path, result, policy=policy, force=force, as_of=as_of)
    details: dict[str, Any] = {"enabled": evaluation.get("enabled", False), "candidate_evaluation": evaluation}
    if not details["enabled"] or not result.ok():
        return details
    root = project_root(spec, spec_path, policy, result)
    lock_path = configured_artifact_path(spec, policy, "part_lock_path_field", result, root=root)
    ranking_path = configured_artifact_path(spec, policy, "ranking_path_field", result, root=root)
    if lock_path is None or ranking_path is None:
        return details
    if not lock_path.is_file():
        result.issue(f"part lock does not exist: {lock_path}")
        return details
    if not ranking_path.is_file():
        result.issue(f"candidate ranking report does not exist: {ranking_path}")
        return details
    lock = load_data_file(lock_path)
    ranking = load_data_file(ranking_path)
    lock_sha = sha256_file(lock_path)
    ranking_sha = sha256_file(ranking_path)
    lock_metadata = mapping_value(get_path(spec, "sourcing.part_lock"))
    expected_pairs = {
        "sha256": lock_sha,
        "architecture_sha256": evaluation.get("architecture_sha256"),
        "sourcing_context_sha256": evaluation.get("sourcing_context_sha256"),
        "candidate_manifest_sha256": evaluation.get("candidate_manifest_sha256"),
    }
    for field, expected in expected_pairs.items():
        if lock_metadata.get(field) != expected:
            result.issue(f"sourcing.part_lock.{field} is missing or stale")
    metadata_expected = {
        "path": get_path(spec, "sourcing.artifacts.part_lock"),
        "locked_at": lock.get("locked_at"),
        "selections": [
            {"requirement_id": item.get("requirement_id"), "candidate_id": item.get("candidate_id")}
            for item in lock.get("selections", [])
            if isinstance(item, dict)
        ],
    }
    for field, expected in metadata_expected.items():
        if lock_metadata.get(field) != expected:
            result.issue(f"sourcing.part_lock metadata field {field} does not match the locked artifact")
    if lock_metadata.get("status") != "locked":
        result.issue("sourcing.part_lock.status must be locked")
    if lock.get("project_name") != get_path(spec, "project.name"):
        result.issue("part lock project_name does not match the spec")
    if lock.get("architecture_sha256") != evaluation.get("architecture_sha256"):
        result.issue("part lock architecture_sha256 is stale")
    if lock.get("sourcing_context_sha256") != evaluation.get("sourcing_context_sha256"):
        result.issue("part lock sourcing_context_sha256 is stale")
    if get_path(lock, "candidate_manifest.sha256") != evaluation.get("candidate_manifest_sha256"):
        result.issue("part lock candidate manifest digest is stale")
    if get_path(lock, "ranking.sha256") != ranking_sha:
        result.issue("part lock ranking digest is stale")
    if get_path(lock, "candidate_manifest.path") != evaluation.get("candidate_manifest"):
        result.issue("part lock candidate manifest path is stale")
    if get_path(lock, "ranking.path") != get_path(spec, "sourcing.artifacts.ranking"):
        result.issue("part lock ranking path is stale")
    for field in ("project_name", "architecture_sha256", "sourcing_context_sha256"):
        expected = get_path(spec, "project.name") if field == "project_name" else evaluation.get(field)
        if ranking.get(field) != expected:
            result.issue(f"candidate ranking {field} is stale")
    if get_path(ranking, "candidate_manifest.sha256") != evaluation.get("candidate_manifest_sha256"):
        result.issue("candidate ranking manifest digest is stale")
    if ranking.get("requirements") != evaluation.get("requirements"):
        result.issue("candidate ranking results are stale or were not deterministically generated")
    if ranking.get("selected_set") != evaluation.get("selected_set"):
        result.issue("candidate ranking selected-set results are stale")
    if lock.get("selected_set") != evaluation.get("selected_set"):
        result.issue("part lock selected-set compatibility or cost summary is stale")
    now = effective_now(as_of)
    lock_max_age = float(get_path(spec, "sourcing.context.lock_max_age_hours") or 0)
    issue = timestamp_issue(lock.get("locked_at"), now, lock_max_age, "part_lock.locked_at")
    if issue:
        result.issue(issue)

    requirements = requirement_map(spec)
    manifest = load_data_file(Path(str(evaluation["candidate_manifest_resolved"])))
    candidates = candidate_map(manifest)
    expected_selected = {
        str(item.get("requirement_id")): str(item.get("selected_candidate_id"))
        for item in evaluation.get("requirements", []) if isinstance(item, dict)
    }
    selections = {
        str(item.get("requirement_id")): item
        for item in lock.get("selections", []) if isinstance(lock.get("selections"), list) and isinstance(item, dict)
    }
    if set(selections) != set(requirements):
        result.issue("part lock selections do not exactly cover sourcing requirements")
    components = {
        str(item.get("ref")): item
        for item in spec.get("components", []) if isinstance(spec.get("components"), list)
        if isinstance(item, dict) and string_value(item.get("ref"))
    }
    providers = providers_map(spec)
    for requirement_id, expected_candidate_id in expected_selected.items():
        selection = selections.get(requirement_id)
        if selection is None:
            continue
        if str(selection.get("candidate_id")) != expected_candidate_id:
            result.issue(f"part lock selection for {requirement_id} is not the highest-ranked qualified candidate")
        requirement = requirements[requirement_id]
        candidate = candidates.get((requirement_id, expected_candidate_id))
        evaluation_by_id = {
            str(item.get("candidate_id")): item
            for item in next(
                (
                    entry.get("candidates", [])
                    for entry in evaluation.get("requirements", [])
                    if isinstance(entry, dict) and entry.get("requirement_id") == requirement_id
                ),
                [],
            )
            if isinstance(item, dict)
        }
        if candidate is None or expected_candidate_id not in evaluation_by_id:
            result.issue(f"part lock candidate {requirement_id}/{expected_candidate_id} is missing from the manifest")
        else:
            alternates = [
                alternate_record(candidates[(requirement_id, str(item.get("candidate_id")))], item)
                for item in evaluation_by_id.values()
                if item.get("qualified") is True and str(item.get("candidate_id")) != expected_candidate_id
            ]
            expected_selection = selection_record(
                requirement, candidate, evaluation_by_id[expected_candidate_id], alternates
            )
            if selection != expected_selection:
                result.issue(f"part lock selection record for {requirement_id} does not match candidate evidence")
        if selection.get("component_refs") != requirement.get("component_refs"):
            result.issue(f"part lock component_refs for {requirement_id} are stale")
        if selection.get("architecture_trace") != {
            "block_ids": requirement.get("block_ids", []),
            "constraint_ids": requirement.get("constraint_ids", []),
        }:
            result.issue(f"part lock architecture trace for {requirement_id} is stale")
        for ref in selection.get("component_refs", []):
            component = components.get(str(ref))
            if component is None:
                result.issue(f"part lock references missing component {ref}")
                continue
            for failure in compare_component_to_selection(component, selection, providers, policy):
                result.issue(failure)

    downstream = mapping_value(get_path(spec, "sourcing.downstream_binding"))
    invalid_statuses = {normalized(value) for value in policy_strings(policy, "downstream_invalid_status_values")}
    if normalized(downstream.get("status")) in invalid_statuses:
        result.issue("sourcing downstream binding is invalidated by a part-lock change")
    ready_statuses = {normalized(value) for value in policy_strings(policy, "downstream_ready_status_values")}
    if before_generation and normalized(downstream.get("status")) not in ready_statuses:
        result.issue("sourcing downstream binding must be ready before KiCad generation")
    if downstream and downstream.get("part_lock_sha256") != lock_sha:
        result.issue("sourcing.downstream_binding.part_lock_sha256 is stale")
    if before_generation and normalized(downstream.get("status")) in ready_statuses:
        details["downstream_binding_manifest"] = check_downstream_manifest(
            spec, lock, lock_sha, downstream, policy, result, root, effective_now(as_of)
        )
    details.update(
        {
            "part_lock": str(lock_path),
            "part_lock_sha256": lock_sha,
            "ranking": str(ranking_path),
            "ranking_sha256": ranking_sha,
            "selection_count": len(selections),
        }
    )
    if result.ok():
        result.warning("part lock uses timestamped availability evidence; inventory is not reserved")
    return details
