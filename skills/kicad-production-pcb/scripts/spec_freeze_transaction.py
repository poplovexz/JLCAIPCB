#!/usr/bin/env python3
"""Run freeze preflights, then atomically persist the local Spec Freeze."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pcb_skill_checks import CheckResult, load_spec, print_result  # noqa: E402
from _spec_freeze import (  # noqa: E402
    apply_freeze_transaction,
    check_freeze_contract,
    freeze_required,
    freeze_tier,
    load_policy,
    prepare_freeze_transaction,
    run_preflight,
)


@contextmanager
def exclusive_file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            import fcntl  # type: ignore[import-not-found]

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            unlock = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except ImportError:
            import msvcrt  # type: ignore[import-not-found]

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            unlock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        try:
            yield
        finally:
            handle.seek(0)
            unlock()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Create an all-or-nothing local Spec Freeze.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--require", action="store_true")
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])

    result = CheckResult()
    transaction: dict = {}
    records: list[dict] = []
    lock_path = args.spec.with_suffix(args.spec.suffix + ".freeze.lock")
    try:
        with exclusive_file_lock(lock_path):
            original_bytes = args.spec.read_bytes()
            spec = load_spec(args.spec)
            policy = load_policy()
            required = freeze_required(spec, policy, forced=args.require or args.production)
            if not required:
                result.warning("Spec Freeze transaction is not required for this legacy/draft spec; use --require to force it")
            else:
                tier = check_freeze_contract(spec, args.spec, policy, result, production=args.production)
                if result.ok():
                    records = run_preflight(args.spec, policy, tier, result)
                if result.ok() and args.spec.read_bytes() != original_bytes:
                    result.issue("Spec changed while freeze preflights were running; rerun the transaction")
                if result.ok():
                    current = load_spec(args.spec)
                    transaction = prepare_freeze_transaction(
                        current,
                        args.spec,
                        policy,
                        result,
                        records,
                        production=args.production,
                    )
                if result.ok() and args.apply:
                    apply_freeze_transaction(args.spec, transaction)
    except Exception as error:
        result.issue(str(error))

    payload = {
        "check": "spec_freeze_transaction",
        "ok": result.ok(),
        "applied": bool(args.apply and result.ok() and transaction),
        "issues": result.issues,
        "warnings": result.warnings,
        "tier": transaction.get("tier") or (freeze_tier(load_spec(args.spec), load_policy(), args.production) if args.spec.is_file() else None),
        "manifest": str(transaction.get("manifest_path")) if transaction.get("manifest_path") else None,
        "product_baseline": (
            str(transaction.get("product_baseline_path")) if transaction.get("product_baseline_path") else None
        ),
        "preflight": records,
    }
    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if result.ok() else 1
    code = print_result("spec_freeze_transaction", result, False)
    for record in records:
        print(f"Spec Freeze preflight {record.get('id')}: {str(record.get('status')).upper()}")
    if result.ok() and transaction:
        action = "applied" if args.apply else "validated (dry run)"
        print(f"Spec Freeze {action}: {payload['manifest']}")
        print(f"Product baseline {action}: {payload['product_baseline']}")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
