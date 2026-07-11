#!/usr/bin/env python3
"""Render a deterministic human handoff from a machine-readable PCB Spec."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from _pcb_skill_checks import get_path


_MISSING = object()


def mapping_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def present(value: Any) -> bool:
    return value is not _MISSING and value not in (None, "", [], {})


def nested_value(value: Any, dotted_path: str) -> Any:
    current = value
    for part in dotted_path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def first_spec_value(spec: dict[str, Any], definition: dict[str, Any]) -> Any:
    configured = definition.get("paths")
    paths = [str(item) for item in configured] if isinstance(configured, list) else []
    if isinstance(definition.get("path"), str):
        paths.insert(0, str(definition["path"]))
    for path in paths:
        value = get_path(spec, path)
        if present(value):
            return value
    return _MISSING


def scalar_text(value: Any) -> str:
    if value is _MISSING or value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, list):
        return ", ".join(scalar_text(item) for item in value)
    if isinstance(value, dict):
        return "; ".join(f"{key}: {scalar_text(item)}" for key, item in value.items())
    return str(value)


def compact_text(value: Any, maximum: int) -> str:
    text = " ".join(scalar_text(value).split())
    if maximum > 3 and len(text) > maximum:
        return text[: maximum - 3].rstrip() + "..."
    return text


def markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")


def markdown_table(columns: list[str], rows: list[list[Any]]) -> list[str]:
    escaped_columns = [markdown_cell(column) for column in columns]
    lines = ["| " + " | ".join(escaped_columns) + " |"]
    lines.append("| " + " | ".join("---" for _column in escaped_columns) + " |")
    for row in rows:
        lines.append("| " + " | ".join(markdown_cell(item) for item in row) + " |")
    return lines


def comparable(value: Any) -> Any:
    return value.strip().lower().replace("_", "-") if isinstance(value, str) else value


def condition_matches(item: Any, condition: dict[str, Any]) -> bool:
    path = str(condition.get("path", ""))
    observed = nested_value(item, path) if path else item
    if observed is _MISSING:
        return False
    normalized = comparable(observed)
    if "equals" in condition:
        return normalized == comparable(condition.get("equals"))
    allowed = condition.get("in")
    if isinstance(allowed, list):
        return normalized in {comparable(value) for value in allowed}
    return bool(observed)


def count_where(value: Any, definition: dict[str, Any]) -> int:
    total = 0
    for item in list_value(value):
        required = [entry for entry in list_value(definition.get("where_all")) if isinstance(entry, dict)]
        excluded = [entry for entry in list_value(definition.get("where_none")) if isinstance(entry, dict)]
        if all(condition_matches(item, entry) for entry in required) and not any(
            condition_matches(item, entry) for entry in excluded
        ):
            total += 1
    return total


def named_items(value: Any, definition: dict[str, Any]) -> str:
    paths = [str(item) for item in list_value(definition.get("item_paths"))]
    names: list[str] = []
    for item in list_value(value):
        selected = _MISSING
        for path in paths:
            candidate = nested_value(item, path)
            if present(candidate):
                selected = candidate
                break
        names.append(scalar_text(selected if selected is not _MISSING else item))
    return ", ".join(name for name in names if name)


def mapping_fields(value: Any, definition: dict[str, Any]) -> str:
    if not isinstance(value, dict):
        return scalar_text(value)
    rendered: list[str] = []
    for field in list_value(definition.get("fields")):
        if not isinstance(field, dict):
            continue
        candidate = nested_value(value, str(field.get("path", "")))
        if present(candidate):
            rendered.append(f"{field.get('label')}: {scalar_text(candidate)}")
    return "; ".join(rendered)


class SafeFormat(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def template_value(spec: dict[str, Any], definition: dict[str, Any], unknown: str) -> str:
    values: dict[str, str] = {}
    for key, path in mapping_value(definition.get("values")).items():
        candidate = get_path(spec, str(path))
        values[str(key)] = scalar_text(candidate) if present(candidate) else unknown
    return str(definition.get("template", "")).format_map(SafeFormat(values))


def brief_value(spec: dict[str, Any], definition: dict[str, Any], unknown: str, maximum: int) -> str:
    mode = str(definition.get("mode", "scalar"))
    if mode == "template":
        rendered = template_value(spec, definition, unknown)
    else:
        value = first_spec_value(spec, definition)
        if not present(value):
            if definition.get("required") is True:
                raise ValueError(f"Product baseline requires brief field: {definition.get('label')}")
            return unknown
        if mode == "scalar":
            rendered = scalar_text(value)
        elif mode == "count":
            rendered = str(len(value)) if isinstance(value, (list, dict)) else "1"
        elif mode == "names":
            rendered = named_items(value, definition)
        elif mode == "mapping_fields":
            rendered = mapping_fields(value, definition)
        elif mode == "count_where":
            rendered = str(count_where(value, definition))
        else:
            raise ValueError(f"Unsupported product baseline brief mode: {mode}")
    return compact_text(rendered or unknown, maximum)


def yaml_compatible(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): yaml_compatible(item) for key, item in value.items()}
    if isinstance(value, list):
        return [yaml_compatible(item) for item in value]
    if isinstance(value, tuple):
        return [yaml_compatible(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def fenced_yaml(value: Any) -> list[str]:
    payload = yaml.safe_dump(
        yaml_compatible(value),
        sort_keys=True,
        allow_unicode=True,
        default_flow_style=False,
    ).rstrip()
    fence = "```"
    while fence in payload:
        fence += "`"
    return [f"{fence}yaml", payload, fence]


def configured_filename(policy: dict[str, Any], preview: bool = False) -> str:
    config = mapping_value(policy.get("product_baseline"))
    key = "preview_filename" if preview else "filename"
    raw = config.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"Spec Freeze policy must declare product_baseline.{key}")
    filename = Path(raw.strip())
    if filename.is_absolute() or filename.name != raw.strip() or raw.strip() in {".", ".."}:
        raise ValueError(f"product_baseline.{key} must be a plain filename")
    return raw.strip()


def product_baseline_path(manifest_target: Path, policy: dict[str, Any], preview: bool = False) -> Path:
    return (manifest_target.parent / configured_filename(policy, preview=preview)).resolve()


def renderer_path() -> Path:
    return Path(__file__).resolve()


def render_product_baseline(
    spec: dict[str, Any],
    policy: dict[str, Any],
    context: dict[str, Any],
) -> bytes:
    config = mapping_value(policy.get("product_baseline"))
    unknown = str(config.get("unknown_text", ""))
    maximum = config.get("max_brief_chars")
    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 32:
        raise ValueError("product_baseline.max_brief_chars must be an integer of at least 32")

    project_name = scalar_text(context.get("project_name") or get_path(spec, "project.name"))
    title = str(config.get("title_template", "{project_name}")).format_map(
        SafeFormat({"project_name": project_name})
    )
    lines = [f"# {title}", "", f"> {config.get('generated_notice', '')}", ""]

    metadata_rows: list[list[Any]] = []
    for field in list_value(config.get("metadata_fields")):
        if not isinstance(field, dict):
            continue
        metadata_rows.append(
            [field.get("label", ""), scalar_text(context.get(str(field.get("context_key", "")), unknown))]
        )
    lines.extend([f"## {config.get('metadata_heading', '')}", ""])
    lines.extend(markdown_table(["Field", "Value"], metadata_rows))

    brief = mapping_value(config.get("build_brief"))
    lines.extend(["", f"## {brief.get('heading', '')}", "", str(brief.get("intro", "")), ""])
    brief_rows: list[list[Any]] = []
    for field in list_value(brief.get("fields")):
        if isinstance(field, dict):
            brief_rows.append([field.get("label", ""), brief_value(spec, field, unknown, maximum)])
    columns = [str(item) for item in list_value(brief.get("columns"))]
    if len(columns) != 2:
        raise ValueError("product_baseline.build_brief.columns must contain two labels")
    lines.extend(markdown_table(columns, brief_rows))

    for section in list_value(config.get("detail_sections")):
        if not isinstance(section, dict):
            continue
        rendered_sources: list[tuple[dict[str, Any], Any]] = []
        for source in list_value(section.get("sources")):
            if not isinstance(source, dict):
                continue
            value = get_path(spec, str(source.get("path", "")))
            if present(value):
                rendered_sources.append((source, value))
        if not rendered_sources:
            continue
        lines.extend(["", f"## {section.get('heading', '')}", "", str(section.get("intro", ""))])
        for source, value in rendered_sources:
            source_path = str(source.get("path", ""))
            lines.extend(["", f"### {source.get('label', '')}", "", f"Spec path: `{source_path}`", ""])
            lines.extend(fenced_yaml(value))

    lines.extend(["", f"## {config.get('evidence_heading', '')}", ""])
    bindings = [item for item in list_value(context.get("artifact_bindings")) if isinstance(item, dict)]
    if bindings:
        evidence_rows = [
            [item.get("id", ""), item.get("path", ""), item.get("sha256", "")] for item in bindings
        ]
        lines.extend(markdown_table(["Evidence", "Local path", "SHA256"], evidence_rows))
    else:
        lines.append(str(config.get("empty_evidence_text", "")))

    lines.extend(["", f"## {config.get('preflight_heading', '')}", ""])
    preflight = [item for item in list_value(context.get("preflight")) if isinstance(item, dict)]
    if preflight:
        preflight_rows = [
            [item.get("id", ""), item.get("status", ""), item.get("script", ""), item.get("script_sha256", "")]
            for item in preflight
        ]
        lines.extend(markdown_table(["Gate", "Status", "Executor", "Executor SHA256"], preflight_rows))
    else:
        lines.append(str(config.get("empty_preflight_text", "")))

    lines.extend(["", f"## {config.get('handoff_heading', '')}", ""])
    for rule in list_value(config.get("handoff_rules")):
        lines.append(f"- {rule}")
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")
