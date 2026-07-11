#!/usr/bin/env python3
"""Validate all sourcing batches, then atomically lock and apply every selected part."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _part_lock import apply_part_lock_transaction, prepare_part_lock_transaction  # noqa: E402
from _pcb_skill_checks import CheckResult, load_spec, print_result  # noqa: E402


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
    parser = argparse.ArgumentParser(description="Create an all-or-nothing sourcing part lock.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--as-of")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])
    result = CheckResult()
    transaction = {}
    lock_file = args.spec.with_suffix(args.spec.suffix + ".sourcing.lock")
    try:
        with exclusive_file_lock(lock_file):
            transaction = prepare_part_lock_transaction(load_spec(args.spec), args.spec, result, as_of=args.as_of)
            if result.ok() and args.apply:
                apply_part_lock_transaction(args.spec, transaction)
    except Exception as error:
        result.issue(str(error))
    payload = {
        "check": "part_lock_transaction",
        "ok": result.ok(),
        "applied": bool(args.apply and result.ok()),
        "issues": result.issues,
        "warnings": result.warnings,
        "selection_changed": transaction.get("selection_changed"),
        "ranking": str(transaction.get("ranking_path")) if transaction.get("ranking_path") else None,
        "part_lock": str(transaction.get("lock_path")) if transaction.get("lock_path") else None,
    }
    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if result.ok() else 1
    code = print_result("part_lock_transaction", result)
    if result.ok():
        action = "applied" if args.apply else "validated (dry run)"
        print(f"part lock {action}: {payload['part_lock']}")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
