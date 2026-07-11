#!/usr/bin/env python3
"""Preview or verify the generated PCB product baseline handoff."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _part_lock import durable_write  # noqa: E402
from _pcb_skill_checks import CheckResult, get_path, load_spec, print_result, sha256_file  # noqa: E402
from _product_baseline import product_baseline_path, render_product_baseline, renderer_path  # noqa: E402
from _spec_freeze import (  # noqa: E402
    artifact_bindings,
    check_freeze_contract,
    check_frozen_spec,
    load_policy,
    manifest_path,
    next_revision,
    policy_path,
    product_baseline_context,
    spec_digest,
    utc_timestamp,
)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Preview or verify the generated PCB product baseline.")
    parser.add_argument("spec", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--check", action="store_true")
    parser.add_argument("--require", action="store_true")
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])

    result = CheckResult()
    output_path: Path | None = None
    details: dict = {}
    try:
        spec = load_spec(args.spec)
        if args.check:
            details = check_frozen_spec(
                spec,
                args.spec,
                result,
                require=args.require,
                production=args.production,
            )
            baseline = details.get("product_baseline")
            output_path = Path(str(baseline)) if baseline else None
        else:
            policy = load_policy()
            tier = check_freeze_contract(spec, args.spec, policy, result, production=args.production)
            root, manifest_target = manifest_path(spec, args.spec, policy, result)
            bindings = artifact_bindings(spec, args.spec, policy, tier, result)
            if result.ok():
                output_path = product_baseline_path(manifest_target, policy, preview=True)
                context = product_baseline_context(
                    spec,
                    args.spec,
                    root,
                    get_path(policy, "product_baseline.preview_status"),
                    tier,
                    next_revision(spec),
                    utc_timestamp(),
                    spec_digest(spec, policy),
                    sha256_file(policy_path()),
                    sha256_file(renderer_path()),
                    bindings,
                    [],
                )
                durable_write(output_path, render_product_baseline(spec, policy, context))
                details = {"tier": tier, "product_baseline": str(output_path)}
    except Exception as error:
        result.issue(str(error))

    payload = {
        "check": "product_baseline",
        "mode": "preview" if args.preview else "check",
        "ok": result.ok(),
        "path": str(output_path) if output_path else None,
        "issues": result.issues,
        "warnings": result.warnings,
        "details": details,
    }
    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if result.ok() else 1
    code = print_result("product_baseline", result, False)
    if result.ok() and output_path:
        action = "preview" if args.preview else "verified"
        print(f"Product baseline {action}: {output_path}")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
