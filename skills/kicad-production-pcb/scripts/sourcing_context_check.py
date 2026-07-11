#!/usr/bin/env python3
"""Validate the bounded sourcing context and requirement decomposition."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pcb_skill_checks import CheckResult, load_spec, print_result  # noqa: E402
from _sourcing_stage import check_sourcing_context  # noqa: E402


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Validate sourcing context and machine-readable requirements.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--require", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])
    result = CheckResult()
    details = {}
    try:
        details = check_sourcing_context(load_spec(args.spec), result, force=args.require, spec_path=args.spec)
    except Exception as error:
        result.issue(str(error))
    if args.json_output:
        print(json.dumps({"check": "sourcing_context_check", "ok": result.ok(), "issues": result.issues, "warnings": result.warnings, "details": details}, indent=2, sort_keys=True))
        return 0 if result.ok() else 1
    return print_result("sourcing_context_check", result)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
