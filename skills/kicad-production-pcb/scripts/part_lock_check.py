#!/usr/bin/env python3
"""Verify part-lock integrity, freshness, ranking, and spec bindings."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _part_lock import check_part_lock  # noqa: E402
from _pcb_skill_checks import CheckResult, load_spec, print_result  # noqa: E402


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Verify the sourcing part lock.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--require", action="store_true")
    parser.add_argument("--as-of")
    parser.add_argument("--before-generation", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])
    result = CheckResult()
    details = {}
    try:
        details = check_part_lock(
            load_spec(args.spec),
            args.spec,
            result,
            force=args.require,
            as_of=args.as_of,
            before_generation=args.before_generation,
        )
    except Exception as error:
        result.issue(str(error))
    if args.json_output:
        print(json.dumps({"check": "part_lock_check", "ok": result.ok(), "issues": result.issues, "warnings": result.warnings, "details": details}, indent=2, sort_keys=True))
        return 0 if result.ok() else 1
    return print_result("part_lock_check", result)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
