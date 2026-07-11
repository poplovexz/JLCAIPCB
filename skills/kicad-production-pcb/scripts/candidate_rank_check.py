#!/usr/bin/env python3
"""Recompute deterministic candidate ranking and write an auditable report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pcb_skill_checks import CheckResult, load_spec, print_result  # noqa: E402
from _sourcing_stage import (  # noqa: E402
    atomic_write_yaml,
    configured_artifact_path,
    evaluate_candidate_manifest,
    load_policy,
    project_root,
    ranking_report,
)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Validate and rank sourcing candidates.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--require", action="store_true")
    parser.add_argument("--as-of")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])
    result = CheckResult()
    details = {}
    output = None
    try:
        spec = load_spec(args.spec)
        policy = load_policy(spec)
        details = evaluate_candidate_manifest(
            spec, args.spec, result, policy=policy, force=args.require, as_of=args.as_of
        )
        if result.ok() and details.get("enabled") and not args.check_only:
            root = project_root(spec, args.spec, policy, result)
            output = args.output or configured_artifact_path(spec, policy, "ranking_path_field", result, root=root)
            if output is not None and result.ok():
                atomic_write_yaml(output, ranking_report(spec, details))
    except Exception as error:
        result.issue(str(error))
    if args.json_output:
        print(json.dumps({"check": "candidate_rank_check", "ok": result.ok(), "issues": result.issues, "warnings": result.warnings, "output": str(output) if output else None, "details": details}, indent=2, sort_keys=True))
        return 0 if result.ok() else 1
    code = print_result("candidate_rank_check", result)
    if output is not None and result.ok():
        print(output)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
