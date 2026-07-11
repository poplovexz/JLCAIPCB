#!/usr/bin/env python3
"""Run spec-driven readiness gates before KiCad generation or release checks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pcb_skill_checks import CheckResult, load_spec, print_result  # noqa: E402
from _readiness_checks import check_power_domains, check_requirements, check_todo_blockers  # noqa: E402


PRODUCTION_STAGE_VALUES = {
    "fabrication",
    "order-ready",
    "order_ready",
    "production",
    "production-package",
    "production_package",
    "release",
}


def mapping_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def validation_config(spec: dict[str, Any], key: str) -> dict[str, Any]:
    validation = mapping_value(spec.get("validation"))
    return mapping_value(validation.get(key))


def readiness_config(spec: dict[str, Any]) -> dict[str, Any]:
    return validation_config(spec, "readiness")


def has_jlcpcb_release(spec: dict[str, Any]) -> bool:
    release = (
        mapping_value(spec.get("manufacturing"))
        .get("jlcpcb", {})
    )
    return isinstance(release, dict) and isinstance(release.get("release"), dict)


def is_production_candidate(spec: dict[str, Any], forced: bool = False) -> bool:
    if forced:
        return True
    project_stage = str(mapping_value(spec.get("project")).get("stage", "")).strip().lower()
    manufacturing = mapping_value(spec.get("manufacturing"))
    return (
        project_stage in PRODUCTION_STAGE_VALUES
        or bool(manufacturing.get("production_ready"))
        or bool(manufacturing.get("order_ready"))
        or has_jlcpcb_release(spec)
    )


def bool_config(config: dict[str, Any], key: str, default: bool = False) -> bool:
    value = config.get(key)
    if value is None:
        return default
    return bool(value)


def run_preflight(spec: dict[str, Any], production: bool = False) -> tuple[CheckResult, list[str]]:
    result = CheckResult()
    executed: list[str] = []
    readiness = readiness_config(spec)
    if bool_config(readiness, "enabled", True) is False:
        return result, executed

    production_candidate = is_production_candidate(spec, forced=production)
    requirements_required = (
        "requirements" in spec
        or bool_config(validation_config(spec, "requirements"), "required")
        or bool_config(readiness, "requirements_required")
        or (production_candidate and bool_config(readiness, "require_requirements_for_production", True))
    )
    power_required = (
        "power_domains" in spec
        or bool_config(validation_config(spec, "power_budget"), "required")
        or bool_config(readiness, "power_budget_required")
    )
    todo_required = (
        "todo" in spec
        and (
            production_candidate
            or bool_config(validation_config(spec, "todo_blockers"), "required")
            or bool_config(readiness, "todo_blockers_required")
        )
    )

    if requirements_required:
        executed.append("requirements_gate --strict")
        check_requirements(spec, result, strict=True)
    if power_required:
        executed.append("power_budget_check --strict")
        check_power_domains(spec, result, strict=True)
    if todo_required:
        executed.append("todo_blocker_check --production")
        check_todo_blockers(spec, result, production=True)
    return result, executed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run spec-driven production readiness preflight checks.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])

    result = CheckResult()
    executed: list[str] = []
    try:
        spec = load_spec(args.spec)
        result, executed = run_preflight(spec, production=args.production)
    except Exception as error:
        result.issue(str(error))

    if args.json_output:
        print(json.dumps({"name": "readiness_preflight", "ok": result.ok, "issues": result.issues, "executed": executed}, indent=2))
        return 0 if result.ok else 1
    if executed:
        print("readiness_preflight executed: " + ", ".join(executed))
    else:
        print("readiness_preflight executed: no readiness gates requested")
    return print_result("readiness_preflight", result, False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
