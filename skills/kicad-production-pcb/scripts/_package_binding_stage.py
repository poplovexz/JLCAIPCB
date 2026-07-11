#!/usr/bin/env python3
"""Physical package, semantic pin-map, provenance, and orientation gate."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from _kicad_sexpr import footprint_feature_summary, footprint_pad_records, symbol_pin_records
from _pcb_skill_checks import CheckResult, get_path, load_spec, sha256_file, string_value
from _schematic_stage import check_evidence as check_schematic_evidence


SKILL_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = SKILL_ROOT / "assets" / "package-binding-stage-policy.yaml"


def mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def sequence(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def strings(value: Any) -> list[str]:
    return [str(item) for item in sequence(value) if string_value(item)]


def load_data(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle) if path.suffix.lower() == ".json" else yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"top-level evidence must be a mapping: {path}")
    return data


def load_policy() -> dict[str, Any]:
    return load_data(POLICY_PATH)


def project_root(spec: dict[str, Any], spec_path: Path) -> Path:
    configured = get_path(spec, "project.root_dir")
    if string_value(configured):
        return (spec_path.resolve().parent / str(configured)).resolve()
    return Path.cwd().resolve()


def resolve(root: Path, value: Any) -> Path:
    path = Path(str(value))
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def artifacts_root(spec: dict[str, Any], root: Path) -> Path:
    return resolve(root, get_path(spec, "project.artifacts_dir"))


def ensure_artifact(path: Path, artifacts: Path, result: CheckResult, label: str) -> None:
    try:
        path.relative_to(artifacts)
    except ValueError:
        result.issue(f"{label} must stay under project.artifacts_dir")


def evidence_path(spec: dict[str, Any], root: Path, policy: dict[str, Any]) -> Path:
    configured = get_path(spec, "verification.package_binding_stage.evidence_file")
    if string_value(configured):
        return resolve(root, configured)
    project_name = str(get_path(spec, "project.name") or "project")
    return artifacts_root(spec, root) / str(policy["default_evidence_subdir"]) / project_name / str(
        policy["default_evidence_filename"]
    )


def stage_required(spec: dict[str, Any], force: bool = False) -> bool:
    explicit = get_path(spec, "verification.package_binding_stage.required")
    binding_status = str(get_path(spec, "sourcing.downstream_binding.status") or "").lower()
    return force or explicit is True or binding_status == "ready"


def no_connects(spec: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for entry in sequence(get_path(spec, "schematic.no_connects")):
        if not isinstance(entry, dict) or not string_value(entry.get("ref")):
            continue
        for pin in sequence(entry.get("pins")):
            result.add(f"{entry['ref']}.{pin}")
    return result


def evidence_record(
    record: Any,
    root: Path,
    artifacts: Path,
    result: CheckResult,
    label: str,
    under_artifacts: bool = True,
    load_content: bool = True,
) -> tuple[Path | None, dict[str, Any]]:
    meta = mapping(record)
    file_value = meta.get("file", meta.get("path"))
    if not string_value(file_value) or not string_value(meta.get("sha256")):
        result.issue(f"{label} requires file/path and sha256")
        return None, {}
    path = resolve(root, file_value)
    if under_artifacts:
        ensure_artifact(path, artifacts, result, label)
    if not path.is_file():
        result.issue(f"{label} file does not exist: {path}")
        return path, {}
    if meta["sha256"] != sha256_file(path):
        result.issue(f"{label} sha256 is stale")
        return path, {}
    if not load_content:
        return path, {}
    try:
        return path, load_data(path)
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as error:
        result.issue(f"cannot load {label}: {error}")
        return path, {}


def require_fields(data: dict[str, Any], fields: list[str], result: CheckResult, label: str) -> None:
    for field in fields:
        value = data.get(field)
        if value is None or value == "" or value == [] or value == {}:
            result.issue(f"{label}.{field} is required")


def finite_angle(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def validate_orientation(binding: dict[str, Any], policy: dict[str, Any], result: CheckResult, label: str) -> None:
    orientation = mapping(binding.get("orientation_contract"))
    config = mapping(policy.get("orientation"))
    require_fields(orientation, strings(config.get("required_fields")), result, f"{label}.orientation_contract")
    if orientation.get("datasheet_view") not in strings(config.get("allowed_datasheet_views")):
        result.issue(f"{label}.orientation_contract.datasheet_view is unsupported")
    if orientation.get("placement_origin") not in strings(config.get("allowed_placement_origins")):
        result.issue(f"{label}.orientation_contract.placement_origin is unsupported")
    if orientation.get("bottom_side_transform") not in strings(config.get("allowed_bottom_side_transforms")):
        result.issue(f"{label}.orientation_contract.bottom_side_transform is unsupported")
    if orientation.get("polarity_kind") not in strings(config.get("allowed_polarity_kinds")):
        result.issue(f"{label}.orientation_contract.polarity_kind is unsupported")
    if not finite_angle(orientation.get("cpl_rotation_offset_deg")):
        result.issue(f"{label}.orientation_contract.cpl_rotation_offset_deg must be numeric")


def validate_geometry(
    binding: dict[str, Any],
    footprint_records: list[dict[str, Any]],
    footprint_features: dict[str, Any],
    root: Path,
    artifacts: Path,
    policy: dict[str, Any],
    result: CheckResult,
    label: str,
) -> tuple[Path | None, dict[str, Any]]:
    path, data = evidence_record(binding.get("footprint_geometry_evidence"), root, artifacts, result, f"{label}.footprint_geometry_evidence")
    config = mapping(policy.get("geometry"))
    require_fields(data, strings(config.get("required_fields")), result, f"{label}.geometry")
    if data.get("schema_version") not in sequence(policy.get("geometry_schema_versions")):
        result.issue(f"{label}.geometry.schema_version is unsupported")
    if data.get("status") not in strings(policy.get("ready_statuses")):
        result.issue(f"{label}.geometry.status is not verified")
    dimensions = mapping(data.get("body_dimensions_mm"))
    for field in strings(config.get("required_body_dimensions")):
        try:
            if float(dimensions.get(field, 0)) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            result.issue(f"{label}.geometry.body_dimensions_mm.{field} must be positive")
    if sequence(data.get("pads")) != footprint_records:
        result.issue(f"{label}.geometry pads do not exactly match the bound footprint geometry")
    if mapping(data.get("features")) != footprint_features:
        result.issue(f"{label}.geometry features do not exactly match the bound footprint")
    contract = mapping(data.get("fabrication_contract"))
    require_fields(
        contract,
        strings(config.get("required_fabrication_contract_fields")),
        result,
        f"{label}.geometry.fabrication_contract",
    )
    graphics = set(mapping(footprint_features.get("graphics_by_layer")))
    if contract.get("courtyard") != "verified" or not any(layer.endswith(".CrtYd") for layer in graphics):
        result.issue(f"{label}.geometry requires a verified Courtyard graphic")
    if contract.get("fab_outline") != "verified" or not any(layer.endswith(".Fab") for layer in graphics):
        result.issue(f"{label}.geometry requires a verified Fab outline")
    allowed_statuses = set(strings(config.get("verified_presence_statuses")))
    for field in ["silkscreen_pin1_marker", "paste_strategy", "exposed_pad_strategy"]:
        item = mapping(contract.get(field))
        if item.get("status") not in allowed_statuses or not string_value(item.get("rationale")):
            result.issue(f"{label}.geometry.fabrication_contract.{field} requires status and rationale")
    if int(footprint_features.get("paste_pad_count", 0)) > 0 and get_path(contract, "paste_strategy.status") != "verified":
        result.issue(f"{label}.geometry paste-bearing footprint requires verified paste_strategy")
    model_disposition = contract.get("model_disposition")
    if model_disposition not in strings(config.get("model_dispositions")):
        result.issue(f"{label}.geometry.fabrication_contract.model_disposition is unsupported")
    if model_disposition == "present" and not sequence(footprint_features.get("models")):
        result.issue(f"{label}.geometry declares a 3D model but the footprint has none")
    anchor = mapping(contract.get("placement_anchor"))
    cpl_anchor = mapping(anchor.get("cpl_anchor_mm"))
    body_offset = mapping(anchor.get("body_center_offset_mm"))
    for name, point in [("cpl_anchor_mm", cpl_anchor), ("body_center_offset_mm", body_offset)]:
        if not all(isinstance(point.get(axis), (int, float)) and not isinstance(point.get(axis), bool) for axis in ["x", "y"]):
            result.issue(f"{label}.geometry.fabrication_contract.placement_anchor.{name} requires numeric x/y")
    if cpl_anchor and (float(cpl_anchor.get("x", 1)) != 0 or float(cpl_anchor.get("y", 1)) != 0):
        result.issue(f"{label}.geometry CPL anchor must be the KiCad footprint origin (0,0)")
    return path, data


def validate_import_provenance(
    binding: dict[str, Any], selection: dict[str, Any], root: Path, artifacts: Path, policy: dict[str, Any], result: CheckResult, label: str
) -> Path | None:
    origin = binding.get("library_origin")
    if origin not in strings(policy.get("library_origins")):
        result.issue(f"{label}.library_origin is unsupported")
        return None
    if origin not in strings(policy.get("imported_origins")):
        return None
    path, data = evidence_record(binding.get("import_provenance"), root, artifacts, result, f"{label}.import_provenance")
    config = mapping(policy.get("import_provenance"))
    require_fields(data, strings(config.get("required_fields")), result, f"{label}.import_provenance")
    if data.get("schema_version") not in sequence(policy.get("import_provenance_schema_versions")):
        result.issue(f"{label}.import_provenance.schema_version is unsupported")
    if data.get("status") not in strings(policy.get("ready_statuses")):
        result.issue(f"{label}.import_provenance.status is not verified")
    import_executor = SKILL_ROOT / "scripts" / "library_import_transaction.py"
    if not import_executor.is_file() or data.get("executor_sha256") != sha256_file(import_executor):
        result.issue(f"{label}.import_provenance was not produced by the current import transaction executor")
    require_fields(mapping(data.get("tool")), strings(config.get("required_tool_fields")), result, f"{label}.import_provenance.tool")
    source = mapping(data.get("source"))
    require_fields(source, strings(config.get("required_source_fields")), result, f"{label}.import_provenance.source")
    if string_value(source.get("file")):
        source_path = resolve(root, source["file"])
        ensure_artifact(source_path, artifacts, result, f"{label}.import_provenance.source")
        if not source_path.is_file() or source.get("sha256") != sha256_file(source_path):
            result.issue(f"{label}.import_provenance source snapshot is missing or stale")
    locked_source = evidence_by_id(selection).get(str(source.get("evidence_id")), {})
    if source.get("evidence_sha256") != locked_source.get("sha256") or source.get("sha256") != locked_source.get("sha256"):
        result.issue(f"{label}.import_provenance source does not match locked part evidence")
    transaction = mapping(data.get("transaction"))
    require_fields(transaction, strings(config.get("required_transaction_fields")), result, f"{label}.import_provenance.transaction")
    if transaction.get("promoted") is not True:
        result.issue(f"{label}.import_provenance.transaction.promoted must be true")
    manifest_path = resolve(root, transaction.get("manifest"))
    ensure_artifact(manifest_path, artifacts, result, f"{label}.import_provenance.transaction.manifest")
    if not manifest_path.is_file() or transaction.get("manifest_sha256") != sha256_file(manifest_path):
        result.issue(f"{label}.import_provenance transaction manifest is missing or stale")
        transaction_manifest: dict[str, Any] = {}
    else:
        transaction_manifest = load_data(manifest_path)
    if mapping(transaction_manifest.get("source")) != source or mapping(transaction_manifest.get("tool")) != mapping(data.get("tool")):
        result.issue(f"{label}.import_provenance source/tool do not match the transaction manifest")
    outputs = mapping(data.get("outputs"))
    for key, evidence_key in [("symbol", "symbol_file_evidence"), ("footprint", "footprint_file_evidence")]:
        if outputs.get(key) != get_path(binding, f"{evidence_key}.sha256"):
            result.issue(f"{label}.import_provenance.outputs.{key} does not bind the library file")
    manifest_outputs = {
        str(item.get("role")): str(item.get("sha256"))
        for item in sequence(transaction_manifest.get("outputs"))
        if isinstance(item, dict) and string_value(item.get("role"))
    }
    if outputs != manifest_outputs:
        result.issue(f"{label}.import_provenance outputs do not match the transaction manifest")
    return path


def locked_selections(spec: dict[str, Any], root: Path, artifacts: Path, result: CheckResult) -> tuple[dict[str, dict[str, Any]], Path | None]:
    lock_meta = mapping(get_path(spec, "sourcing.part_lock"))
    path_value = lock_meta.get("path")
    if not string_value(path_value) or not string_value(lock_meta.get("sha256")):
        result.issue("sourcing.part_lock requires path and sha256 for package binding")
        return {}, None
    path = resolve(root, path_value)
    ensure_artifact(path, artifacts, result, "sourcing.part_lock")
    if not path.is_file() or lock_meta.get("sha256") != sha256_file(path):
        result.issue("sourcing.part_lock file is missing or stale for package binding")
        return {}, path
    data = load_data(path)
    selections: dict[str, dict[str, Any]] = {}
    for selection in sequence(data.get("selections")):
        if not isinstance(selection, dict):
            continue
        for ref in strings(selection.get("component_refs")):
            selections[ref] = selection
    return selections, path


def compare_package_dimensions(
    geometry: dict[str, Any], locked_package: dict[str, Any], policy: dict[str, Any], result: CheckResult, label: str
) -> None:
    dimensions = mapping(geometry.get("body_dimensions_mm"))
    tolerance = float(get_path(policy, "geometry.dimension_tolerance_mm") or 0)
    for geometry_key, lock_key in [("length", "body_length_mm"), ("width", "body_width_mm")]:
        try:
            actual = float(dimensions[geometry_key])
            expected = float(locked_package[lock_key])
        except (KeyError, TypeError, ValueError):
            result.issue(f"{label} cannot compare geometry {geometry_key} with locked package {lock_key}")
            continue
        if not math.isclose(actual, expected, rel_tol=0, abs_tol=tolerance):
            result.issue(f"{label} geometry {geometry_key}={actual} does not match locked package {lock_key}={expected}")


def evidence_by_id(selection: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("id")): item
        for item in sequence(selection.get("evidence_records"))
        if isinstance(item, dict) and string_value(item.get("id"))
    }


def validate_datasheet_pin_table(
    binding: dict[str, Any],
    selection: dict[str, Any],
    pinmap: dict[str, Any],
    root: Path,
    artifacts: Path,
    policy: dict[str, Any],
    result: CheckResult,
    label: str,
) -> Path | None:
    path, data = evidence_record(
        binding.get("datasheet_pin_table_evidence"), root, artifacts, result, f"{label}.datasheet_pin_table_evidence"
    )
    config = mapping(policy.get("datasheet_pin_table"))
    require_fields(data, strings(config.get("required_fields")), result, f"{label}.datasheet_pin_table")
    if data.get("schema_version") not in sequence(policy.get("datasheet_pin_table_schema_versions")):
        result.issue(f"{label}.datasheet_pin_table schema_version is unsupported")
    if data.get("status") not in strings(policy.get("ready_statuses")):
        result.issue(f"{label}.datasheet_pin_table status is not verified")
    if data.get("manufacturer") != selection.get("manufacturer") or data.get("mpn") != selection.get("mpn"):
        result.issue(f"{label}.datasheet_pin_table part identity does not match the part lock")
    if data.get("package") != get_path(selection, "package.name"):
        result.issue(f"{label}.datasheet_pin_table package does not match the part lock")
    source = mapping(data.get("source"))
    require_fields(source, strings(config.get("required_source_fields")), result, f"{label}.datasheet_pin_table.source")
    records = evidence_by_id(selection)
    locked_source = records.get(str(source.get("evidence_id")), {})
    if locked_source.get("kind") != "manufacturer-datasheet" or source.get("evidence_sha256") != locked_source.get("sha256"):
        result.issue(f"{label}.datasheet_pin_table source is not the locked manufacturer datasheet")
    if string_value(source.get("file")):
        source_path = resolve(root, source["file"])
        ensure_artifact(source_path, artifacts, result, f"{label}.datasheet_pin_table.source")
        if not source_path.is_file() or source.get("file_sha256") != sha256_file(source_path):
            result.issue(f"{label}.datasheet_pin_table raw datasheet snapshot is missing or stale")
        elif source.get("file_sha256") != source.get("evidence_sha256"):
            result.issue(f"{label}.datasheet_pin_table snapshot does not match locked datasheet evidence")
    require_fields(mapping(data.get("extractor")), strings(config.get("required_extractor_fields")), result, f"{label}.datasheet_pin_table.extractor")
    table_records: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(sequence(data.get("records"))):
        item = mapping(raw)
        require_fields(item, strings(config.get("required_record_fields")), result, f"{label}.datasheet_pin_table.records[{index}]")
        pin = str(item.get("pin", ""))
        if pin in table_records:
            result.issue(f"{label}.datasheet_pin_table duplicates pin {pin}")
        table_records[pin] = item
    mapped_records = {str(item.get("datasheet_pin", "")): item for item in sequence(pinmap.get("mappings")) if isinstance(item, dict)}
    if set(table_records) != set(mapped_records):
        result.issue(f"{label}.datasheet_pin_table does not exactly cover the semantic pin map")
    for pin, table_item in table_records.items():
        mapped = mapped_records.get(pin, {})
        for table_field, mapping_field in [("name", "datasheet_pin_name"), ("function", "function"), ("source_locator", "source_locator")]:
            if table_item.get(table_field) != mapped.get(mapping_field):
                result.issue(f"{label}.datasheet pin {pin} {table_field} does not match the semantic pin map")
    return path


def validate_orientation_sources(
    binding: dict[str, Any], selection: dict[str, Any], policy: dict[str, Any], result: CheckResult, label: str
) -> None:
    orientation = mapping(binding.get("orientation_contract"))
    records = evidence_by_id(selection)
    source = records.get(str(orientation.get("source_evidence_id")), {})
    if orientation.get("source_evidence_sha256") != source.get("sha256"):
        result.issue(f"{label}.orientation_contract source hash does not match the part lock")
    if selection.get("disposition") != "pcba":
        return
    config = mapping(policy.get("orientation"))
    require_fields(
        orientation,
        strings(config.get("pcba_independent_source_fields")),
        result,
        f"{label}.orientation_contract",
    )
    assembly_id = str(orientation.get("assembly_source_evidence_id", ""))
    assembly_source = records.get(assembly_id, {})
    if assembly_id == str(orientation.get("source_evidence_id", "")):
        result.issue(f"{label}.orientation_contract PCBA source must be independent from the datasheet source")
    if assembly_source.get("kind") not in strings(config.get("pcba_independent_source_kinds")):
        result.issue(f"{label}.orientation_contract PCBA source kind is not accepted")
    if orientation.get("assembly_source_evidence_sha256") != assembly_source.get("sha256"):
        result.issue(f"{label}.orientation_contract PCBA source hash does not match the part lock")


def validate_pinmap(
    spec: dict[str, Any],
    component: dict[str, Any],
    binding: dict[str, Any],
    symbol_records: list[dict[str, str]],
    footprint_records: list[dict[str, Any]],
    root: Path,
    artifacts: Path,
    policy: dict[str, Any],
    result: CheckResult,
    label: str,
) -> tuple[Path | None, dict[str, Any]]:
    path, data = evidence_record(binding.get("pinmap_evidence"), root, artifacts, result, f"{label}.pinmap_evidence")
    require_fields(data, strings(policy.get("required_pinmap_fields")), result, f"{label}.pinmap")
    if data.get("schema_version") not in sequence(policy.get("pinmap_schema_versions")):
        result.issue(f"{label}.pinmap schema_version is unsupported")
    if data.get("status") not in strings(policy.get("ready_statuses")):
        result.issue(f"{label}.pinmap status is not verified")
    if data.get("ref") != component.get("ref") or data.get("symbol_id") != binding.get("symbol_id") or data.get("footprint") != binding.get("footprint"):
        result.issue(f"{label}.pinmap identity does not match the binding")
    if data.get("symbol_file_sha256") != get_path(binding, "symbol_file_evidence.sha256"):
        result.issue(f"{label}.pinmap symbol_file_sha256 does not match the binding")
    if data.get("footprint_file_sha256") != get_path(binding, "footprint_file_evidence.sha256"):
        result.issue(f"{label}.pinmap footprint_file_sha256 does not match the binding")
    actual_symbols = {record["number"]: record for record in symbol_records}
    if len(actual_symbols) != len(symbol_records):
        result.issue(f"{label} symbol contains duplicate electrical pin numbers")
    footprint_numbers = {record["number"] for record in footprint_records}
    covered_symbols: set[str] = set()
    covered_footprints: set[str] = set()
    connected = set(strings(policy.get("connected_dispositions")))
    unconnected = set(strings(policy.get("unconnected_dispositions")))
    nc = no_connects(spec)
    pads = {str(key): str(value) for key, value in mapping(component.get("pads")).items()}
    mappings = sequence(data.get("mappings"))
    for index, raw in enumerate(mappings):
        item = mapping(raw)
        item_label = f"{label}.pinmap.mappings[{index}]"
        require_fields(item, strings(policy.get("required_mapping_fields")), result, item_label)
        symbol_pin = str(item.get("symbol_pin", ""))
        footprint_pad = str(item.get("footprint_pad", ""))
        actual = actual_symbols.get(symbol_pin)
        if actual is None:
            result.issue(f"{item_label}.symbol_pin is absent from the bound symbol")
        else:
            if item.get("symbol_pin_name") != actual["name"]:
                result.issue(f"{item_label}.symbol_pin_name does not match the bound symbol")
            if item.get("symbol_electrical_type") != actual["electrical_type"]:
                result.issue(f"{item_label}.symbol_electrical_type does not match the bound symbol")
        if footprint_pad not in footprint_numbers:
            result.issue(f"{item_label}.footprint_pad is absent from the bound footprint")
        if str(item.get("datasheet_pin")) != footprint_pad:
            result.issue(f"{item_label}.datasheet_pin must identify the mapped KiCad footprint pad")
        disposition = item.get("disposition")
        pin_id = f"{component.get('ref')}.{symbol_pin}"
        if disposition in connected:
            if symbol_pin not in pads:
                result.issue(f"{item_label} says connected but the Spec has no net")
            elif item.get("net") != pads[symbol_pin]:
                result.issue(f"{item_label}.net does not match the Spec net")
        elif disposition in unconnected:
            if pin_id not in nc:
                result.issue(f"{item_label} says no_connect but the schematic contract does not")
        else:
            result.issue(f"{item_label}.disposition is unsupported")
        if symbol_pin in covered_symbols:
            result.issue(f"{label}.pinmap duplicates symbol pin {symbol_pin}")
        covered_symbols.add(symbol_pin)
        covered_footprints.add(footprint_pad)
    if covered_symbols != set(actual_symbols):
        result.issue(f"{label}.pinmap does not cover every symbol pin")
    if covered_footprints != footprint_numbers:
        result.issue(f"{label}.pinmap does not cover every electrical footprint pad number")
    return path, data


def validate_contract(spec: dict[str, Any], spec_path: Path, result: CheckResult, force: bool = False) -> dict[str, Any]:
    policy = load_policy()
    required = stage_required(spec, force)
    details: dict[str, Any] = {"required": required}
    if not required:
        result.warning("Package binding stage is not required for this legacy/draft spec")
        return details
    root = project_root(spec, spec_path)
    artifacts = artifacts_root(spec, root)
    manifest_meta = mapping(get_path(spec, "sourcing.downstream_binding.manifest"))
    manifest_path, manifest = evidence_record(manifest_meta, root, artifacts, result, "sourcing.downstream_binding.manifest")
    details["binding_manifest"] = str(manifest_path) if manifest_path else None
    if manifest.get("schema_version") not in sequence(policy.get("binding_manifest_schema_versions")):
        result.issue("library binding manifest schema_version does not support the package binding stage")
    components = {
        str(item.get("ref")): item for item in sequence(spec.get("components")) if isinstance(item, dict) and string_value(item.get("ref"))
    }
    locked_by_ref, part_lock_path = locked_selections(spec, root, artifacts, result)
    if manifest.get("part_lock_sha256") != get_path(spec, "sourcing.part_lock.sha256"):
        result.issue("library binding manifest does not bind the current part lock")
    checked_files: list[Path] = [path for path in [manifest_path, part_lock_path] if path]
    for index, raw in enumerate(sequence(manifest.get("bindings"))):
        binding = mapping(raw)
        ref = str(binding.get("ref", ""))
        label = f"bindings[{index}]({ref or '?'})"
        component = components.get(ref)
        if component is None:
            result.issue(f"{label} references an unknown component")
            continue
        symbol_path, _ = evidence_record(
            binding.get("symbol_file_evidence"), root, artifacts, result, f"{label}.symbol_file_evidence",
            under_artifacts=False, load_content=False
        )
        footprint_path, _ = evidence_record(
            binding.get("footprint_file_evidence"), root, artifacts, result, f"{label}.footprint_file_evidence",
            under_artifacts=False, load_content=False
        )
        if symbol_path is None or footprint_path is None or not symbol_path.is_file() or not footprint_path.is_file():
            continue
        checked_files.extend([symbol_path, footprint_path])
        try:
            symbol_records = symbol_pin_records(symbol_path, str(binding.get("symbol_id", "")))
            footprint_records = footprint_pad_records(footprint_path, str(binding.get("footprint", "")))
            footprint_features = footprint_feature_summary(footprint_path, str(binding.get("footprint", "")))
        except (OSError, ValueError) as error:
            result.issue(f"{label} cannot parse bound library geometry: {error}")
            continue
        pinmap_path, pinmap = validate_pinmap(spec, component, binding, symbol_records, footprint_records, root, artifacts, policy, result, label)
        selection = locked_by_ref.get(ref, {})
        datasheet_table_path = validate_datasheet_pin_table(binding, selection, pinmap, root, artifacts, policy, result, label)
        geometry_path, geometry = validate_geometry(
            binding, footprint_records, footprint_features, root, artifacts, policy, result, label
        )
        pinmap_source_ids = set(strings(pinmap.get("source_evidence_ids")))
        pinmap_source_hashes = set(strings(pinmap.get("source_evidence_sha256")))
        if set(strings(geometry.get("source_evidence_ids"))) != pinmap_source_ids:
            result.issue(f"{label}.geometry source evidence IDs do not match the semantic pin map")
        if set(strings(geometry.get("source_evidence_sha256"))) != pinmap_source_hashes:
            result.issue(f"{label}.geometry source evidence hashes do not match the semantic pin map")
        orientation_source = get_path(binding, "orientation_contract.source_evidence_id")
        if orientation_source not in pinmap_source_ids:
            result.issue(f"{label}.orientation_contract source is not bound to the semantic pin map")
        compare_package_dimensions(geometry, mapping(selection.get("package")), policy, result, label)
        provenance_path = validate_import_provenance(binding, selection, root, artifacts, policy, result, label)
        validate_orientation(binding, policy, result, label)
        validate_orientation_sources(binding, selection, policy, result, label)
        checked_files.extend(path for path in [pinmap_path, datasheet_table_path, geometry_path, provenance_path] if path)
    if not sequence(manifest.get("bindings")):
        result.issue("library binding manifest contains no bindings")
    details["root"] = str(root)
    details["evidence"] = str(evidence_path(spec, root, policy))
    details["checked_files"] = sorted({str(path) for path in checked_files})
    return details


def write_stage_evidence(spec: dict[str, Any], spec_path: Path, result: CheckResult, force: bool = False) -> dict[str, Any]:
    details = validate_contract(spec, spec_path, result, force)
    if not details.get("required") or not result.ok():
        return details
    check_schematic_evidence(spec, spec_path, result, force=True)
    if not result.ok():
        return details
    policy = load_policy()
    root = Path(details["root"])
    target = Path(details["evidence"])
    ensure_artifact(target, artifacts_root(spec, root), result, "package binding stage evidence")
    files = []
    for raw_path in details["checked_files"]:
        path = Path(raw_path)
        files.append({"path": str(path.relative_to(root)) if path.is_relative_to(root) else str(path), "sha256": sha256_file(path)})
    payload = {
        "schema_version": policy["manifest_schema_version"],
        "status": "passed",
        "project_name": get_path(spec, "project.name"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "spec_sha256": sha256_file(spec_path),
        "policy_sha256": sha256_file(POLICY_PATH),
        "executor_sha256": sha256_file(Path(__file__).resolve()),
        "files": files,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return details


def check_stage_evidence(spec: dict[str, Any], spec_path: Path, result: CheckResult, force: bool = False) -> dict[str, Any]:
    details = validate_contract(spec, spec_path, result, force)
    if not details.get("required"):
        return details
    target = Path(details["evidence"])
    if not target.is_file():
        result.issue(f"Package binding stage evidence is missing: {target}")
        return details
    data = load_data(target)
    policy = load_policy()
    if data.get("schema_version") != policy.get("manifest_schema_version") or data.get("status") != "passed":
        result.issue("Package binding stage evidence is stale or not passed")
    if data.get("project_name") != get_path(spec, "project.name") or data.get("spec_sha256") != sha256_file(spec_path):
        result.issue("Package binding stage evidence does not bind the current Spec")
    if data.get("policy_sha256") != sha256_file(POLICY_PATH) or data.get("executor_sha256") != sha256_file(Path(__file__).resolve()):
        result.issue("Package binding stage policy or executor changed after validation")
    expected = {str(path): sha256_file(Path(path)) for path in details["checked_files"]}
    recorded: dict[str, str] = {}
    root = Path(details["root"])
    for record in sequence(data.get("files")):
        item = mapping(record)
        path = resolve(root, item.get("path"))
        recorded[str(path)] = str(item.get("sha256", ""))
    if recorded != expected:
        result.issue("Package binding stage checked files changed after validation")
    return details
