#!/usr/bin/env python3
"""Gate schematic generation, exact KiCad nets, strict ERC, and stage evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pcb_skill_checks import CheckResult, load_spec, print_result  # noqa: E402
from _schematic_stage import check_contract, check_evidence, load_policy, run_after_generation  # noqa: E402


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run the independent schematic-stage production gate.")
    parser.add_argument("spec", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--before-generation", action="store_true")
    mode.add_argument("--after-generation", action="store_true")
    mode.add_argument("--check-evidence", action="store_true")
    parser.add_argument("--generator", type=Path)
    parser.add_argument("--require", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])

    result = CheckResult()
    details: dict = {}
    try:
        spec = load_spec(args.spec)
        if args.before_generation:
            details = check_contract(spec, args.spec, load_policy(), result, force=args.require)
        elif args.after_generation:
            if args.generator is None:
                result.issue("--generator is required after schematic generation")
            else:
                details = run_after_generation(spec, args.spec, args.generator.resolve(), result, force=args.require)
        else:
            details = check_evidence(spec, args.spec, result, force=args.require)
    except Exception as error:
        result.issue(str(error))

    payload = {
        "check": "schematic_stage_gate",
        "ok": result.ok(),
        "issues": result.issues,
        "warnings": result.warnings,
        "details": details,
    }
    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if result.ok() else 1
    return print_result("schematic_stage_gate", result, False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
