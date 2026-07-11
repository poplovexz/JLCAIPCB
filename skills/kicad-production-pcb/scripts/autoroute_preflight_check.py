#!/usr/bin/env python3
"""Validate a KiCad project before route deletion, DSN export, or Freerouting."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pcb_skill_checks import CheckResult, get_path, load_spec, load_yaml, print_result, string_value  # noqa: E402


CATEGORY_PATTERN = re.compile(r"^\s*\[([^\]]+)\]")
URI_PATTERN = re.compile(r"\(uri\s+(\"[^\"]+\"|[^\s\)]+)\)")
NET_IN_ITEM_PATTERN = re.compile(r"\[([^\]]+)\]")


@dataclass
class ProjectFiles:
    name: str
    project_dir: Path
    schematic: Path | None
    pcb: Path | None


def mapping_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def policy_path(spec: dict[str, Any] | None) -> Path:
    configured = get_path(spec or {}, "validation.autoroute_preflight.policy_file")
    if string_value(configured):
        path = Path(str(configured))
        return path if path.is_absolute() else Path.cwd() / path
    return Path(__file__).resolve().parents[1] / "assets" / "autoroute-preflight-policy.yaml"


def load_policy(spec: dict[str, Any] | None) -> dict[str, Any]:
    try:
        return load_yaml(policy_path(spec))
    except (OSError, ValueError, yaml.YAMLError):
        return {}


def active_for_spec(spec: dict[str, Any], force: bool) -> bool:
    if force:
        return True
    if get_path(spec, "routing.freerouting.enabled") is True:
        return True
    if get_path(spec, "validation.autoroute_preflight.required") is True:
        return True
    return False


def project_name_from_spec(spec: dict[str, Any], spec_path: Path) -> str:
    value = get_path(spec, "project.name")
    return str(value).strip() if string_value(value) else spec_path.stem


def project_from_spec(spec: dict[str, Any], spec_path: Path) -> ProjectFiles | None:
    output_dir = get_path(spec, "project.output_dir")
    name = project_name_from_spec(spec, spec_path)
    if not string_value(output_dir):
        return None
    project_dir = Path(str(output_dir))
    return ProjectFiles(
        name=name,
        project_dir=project_dir,
        schematic=project_dir / f"{name}.kicad_sch",
        pcb=project_dir / f"{name}.kicad_pcb",
    )


def single_match(project_dir: Path, pattern: str) -> Path | None:
    matches = sorted(project_dir.glob(pattern))
    return matches[0] if matches else None


def project_from_dir(project_dir: Path) -> ProjectFiles:
    project_file = single_match(project_dir, "*.kicad_pro")
    name = project_file.stem if project_file else project_dir.name
    return ProjectFiles(
        name=name,
        project_dir=project_dir,
        schematic=single_match(project_dir, "*.kicad_sch"),
        pcb=single_match(project_dir, "*.kicad_pcb"),
    )


def default_output_dir(spec: dict[str, Any] | None, project: ProjectFiles) -> Path:
    artifacts = get_path(spec or {}, "project.artifacts_dir")
    root = Path(str(artifacts)) if string_value(artifacts) else Path("artifacts")
    return root / "autoroute_preflight" / project.name


def resolve_configured_path(spec_path: Path | None, value: Any) -> Path | None:
    if not string_value(value):
        return None
    path = Path(str(value))
    if path.is_absolute():
        return path
    if spec_path is not None:
        candidate = spec_path.parent / path
        if candidate.exists():
            return candidate.resolve()
    return (Path.cwd() / path).resolve()


def resolve_uri(project_dir: Path, uri: str) -> Path | None:
    text = uri.strip().strip('"')
    if not text:
        return None
    text = text.replace("${KIPRJMOD}", str(project_dir))
    expanded = os.path.expandvars(os.path.expanduser(text))
    if "$" in expanded or "://" in expanded:
        return None
    path = Path(expanded)
    return path if path.is_absolute() else project_dir / path


def check_library_tables(project: ProjectFiles, policy: dict[str, Any], result: CheckResult) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for table_name in list_value(policy.get("local_library_tables")):
        if not isinstance(table_name, str) or not table_name.strip():
            continue
        table = project.project_dir / table_name
        if not table.exists():
            continue
        text = table.read_text(encoding="utf-8", errors="replace")
        for match in URI_PATTERN.finditer(text):
            uri = match.group(1).strip('"')
            path = resolve_uri(project.project_dir, uri)
            if path is None:
                entries.append({"table": table_name, "uri": uri, "checked": False, "exists": None})
                continue
            exists = path.exists()
            entries.append({"table": table_name, "uri": uri, "path": str(path), "checked": True, "exists": exists})
            if not exists:
                result.issue(f"{table_name} declares missing local library path: {uri} -> {path}")
    return entries


def parse_categories(report: Path | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    if report is None or not report.exists():
        return counts
    for line in report.read_text(encoding="utf-8", errors="replace").splitlines():
        match = CATEGORY_PATTERN.match(line)
        if match:
            category = match.group(1).strip()
            counts[category] = counts.get(category, 0) + 1
    return dict(sorted(counts.items()))


def run_kicad(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(command, text=True, capture_output=True)
    return {
        "command": command,
        "exit_code": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def produce_reports(project: ProjectFiles, output_dir: Path, result: CheckResult) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    details: dict[str, Any] = {}
    if project.schematic is None or not project.schematic.exists():
        result.issue("Missing KiCad schematic file for autoroute preflight")
    else:
        erc_report = output_dir / "erc.rpt"
        details["erc_report"] = str(erc_report)
        details["erc_run"] = run_kicad(["kicad-cli", "sch", "erc", "--severity-all", "--output", str(erc_report), str(project.schematic)])
    if project.pcb is None or not project.pcb.exists():
        result.issue("Missing KiCad PCB file for autoroute preflight")
    else:
        drc_report = output_dir / "drc.rpt"
        details["drc_report"] = str(drc_report)
        details["drc_run"] = run_kicad(["kicad-cli", "pcb", "drc", "--severity-all", "--output", str(drc_report), str(project.pcb)])
    return details


def blocking_categories(spec: dict[str, Any] | None, policy: dict[str, Any], key: str) -> set[str]:
    configured = get_path(spec or {}, f"validation.autoroute_preflight.{key}")
    values = configured if isinstance(configured, list) else policy.get(key, [])
    return {str(item) for item in values if isinstance(item, str) and item.strip()}


def check_category_blockers(
    label: str,
    counts: dict[str, int],
    blocked: set[str],
    result: CheckResult,
) -> None:
    for category in sorted(blocked):
        count = counts.get(category, 0)
        if count:
            result.issue(f"{label} contains blocking autoroute input category [{category}] x{count}")


def check_planned_unconnected(spec: dict[str, Any] | None, project: ProjectFiles, output_dir: Path, result: CheckResult) -> dict[str, Any]:
    report = output_dir / "drc-unconnected.json"
    run = run_kicad(["kicad-cli", "pcb", "drc", "--format", "json", "--severity-all", "--output", str(report), str(project.pcb)])
    details: dict[str, Any] = {"run": run, "report": str(report)}
    if run["exit_code"] or not report.is_file():
        result.issue("Cannot inspect autoroute preflight unconnected items")
        return details
    payload = json.loads(report.read_text(encoding="utf-8"))
    items = list_value(payload.get("unconnected_items"))
    details["count"] = len(items)
    routing = mapping_value((spec or {}).get("routing"))
    if not routing:
        if items:
            result.issue(f"PCB has {len(items)} unconnected item(s) without a declared routing plan")
        return details
    allowed = {
        net
        for batch in list_value(routing.get("batches"))
        if isinstance(batch, dict) and str(batch.get("state", "")) == "planned"
        for net in list_value(batch.get("nets"))
        if isinstance(net, str)
    }
    unexpected: set[str] = set()
    for item in items:
        descriptions = [str(entry.get("description", "")) for entry in list_value(item.get("items")) if isinstance(entry, dict)] if isinstance(item, dict) else []
        nets = {match.group(1) for description in descriptions for match in NET_IN_ITEM_PATTERN.finditer(description)}
        if not nets or not nets <= allowed:
            unexpected.update(nets or {"<unknown>"})
    if unexpected:
        result.issue("autoroute preflight has unconnected nets outside planned routing batches: " + ", ".join(sorted(unexpected)))
    details["allowed_planned_nets"] = sorted(allowed)
    details["unexpected_nets"] = sorted(unexpected)
    return details


def report_path_from_details(details: dict[str, Any], key: str) -> Path | None:
    value = details.get(key)
    return Path(str(value)) if isinstance(value, str) and value.strip() else None


def check_preflight(
    spec: dict[str, Any] | None,
    spec_path: Path | None,
    project: ProjectFiles | None,
    output_dir: Path | None,
    result: CheckResult,
) -> dict[str, Any]:
    policy = load_policy(spec)
    details: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "policy": str(policy_path(spec)),
    }
    if project is None:
        result.issue("Cannot identify KiCad project directory for autoroute preflight")
        return details
    details["project"] = {
        "name": project.name,
        "project_dir": str(project.project_dir),
        "schematic": str(project.schematic) if project.schematic else None,
        "pcb": str(project.pcb) if project.pcb else None,
    }

    skip_library_tables = get_path(spec or {}, "validation.autoroute_preflight.skip_library_table_check") is True
    if not skip_library_tables:
        details["library_tables"] = check_library_tables(project, policy, result)

    validation = mapping_value(get_path(spec or {}, "validation.autoroute_preflight"))
    erc_report = resolve_configured_path(spec_path, validation.get("erc_report"))
    drc_report = resolve_configured_path(spec_path, validation.get("drc_report"))
    if erc_report or drc_report:
        details["erc_report"] = str(erc_report) if erc_report else None
        details["drc_report"] = str(drc_report) if drc_report else None
        if erc_report and not erc_report.exists():
            result.issue(f"Configured ERC report does not exist: {erc_report}")
        if drc_report and not drc_report.exists():
            result.issue(f"Configured DRC report does not exist: {drc_report}")
    else:
        target_output = output_dir or default_output_dir(spec, project)
        details.update(produce_reports(project, target_output, result))
        erc_report = report_path_from_details(details, "erc_report")
        drc_report = report_path_from_details(details, "drc_report")

    erc_counts = parse_categories(erc_report)
    drc_counts = parse_categories(drc_report)
    details["erc_categories"] = erc_counts
    details["drc_categories"] = drc_counts
    check_category_blockers("ERC report", erc_counts, blocking_categories(spec, policy, "blocking_erc_categories"), result)
    check_category_blockers("DRC report", drc_counts, blocking_categories(spec, policy, "blocking_drc_categories"), result)
    if project.pcb and project.pcb.is_file():
        details["unconnected_plan_check"] = check_planned_unconnected(spec, project, output_dir or default_output_dir(spec, project), result)

    manifest_path = (output_dir or default_output_dir(spec, project)) / "autoroute_preflight.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "check": "autoroute_preflight_check",
        "ok": result.ok(),
        "issues": result.issues,
        "warnings": result.warnings,
        "details": details,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    details["manifest"] = str(manifest_path)
    return details


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Validate KiCad project inputs before Freerouting or route deletion.")
    parser.add_argument("spec", nargs="?", type=Path)
    parser.add_argument("--project-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--require", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])

    result = CheckResult()
    details: dict[str, Any] = {}
    try:
        spec: dict[str, Any] | None = None
        spec_path: Path | None = None
        project: ProjectFiles | None = None
        active = args.require or args.project_dir is not None
        if args.spec:
            spec_path = args.spec
            spec = load_spec(args.spec)
            active = active or active_for_spec(spec, args.require)
            project = project_from_spec(spec, args.spec)
        if args.project_dir:
            project = project_from_dir(args.project_dir)
        if not active:
            result.warning("autoroute preflight not required for this spec")
            details = {"enabled": False}
        else:
            details = check_preflight(spec, spec_path, project, args.output_dir, result)
    except FileNotFoundError as error:
        result.issue(f"Required command or file not found: {error}")
    except Exception as error:
        result.issue(str(error))

    if args.json_output:
        payload = {
            "check": "autoroute_preflight_check",
            "ok": result.ok(),
            "issues": result.issues,
            "warnings": result.warnings,
            "details": details,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if result.ok() else 1
    if details.get("manifest"):
        print(f"autoroute_preflight_check manifest: {details['manifest']}")
    return print_result("autoroute_preflight_check", result, False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
