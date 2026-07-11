#!/usr/bin/env python3
"""CLI for the package binding stage before PCB generation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _package_binding_stage import check_stage_evidence, validate_contract, write_stage_evidence  # noqa: E402
from _pcb_skill_checks import CheckResult, load_spec, print_result  # noqa: E402


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Validate semantic pin maps, footprint geometry, provenance, and orientation.")
    parser.add_argument("spec", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check-contract", action="store_true")
    mode.add_argument("--before-board", action="store_true")
    mode.add_argument("--check-evidence", action="store_true")
    parser.add_argument("--require", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])
    result = CheckResult()
    details: dict = {}
    try:
        spec = load_spec(args.spec)
        if args.check_contract:
            details = validate_contract(spec, args.spec, result, args.require)
        elif args.before_board:
            details = write_stage_evidence(spec, args.spec, result, args.require)
        else:
            details = check_stage_evidence(spec, args.spec, result, args.require)
    except Exception as error:
        result.issue(str(error))
    payload = {"check": "package_binding_stage_gate", "ok": result.ok(), "issues": result.issues, "warnings": result.warnings, "details": details}
    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if result.ok() else 1
    return print_result("package_binding_stage_gate", result, False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
