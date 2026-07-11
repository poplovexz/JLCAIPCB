#!/usr/bin/env python3
"""Require a current local Spec Freeze manifest before KiCad generation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pcb_skill_checks import CheckResult, load_spec, print_result  # noqa: E402
from _spec_freeze import check_frozen_spec  # noqa: E402


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Validate the current local Spec Freeze manifest.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--before-generation", action="store_true")
    parser.add_argument("--require", action="store_true")
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])

    result = CheckResult()
    details: dict = {}
    try:
        details = check_frozen_spec(
            load_spec(args.spec),
            args.spec,
            result,
            require=args.require,
            production=args.production,
        )
    except Exception as error:
        result.issue(str(error))
    if args.json_output:
        print(
            json.dumps(
                {
                    "check": "spec_freeze_check",
                    "ok": result.ok(),
                    "issues": result.issues,
                    "warnings": result.warnings,
                    "details": details,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if result.ok() else 1
    if details:
        print(
            "spec_freeze_check state: "
            f"required={details.get('required')} tier={details.get('tier')} "
            f"manifest={details.get('manifest') or '<none>'}"
        )
    return print_result("spec_freeze_check", result, False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
