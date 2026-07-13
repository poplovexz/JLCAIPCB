#!/usr/bin/env python3
"""Build and verify local immutable Spec Freeze manifests."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from _part_lock import bytes_sha256, replace_files_transactionally, roundtrip_spec_bytes
from _pcb_skill_checks import CheckResult, get_path, load_spec, load_yaml, net_names, sha256_file, string_value
from _product_baseline import product_baseline_path, render_product_baseline, renderer_path


def mapping_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def normalized(value: Any) -> str:
    return str(value).strip().lower().replace("_", "-") if value is not None else ""


def string_list(value: Any) -> list[str]:
    return [str(item) for item in list_value(value) if string_value(item)]


def policy_path() -> Path:
    return Path(__file__).resolve().parents[1] / "assets" / "spec-freeze-policy.yaml"


def load_policy() -> dict[str, Any]:
    return load_yaml(policy_path())


def dotted_exists(data: dict[str, Any], dotted_path: str) -> bool:
    current: Any = data
    for part in dotted_path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return current not in (None, "", [], {})


def bool_field(data: dict[str, Any], dotted_path: str) -> bool:
    return get_path(data, dotted_path) is True


def production_required(spec: dict[str, Any], policy: dict[str, Any], forced: bool = False) -> bool:
    if forced:
        return True
    stage = normalized(get_path(spec, "project.stage"))
    production_stages = {normalized(item) for item in string_list(policy.get("production_stage_values"))}
    signals = string_list(policy.get("production_signals"))
    return stage in production_stages or any(dotted_exists(spec, signal) for signal in signals)


def freeze_required(spec: dict[str, Any], policy: dict[str, Any], forced: bool = False) -> bool:
    if forced:
        return True
    if production_required(spec, policy):
        return True
    required_field = str(policy.get("force_required_field", "validation.spec_freeze.required"))
    if bool_field(spec, required_field):
        return True
    stage = normalized(get_path(spec, "project.stage"))
    return stage in {normalized(item) for item in string_list(policy.get("required_stage_values"))}


def freeze_tier(spec: dict[str, Any], policy: dict[str, Any], production: bool = False) -> str:
    return "production" if production_required(spec, policy, forced=production) else "local_mvp"


def json_compatible(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_compatible(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_compatible(item) for item in value]
    if isinstance(value, tuple):
        return [json_compatible(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        json_compatible(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonical_spec(spec: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    excluded = {str(item) for item in string_list(get_path(policy, "digest.excluded_top_level_keys"))}
    return {key: copy.deepcopy(value) for key, value in spec.items() if str(key) not in excluded}


def spec_digest(spec: dict[str, Any], policy: dict[str, Any]) -> str:
    return canonical_sha256(canonical_spec(spec, policy))


def section_digests(spec: dict[str, Any], policy: dict[str, Any]) -> dict[str, str]:
    return {
        section: canonical_sha256(spec.get(section))
        for section in string_list(get_path(policy, "digest.bound_sections"))
        if section in spec
    }


def project_root(spec: dict[str, Any], spec_path: Path, policy: dict[str, Any], result: CheckResult) -> Path:
    root_field = str(get_path(policy, "paths.project_root_field") or "project.root_dir")
    configured = get_path(spec, root_field)
    if string_value(configured):
        raw = Path(str(configured))
        root = raw.resolve() if raw.is_absolute() else (spec_path.resolve().parent / raw).resolve()
    else:
        root = Path.cwd().resolve()
        if production_required(spec, policy) and get_path(policy, "paths.require_explicit_project_root_for_production") is True:
            result.issue(f"{root_field} is required for deterministic production Spec Freeze paths")
    if get_path(policy, "paths.require_spec_under_project_root") is True:
        try:
            spec_path.resolve().relative_to(root)
        except ValueError:
            result.issue(f"spec path must stay under {root_field}: {root}")
    return root


def resolve_from_root(value: Any, root: Path) -> Path:
    raw = Path(str(value))
    return raw.resolve() if raw.is_absolute() else (root / raw).resolve()


def artifacts_root(
    spec: dict[str, Any], spec_path: Path, policy: dict[str, Any], result: CheckResult
) -> tuple[Path, Path]:
    root = project_root(spec, spec_path, policy, result)
    field = str(get_path(policy, "paths.artifacts_dir_field") or "project.artifacts_dir")
    configured = get_path(spec, field)
    if not string_value(configured):
        result.issue(f"{field} is required for local Spec Freeze evidence")
        return root, root
    artifacts = resolve_from_root(configured, root)
    if get_path(policy, "paths.require_artifacts_under_project_root") is True:
        try:
            artifacts.relative_to(root)
        except ValueError:
            result.issue(f"{field} must stay under project.root_dir: {artifacts}")
    return root, artifacts


def check_output_root(
    spec: dict[str, Any], spec_path: Path, policy: dict[str, Any], result: CheckResult
) -> None:
    root = project_root(spec, spec_path, policy, result)
    field = str(get_path(policy, "paths.output_dir_field") or "project.output_dir")
    configured = get_path(spec, field)
    if not string_value(configured):
        result.issue(f"{field} is required for frozen generated outputs")
        return
    output = resolve_from_root(configured, root)
    if get_path(policy, "paths.require_output_under_project_root") is True:
        try:
            output.relative_to(root)
        except ValueError:
            result.issue(f"{field} must stay under project.root_dir: {output}")


def safe_name(value: Any) -> str:
    text = str(value).strip()
    cleaned = "".join(character if character.isalnum() or character in "._-" else "_" for character in text)
    return cleaned.strip("._-") or "project"


def manifest_path(spec: dict[str, Any], spec_path: Path, policy: dict[str, Any], result: CheckResult) -> tuple[Path, Path]:
    root, artifacts = artifacts_root(spec, spec_path, policy, result)
    directory = str(get_path(policy, "paths.manifest_directory"))
    filename = str(get_path(policy, "paths.manifest_filename"))
    if not directory or not filename:
        result.issue("Spec Freeze policy must declare manifest directory and filename")
        return root, artifacts
    project_name = safe_name(get_path(spec, "project.name") or spec_path.stem)
    target = (artifacts / directory / project_name / filename).resolve()
    try:
        target.relative_to(artifacts)
    except ValueError:
        result.issue(f"Spec Freeze manifest must stay under project.artifacts_dir: {target}")
    return root, target


def display_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def require_shapes(spec: dict[str, Any], policy: dict[str, Any], tier: str, result: CheckResult) -> None:
    shapes = mapping_value(policy.get("section_shapes"))
    for section in string_list(get_path(policy, f"required_sections.{tier}")):
        if section not in spec:
            result.issue(f"Spec Freeze requires top-level section: {section}")
            continue
        expected = str(shapes.get(section, ""))
        value = spec.get(section)
        if expected == "mapping" and not isinstance(value, dict):
            result.issue(f"{section} must be a mapping for Spec Freeze")
        elif expected == "list" and not isinstance(value, list):
            result.issue(f"{section} must be a list for Spec Freeze")


def require_fields(spec: dict[str, Any], policy: dict[str, Any], tier: str, result: CheckResult) -> None:
    fields = string_list(get_path(policy, "required_fields.local_mvp"))
    if tier == "production":
        fields.extend(string_list(get_path(policy, "required_fields.production")))
    positive_fields = set(string_list(policy.get("positive_number_fields")))
    for field in fields:
        value = get_path(spec, field)
        if value in (None, "", [], {}):
            result.issue(f"Spec Freeze requires {field}")
            continue
        if field in positive_fields and (
            not isinstance(value, (int, float)) or isinstance(value, bool) or float(value) <= 0
        ):
            result.issue(f"{field} must be a positive number for Spec Freeze")


def check_manufacturing(spec: dict[str, Any], policy: dict[str, Any], result: CheckResult) -> None:
    mode = normalized(get_path(spec, "manufacturing.mode"))
    allowed_modes = {normalized(item) for item in string_list(policy.get("manufacturing_modes"))}
    if mode not in allowed_modes:
        result.issue(f"manufacturing.mode must be one of {', '.join(sorted(allowed_modes))}")
        return
    outputs = {
        normalized(item)
        for item in list_value(get_path(spec, "manufacturing.required_outputs"))
        if string_value(item)
    }
    if not outputs:
        result.issue("manufacturing.required_outputs must be a non-empty list")
        return
    required = {
        normalized(item)
        for item in string_list(get_path(policy, f"manufacturing_required_outputs.{mode}"))
    }
    missing = sorted(required - outputs)
    if missing:
        result.issue(f"manufacturing.required_outputs is missing for {mode}: {', '.join(missing)}")


def check_expected_graph_coverage(spec: dict[str, Any], result: CheckResult) -> None:
    expected = spec.get("expected_net_graph")
    if not isinstance(expected, dict):
        return
    expected_nets = expected.get("nets", expected)
    if not isinstance(expected_nets, dict) or not expected_nets:
        result.issue("expected_net_graph must explicitly cover every declared net before Spec Freeze")
        return
    declared = net_names(spec)
    observed = {str(name) for name in expected_nets}
    missing = sorted(declared - observed)
    extra = sorted(observed - declared)
    if missing:
        result.issue(f"expected_net_graph is missing declared nets: {', '.join(missing)}")
    if extra:
        result.issue(f"expected_net_graph contains undeclared nets: {', '.join(extra)}")
    for name, rule in expected_nets.items():
        if not isinstance(rule, dict):
            continue
        if rule.get("exact") is not True:
            result.issue(f"expected_net_graph.{name}.exact must be true for Spec Freeze")
        pins = rule.get("required_pins", rule.get("pins"))
        if not isinstance(pins, list) or not pins:
            result.issue(f"expected_net_graph.{name} must list required_pins")


def check_verification_dispositions(spec: dict[str, Any], policy: dict[str, Any], result: CheckResult) -> None:
    allowed = {normalized(item) for item in string_list(policy.get("verification_disposition_statuses"))}
    required_fields = string_list(policy.get("verification_disposition_required_fields"))
    verification = mapping_value(spec.get("verification"))
    sections = string_list(get_path(policy, "production_policy_controls.spec_closure_verification_sections"))
    for section in sections:
        value = verification.get(section)
        if not isinstance(value, dict):
            continue
        status = normalized(value.get("status"))
        if status in allowed:
            for field in required_fields:
                if not string_value(value.get(field)):
                    result.issue(f"verification.{section}.{field} is required for status {status}")


def check_board_constraints(spec: dict[str, Any], policy: dict[str, Any], tier: str, result: CheckResult) -> None:
    dispositions = get_path(spec, "board.constraint_dispositions")
    if not isinstance(dispositions, dict):
        result.issue("board.constraint_dispositions must explicitly classify every frozen board constraint area")
        return
    allowed = {normalized(item) for item in string_list(policy.get("board_constraint_statuses"))}
    for area in string_list(get_path(policy, f"board_constraint_areas.{tier}")):
        item = dispositions.get(area)
        label = f"board.constraint_dispositions.{area}"
        if not isinstance(item, dict):
            result.issue(f"{label} must be a mapping")
            continue
        status = normalized(item.get("status"))
        if status not in allowed:
            result.issue(f"{label}.status must be one of {', '.join(sorted(allowed))}")
        if not string_value(item.get("rationale")):
            result.issue(f"{label}.rationale must explain the disposition")
        references = item.get("references", [])
        if status == "defined":
            if not isinstance(references, list) or not references:
                result.issue(f"{label}.references must point to machine-readable constraints")
                continue
            for reference in references:
                if not string_value(reference) or not dotted_exists(spec, str(reference)):
                    result.issue(f"{label}.references points to missing spec data: {reference}")


def check_todo_dispositions(spec: dict[str, Any], policy: dict[str, Any], result: CheckResult) -> None:
    todo_policy = mapping_value(policy.get("todo"))
    closed = {normalized(item) for item in string_list(todo_policy.get("closed_statuses"))}
    dispositions = {normalized(item) for item in string_list(todo_policy.get("explicit_disposition_statuses"))}
    post_fab = {normalized(item) for item in string_list(todo_policy.get("post_fab_categories"))}
    non_blocking_category = normalized(todo_policy.get("non_blocking_category"))
    non_blocking_status = normalized(todo_policy.get("non_blocking_status"))
    not_applicable_status = normalized(todo_policy.get("not_applicable_status"))
    required_fields = string_list(todo_policy.get("required_disposition_fields"))
    for index, item in enumerate(list_value(spec.get("todo"))):
        if not isinstance(item, dict):
            result.issue(f"todo[{index}] must be structured before Spec Freeze")
            continue
        status = normalized(item.get("status"))
        category = normalized(item.get("category"))
        if status in closed:
            continue
        if status == not_applicable_status:
            for field in required_fields:
                if not string_value(item.get(field)):
                    result.issue(f"todo[{index}].{field} is required for a non-blocking/not-applicable disposition")
        elif status == non_blocking_status or item.get("blocking") is False:
            if category not in post_fab and category != non_blocking_category:
                result.issue(
                    f"todo[{index}] may be non-blocking only as {non_blocking_category} or a trusted post-fabrication category"
                )
            for field in required_fields:
                if not string_value(item.get(field)):
                    result.issue(f"todo[{index}].{field} is required for a non-blocking/not-applicable disposition")
        elif status not in dispositions and category not in post_fab and item.get("blocking") is not True:
            result.issue(f"todo[{index}] open pre-fabrication work must declare blocking: true")


def check_production_policy_controls(
    spec: dict[str, Any], policy: dict[str, Any], result: CheckResult, production: bool = False
) -> None:
    is_production = production_required(spec, policy, forced=production)
    controls = mapping_value(policy.get("production_policy_controls"))
    closure = mapping_value(get_path(spec, "validation.spec_closure"))
    required_areas = set(string_list(controls.get("spec_closure_required_areas")))
    configured_areas = set(string_list(closure.get("required_areas")))
    if "required_areas" in closure and not required_areas.issubset(configured_areas):
        result.issue("Spec Freeze validation.spec_closure.required_areas may add checks but must not remove trusted areas")
    required_verification = set(string_list(controls.get("spec_closure_verification_sections")))
    configured_verification = set(string_list(closure.get("required_verification_sections")))
    if "required_verification_sections" in closure and not required_verification.issubset(configured_verification):
        result.issue("Spec Freeze closure may not remove trusted SI/PI/EMC/thermal sections")

    requirement_config = mapping_value(get_path(spec, "validation.requirements"))
    required_requirement_areas = set(string_list(controls.get("requirements_required_areas")))
    configured_requirement_areas = set(string_list(requirement_config.get("required_areas")))
    if "required_areas" in requirement_config and not required_requirement_areas.issubset(configured_requirement_areas):
        result.issue("Spec Freeze validation.requirements.required_areas may not remove trusted requirement areas")
    allowed_statuses = set(string_list(controls.get("requirements_allowed_statuses")))
    configured_statuses = set(string_list(requirement_config.get("accepted_statuses")))
    if "accepted_statuses" in requirement_config and not configured_statuses.issubset(allowed_statuses):
        result.issue("Spec Freeze validation.requirements.accepted_statuses may not add weaker statuses")
    if is_production and requirement_config.get("allow_assumptions") is True:
        result.issue("production requirements may not enable unresolved assumptions")

    readiness_config = mapping_value(get_path(spec, "validation.readiness"))
    if readiness_config.get("enabled") is False:
        result.issue("Spec Freeze may not disable readiness preflight")

    power_config = mapping_value(get_path(spec, "validation.power_budget"))
    minimum_margin = get_path(controls, "minimum_power_margin_percent")
    configured_margin = power_config.get("default_margin_percent")
    if is_production and isinstance(minimum_margin, (int, float)) and isinstance(configured_margin, (int, float)):
        if float(configured_margin) < float(minimum_margin):
            result.issue(
                f"production power margin {configured_margin:g}% is below trusted minimum {float(minimum_margin):g}%"
            )
    if is_production and get_path(controls, "require_power_domain_protection") is True:
        for index, domain in enumerate(list_value(spec.get("power_domains"))):
            if isinstance(domain, dict) and not mapping_value(domain.get("protection")):
                result.issue(f"production power_domains[{index}].protection must be declared")

    todo_config = mapping_value(get_path(spec, "validation.todo_blockers"))
    configured_post_fab = set(string_list(todo_config.get("post_fab_nonblocking_categories")))
    trusted_post_fab = {str(item) for item in string_list(get_path(policy, "todo.post_fab_categories"))}
    if is_production and "post_fab_nonblocking_categories" in todo_config and not configured_post_fab.issubset(trusted_post_fab):
        result.issue("production TODO policy may not classify additional categories as post-fabrication")
    if is_production and get_path(controls, "forbid_single_pin_net_allowlist") is True and list_value(
        get_path(spec, "validation.net_graph.allow_single_pin_nets")
    ):
        result.issue("production Spec Freeze forbids validation.net_graph.allow_single_pin_nets")
    if is_production and get_path(controls, "forbid_generated_extra_pin_allowlist") is True and list_value(
        get_path(spec, "validation.generated_netlist.allow_extra_pins")
    ):
        result.issue("production Spec Freeze forbids validation.generated_netlist.allow_extra_pins")


def check_freeze_contract(
    spec: dict[str, Any], spec_path: Path, policy: dict[str, Any], result: CheckResult, production: bool = False
) -> str:
    tier = freeze_tier(spec, policy, production=production)
    stage = normalized(get_path(spec, "project.stage"))
    allowed_stages = {normalized(item) for item in string_list(policy.get("required_stage_values"))}
    if stage not in allowed_stages:
        result.issue(f"project.stage is not a freeze-capable lifecycle state: {stage or '<missing>'}")
    override_field = str(policy.get("policy_override_field", "validation.spec_freeze.policy_file"))
    if string_value(get_path(spec, override_field)):
        result.issue(f"{override_field} is forbidden when Spec Freeze is required; use the bundled trusted policy")
    for section, message in mapping_value(policy.get("forbidden_duplicate_sections")).items():
        if section in spec:
            result.issue(f"{section} is a duplicate source of truth. {message}")
    require_shapes(spec, policy, tier, result)
    require_fields(spec, policy, tier, result)
    check_manufacturing(spec, policy, result)
    check_expected_graph_coverage(spec, result)
    check_board_constraints(spec, policy, tier, result)
    check_verification_dispositions(spec, policy, result)
    check_todo_dispositions(spec, policy, result)
    check_production_policy_controls(spec, policy, result, production=production)
    check_output_root(spec, spec_path, policy, result)
    artifacts_root(spec, spec_path, policy, result)
    return tier


def artifact_bindings(
    spec: dict[str, Any], spec_path: Path, policy: dict[str, Any], tier: str, result: CheckResult
) -> list[dict[str, Any]]:
    root, _artifacts = artifacts_root(spec, spec_path, policy, result)
    bindings: list[dict[str, Any]] = []
    for item in list_value(policy.get("artifact_bindings")):
        if not isinstance(item, dict) or tier not in string_list(item.get("tiers")):
            continue
        binding_id = str(item.get("id", "artifact"))
        path_field = str(item.get("path_field", ""))
        raw_path = get_path(spec, path_field)
        if not path_field or not string_value(raw_path):
            if item.get("optional") is True and path_field:
                continue
            result.issue(f"Spec Freeze artifact {binding_id} requires {path_field or '<path_field>'}")
            continue
        path = resolve_from_root(raw_path, root)
        if not path.is_file():
            result.issue(f"Spec Freeze artifact {binding_id} does not exist: {path}")
            continue
        digest = sha256_file(path)
        expected_field = item.get("expected_sha256_field")
        if string_value(expected_field):
            expected = get_path(spec, str(expected_field))
            if normalized(expected) != digest:
                result.issue(f"{expected_field} is stale for Spec Freeze artifact {binding_id}")
        bindings.append(
            {
                "id": binding_id,
                "path_field": path_field,
                "path": display_path(path, root),
                "sha256": digest,
            }
        )
    return bindings


def preflight_definitions(policy: dict[str, Any], tier: str) -> list[dict[str, Any]]:
    return [item for item in list_value(get_path(policy, f"preflight.{tier}")) if isinstance(item, dict)]


def run_preflight(
    spec_path: Path, policy: dict[str, Any], tier: str, result: CheckResult
) -> list[dict[str, Any]]:
    scripts_dir = Path(__file__).resolve().parent
    records: list[dict[str, Any]] = []
    for definition in preflight_definitions(policy, tier):
        check_id = str(definition.get("id", "check"))
        script_name = str(definition.get("script", ""))
        args = string_list(definition.get("args"))
        script = scripts_dir / script_name
        if not script_name or not script.is_file():
            result.issue(f"Spec Freeze preflight script is missing for {check_id}: {script}")
            break
        command = [sys.executable, str(script), *args, str(spec_path)]
        completed = subprocess.run(command, text=True, capture_output=True)
        output = completed.stdout + completed.stderr
        record = {
            "id": check_id,
            "script": script_name,
            "args": args,
            "status": "passed" if completed.returncode == 0 else "failed",
            "exit_code": completed.returncode,
            "script_sha256": sha256_file(script),
            "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        }
        records.append(record)
        if completed.returncode != 0:
            details = "\n".join(line for line in output.strip().splitlines()[-12:] if line)
            result.issue(f"Spec Freeze stopped at {check_id}:\n{details or 'check failed without output'}")
            break
    return records


def manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True).encode("utf-8")


def next_revision(spec: dict[str, Any]) -> int:
    value = get_path(spec, "spec_freeze.revision")
    return value + 1 if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 1


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def product_baseline_context(
    spec: dict[str, Any],
    spec_path: Path,
    root: Path,
    status: Any,
    tier: str,
    revision: int,
    frozen_at: str,
    digest: str,
    policy_digest: str,
    renderer_digest: str,
    bindings: list[dict[str, Any]],
    preflight_records: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "project_name": get_path(spec, "project.name"),
        "status": status,
        "tier": tier,
        "freeze_revision": revision,
        "frozen_at": frozen_at,
        "spec_path": display_path(spec_path, root),
        "spec_sha256": digest,
        "policy_sha256": policy_digest,
        "renderer_sha256": renderer_digest,
        "artifact_bindings": copy.deepcopy(bindings),
        "preflight": copy.deepcopy(preflight_records),
    }


def prepare_freeze_transaction(
    spec: dict[str, Any],
    spec_path: Path,
    policy: dict[str, Any],
    result: CheckResult,
    preflight_records: list[dict[str, Any]],
    production: bool = False,
) -> dict[str, Any]:
    tier = check_freeze_contract(spec, spec_path, policy, result, production=production)
    if not result.ok():
        return {"tier": tier, "preflight": preflight_records}
    expected_ids = [str(item.get("id")) for item in preflight_definitions(policy, tier)]
    observed_ids = [str(item.get("id")) for item in preflight_records]
    if observed_ids != expected_ids or any(item.get("status") != "passed" for item in preflight_records):
        result.issue("Spec Freeze preflight evidence is incomplete or out of order")
        return {"tier": tier, "preflight": preflight_records}

    root, target = manifest_path(spec, spec_path, policy, result)
    bindings = artifact_bindings(spec, spec_path, policy, tier, result)
    if not result.ok():
        return {"tier": tier, "preflight": preflight_records}

    revision = next_revision(spec)
    frozen_at = utc_timestamp()
    digest = spec_digest(spec, policy)
    policy_digest = sha256_file(policy_path())
    renderer_digest = sha256_file(renderer_path())
    baseline_target = product_baseline_path(target, policy)
    baseline_context = product_baseline_context(
        spec,
        spec_path,
        root,
        policy.get("freeze_status"),
        tier,
        revision,
        frozen_at,
        digest,
        policy_digest,
        renderer_digest,
        bindings,
        preflight_records,
    )
    baseline_data = render_product_baseline(spec, policy, baseline_context)
    baseline_digest = bytes_sha256(baseline_data)
    manifest = {
        "schema_version": policy.get("manifest_schema_version"),
        "executor_version": policy.get("executor_version"),
        "status": policy.get("freeze_status"),
        "project_name": get_path(spec, "project.name"),
        "tier": tier,
        "freeze_revision": revision,
        "frozen_at": frozen_at,
        "spec": {
            "path": display_path(spec_path, root),
            "sha256": digest,
            "section_sha256": section_digests(spec, policy),
        },
        "policy": {
            "path": display_path(policy_path(), Path(__file__).resolve().parents[1]),
            "sha256": policy_digest,
            "executor_sha256": sha256_file(Path(__file__).resolve()),
        },
        "product_baseline": {
            "schema_version": get_path(policy, "product_baseline.schema_version"),
            "path": display_path(baseline_target, root),
            "sha256": baseline_digest,
            "renderer_sha256": renderer_digest,
        },
        "artifact_bindings": bindings,
        "preflight": copy.deepcopy(preflight_records),
    }
    data = manifest_bytes(manifest)
    manifest_digest = bytes_sha256(data)
    updated = copy.deepcopy(spec)
    updated["spec_freeze"] = {
        "schema_version": policy.get("schema_version"),
        "status": policy.get("freeze_status"),
        "revision": revision,
        "tier": tier,
        "frozen_at": frozen_at,
        "spec_sha256": digest,
        "policy_sha256": policy_digest,
        "manifest": {
            "path": display_path(target, root),
            "sha256": manifest_digest,
        },
        "product_baseline": {
            "schema_version": get_path(policy, "product_baseline.schema_version"),
            "path": display_path(baseline_target, root),
            "sha256": baseline_digest,
            "renderer_sha256": renderer_digest,
        },
    }
    return {
        "tier": tier,
        "manifest": manifest,
        "manifest_path": target,
        "manifest_data": data,
        "product_baseline_path": baseline_target,
        "product_baseline_data": baseline_data,
        "updated_spec": updated,
        "preflight": preflight_records,
    }


def apply_freeze_transaction(spec_path: Path, transaction: dict[str, Any]) -> None:
    target = transaction.get("manifest_path")
    updated = transaction.get("updated_spec")
    data = transaction.get("manifest_data")
    baseline_target = transaction.get("product_baseline_path")
    baseline_data = transaction.get("product_baseline_data")
    if (
        not isinstance(target, Path)
        or not isinstance(updated, dict)
        or not isinstance(data, bytes)
        or not isinstance(baseline_target, Path)
        or not isinstance(baseline_data, bytes)
    ):
        raise ValueError("Spec Freeze transaction is incomplete")
    replace_files_transactionally(
        [
            (baseline_target, baseline_data),
            (target, data),
            (spec_path, roundtrip_spec_bytes(spec_path, updated)),
        ],
        spec_path.with_suffix(spec_path.suffix + ".freeze-txn.json"),
    )


def check_manifest_bindings(
    spec: dict[str, Any], spec_path: Path, policy: dict[str, Any], manifest: dict[str, Any], result: CheckResult
) -> None:
    root, _artifacts = artifacts_root(spec, spec_path, policy, result)
    current: dict[str, dict[str, Any]] = {}
    tier = freeze_tier(spec, policy)
    for binding in artifact_bindings(spec, spec_path, policy, tier, result):
        current[str(binding.get("id"))] = binding
    recorded = {
        str(item.get("id")): item
        for item in list_value(manifest.get("artifact_bindings"))
        if isinstance(item, dict) and string_value(item.get("id"))
    }
    if set(recorded) != set(current):
        result.issue("Spec Freeze artifact binding set is stale")
        return
    for binding_id, expected in current.items():
        observed = recorded[binding_id]
        if observed.get("path") != expected.get("path") or observed.get("sha256") != expected.get("sha256"):
            result.issue(f"Spec Freeze artifact binding is stale: {binding_id}")
        path = resolve_from_root(observed.get("path"), root)
        if not path.is_file() or sha256_file(path) != observed.get("sha256"):
            result.issue(f"Spec Freeze artifact changed after freeze: {binding_id}")


def check_product_baseline_binding(
    spec: dict[str, Any],
    spec_path: Path,
    policy: dict[str, Any],
    freeze: dict[str, Any],
    manifest: dict[str, Any],
    manifest_target: Path,
    root: Path,
    artifacts: Path,
    tier: str,
    current_digest: str,
    current_policy_digest: str,
    result: CheckResult,
    details: dict[str, Any],
) -> None:
    expected_schema = get_path(policy, "product_baseline.schema_version")
    current_renderer_digest = sha256_file(renderer_path())
    expected_target = product_baseline_path(manifest_target, policy)
    try:
        expected_target.relative_to(artifacts)
    except ValueError:
        result.issue(f"Product baseline must stay under project.artifacts_dir: {expected_target}")
        return

    freeze_meta = mapping_value(freeze.get("product_baseline"))
    manifest_meta = mapping_value(manifest.get("product_baseline"))
    expected_display_path = display_path(expected_target, root)
    for owner, metadata in (("spec_freeze", freeze_meta), ("Spec Freeze manifest", manifest_meta)):
        if metadata.get("schema_version") != expected_schema:
            result.issue(f"{owner} product baseline schema_version is stale")
        if metadata.get("path") != expected_display_path:
            result.issue(f"{owner} product baseline path is stale")
        if metadata.get("renderer_sha256") != current_renderer_digest:
            result.issue(f"{owner} product baseline renderer changed; rerun the freeze transaction")

    if freeze_meta.get("sha256") != manifest_meta.get("sha256"):
        result.issue("Spec Freeze product baseline hashes do not match")
    details["product_baseline"] = str(expected_target)
    if not expected_target.is_file():
        result.issue(f"Product baseline is missing: {expected_target}")
        return
    actual_data = expected_target.read_bytes()
    actual_digest = bytes_sha256(actual_data)
    if freeze_meta.get("sha256") != actual_digest or manifest_meta.get("sha256") != actual_digest:
        result.issue("Product baseline changed after freeze")

    revision = manifest.get("freeze_revision")
    frozen_at = manifest.get("frozen_at")
    if not isinstance(revision, int) or isinstance(revision, bool) or not string_value(frozen_at):
        result.issue("Spec Freeze manifest cannot reconstruct the product baseline")
        return
    context = product_baseline_context(
        spec,
        spec_path,
        root,
        manifest.get("status"),
        tier,
        revision,
        str(frozen_at),
        current_digest,
        current_policy_digest,
        current_renderer_digest,
        [item for item in list_value(manifest.get("artifact_bindings")) if isinstance(item, dict)],
        [item for item in list_value(manifest.get("preflight")) if isinstance(item, dict)],
    )
    expected_data = render_product_baseline(spec, policy, context)
    if actual_data != expected_data:
        result.issue("Product baseline is not the deterministic view of the frozen Spec")


def check_frozen_spec(
    spec: dict[str, Any],
    spec_path: Path,
    result: CheckResult,
    require: bool = False,
    production: bool = False,
) -> dict[str, Any]:
    policy = load_policy()
    required = freeze_required(spec, policy, forced=require or production)
    details: dict[str, Any] = {
        "required": required,
        "tier": freeze_tier(spec, policy, production=production),
        "policy": str(policy_path()),
    }
    freeze = spec.get("spec_freeze")
    if not required and not isinstance(freeze, dict):
        result.warning("Spec Freeze is not required for this legacy/draft spec")
        return details
    if not isinstance(freeze, dict):
        result.issue("Spec Freeze is required before KiCad generation; run spec_freeze_transaction.py --apply")
        return details

    tier = check_freeze_contract(spec, spec_path, policy, result, production=production)
    details["tier"] = tier
    expected_status = policy.get("freeze_status")
    if freeze.get("schema_version") != policy.get("schema_version"):
        result.issue("spec_freeze.schema_version is stale")
    if freeze.get("status") != expected_status:
        result.issue(f"spec_freeze.status must be {expected_status}")
    revision = freeze.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        result.issue("spec_freeze.revision must be a positive integer")
    if freeze.get("tier") != tier:
        result.issue(f"spec_freeze.tier is stale; expected {tier}")
    current_digest = spec_digest(spec, policy)
    if freeze.get("spec_sha256") != current_digest:
        result.issue("Spec changed after freeze; rerun spec_freeze_transaction.py --apply")
    current_policy_digest = sha256_file(policy_path())
    if freeze.get("policy_sha256") != current_policy_digest:
        result.issue("Spec Freeze policy changed; rerun spec_freeze_transaction.py --apply")

    root, artifacts = artifacts_root(spec, spec_path, policy, result)
    manifest_meta = mapping_value(freeze.get("manifest"))
    raw_path = manifest_meta.get("path")
    if not string_value(raw_path):
        result.issue("spec_freeze.manifest.path is required")
        return details
    target = resolve_from_root(raw_path, root)
    try:
        target.relative_to(artifacts)
    except ValueError:
        result.issue(f"Spec Freeze manifest must stay under project.artifacts_dir: {target}")
        return details
    details["manifest"] = str(target)
    if not target.is_file():
        result.issue(f"Spec Freeze manifest is missing: {target}")
        return details
    actual_manifest_sha = sha256_file(target)
    if manifest_meta.get("sha256") != actual_manifest_sha:
        result.issue("spec_freeze.manifest.sha256 is stale")
        return details
    manifest = load_yaml(target)
    expected_schema = policy.get("manifest_schema_version")
    if manifest.get("schema_version") != expected_schema:
        result.issue(f"Spec Freeze manifest schema_version must be {expected_schema}")
    if manifest.get("executor_version") != policy.get("executor_version"):
        result.issue("Spec Freeze manifest executor_version is stale")
    if manifest.get("status") != expected_status:
        result.issue(f"Spec Freeze manifest status must be {expected_status}")
    if manifest.get("project_name") != get_path(spec, "project.name"):
        result.issue("Spec Freeze manifest project_name does not match the spec")
    if manifest.get("tier") != tier:
        result.issue("Spec Freeze manifest tier is stale")
    if manifest.get("freeze_revision") != revision:
        result.issue("Spec Freeze manifest revision does not match the spec")
    if manifest.get("frozen_at") != freeze.get("frozen_at"):
        result.issue("Spec Freeze manifest timestamp does not match the spec")
    if get_path(manifest, "spec.sha256") != current_digest:
        result.issue("Spec Freeze manifest does not bind the current spec")
    if mapping_value(get_path(manifest, "spec.section_sha256")) != section_digests(spec, policy):
        result.issue("Spec Freeze section digest set is stale")
    if get_path(manifest, "policy.sha256") != current_policy_digest:
        result.issue("Spec Freeze manifest policy digest is stale")
    if get_path(manifest, "policy.executor_sha256") != sha256_file(Path(__file__).resolve()):
        result.issue("Spec Freeze executor changed; rerun spec_freeze_transaction.py --apply")

    definitions = preflight_definitions(policy, tier)
    recorded_preflight = [item for item in list_value(manifest.get("preflight")) if isinstance(item, dict)]
    if len(recorded_preflight) != len(definitions):
        result.issue("Spec Freeze manifest preflight set is incomplete or stale")
    else:
        scripts_dir = Path(__file__).resolve().parent
        for definition, record in zip(definitions, recorded_preflight):
            expected_id = str(definition.get("id"))
            expected_script = str(definition.get("script"))
            expected_args = string_list(definition.get("args"))
            script = scripts_dir / expected_script
            if (
                record.get("id") != expected_id
                or record.get("script") != expected_script
                or record.get("args") != expected_args
                or record.get("status") != "passed"
                or record.get("exit_code") != 0
            ):
                result.issue(f"Spec Freeze preflight record is stale: {expected_id}")
                continue
            if not script.is_file() or record.get("script_sha256") != sha256_file(script):
                result.issue(f"Spec Freeze preflight executor changed: {expected_id}")
    check_manifest_bindings(spec, spec_path, policy, manifest, result)
    check_product_baseline_binding(
        spec,
        spec_path,
        policy,
        freeze,
        manifest,
        target,
        root,
        artifacts,
        tier,
        current_digest,
        current_policy_digest,
        result,
        details,
    )
    return details
