#!/usr/bin/env python3
"""Validate bounded candidate batches, evidence, inventory, and hard constraints."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pcb_skill_checks import CheckResult, load_spec, print_result  # noqa: E402
from _sourcing_stage import evaluate_candidate_manifest  # noqa: E402


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Validate sourcing candidate batches.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--require", action="store_true")
    parser.add_argument("--as-of")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])
    result = CheckResult()
    details = {}
    try:
        details = evaluate_candidate_manifest(
            load_spec(args.spec), args.spec, result, force=args.require, as_of=args.as_of
        )
    except Exception as error:
        result.issue(str(error))
    if args.json_output:
        print(json.dumps({"check": "candidate_batch_check", "ok": result.ok(), "issues": result.issues, "warnings": result.warnings, "details": details}, indent=2, sort_keys=True))
        return 0 if result.ok() else 1
    return print_result("candidate_batch_check", result)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

