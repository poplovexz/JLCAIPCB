#!/usr/bin/env python3
"""Run or verify strict local ERC/DRC evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _local_validation import check_evidence, run_validation  # noqa: E402
from _pcb_skill_checks import CheckResult, load_spec, print_result  # noqa: E402


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run strict ERC/DRC and bind zero-violation evidence to current inputs.")
    parser.add_argument("spec", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--check-evidence", action="store_true")
    parser.add_argument("--require", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])
    result = CheckResult()
    details = {}
    try:
        spec = load_spec(args.spec)
        details = run_validation(spec, args.spec, result, args.require) if args.run else check_evidence(spec, args.spec, result, args.require)
    except Exception as error:
        result.issue(str(error))
    payload = {"check": "local_validation_gate", "ok": result.ok(), "issues": result.issues, "warnings": result.warnings, "details": details}
    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if result.ok() else 1
    return print_result("local_validation_gate", result, False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
