#!/usr/bin/env python3
"""Run bundled golden specs through the skill check executors."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import yaml


def load_case(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Golden spec must be a mapping: {path}")
    return data


def check_command(check: object, scripts_dir: Path, case: Path) -> tuple[str, list[str], int | None]:
    if isinstance(check, str):
        return check, [sys.executable, str(scripts_dir / check), str(case)], None
    if not isinstance(check, dict):
        raise ValueError(f"benchmark.checks entries must be strings or mappings: {check}")
    script = check.get("script")
    if not isinstance(script, str) or not script.strip():
        raise ValueError(f"benchmark.checks mapping missing script: {check}")
    args = check.get("args", [])
    if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
        raise ValueError(f"benchmark.checks args must be a list of strings: {check}")
    expected_exit = check.get("expected_exit")
    if expected_exit is not None:
        expected_exit = int(expected_exit)
    return script, [sys.executable, str(scripts_dir / script), *args, str(case)], expected_exit


def resolve_case_path(case: Path, value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"proposal path must be a non-empty string: {value}")
    path = Path(value)
    if path.is_absolute():
        return path
    return (case.parent / path).resolve()


def main(argv: list[str]) -> int:
    skill_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Run kicad-production-pcb golden spec checks.")
    parser.add_argument("--golden-dir", type=Path, default=skill_root / "assets" / "golden-specs")
    args = parser.parse_args(argv[1:])

    scripts_dir = skill_root / "scripts"
    failures: list[str] = []
    cases = sorted(args.golden_dir.glob("*.yaml"))
    if not cases:
        print(f"No golden specs found: {args.golden_dir}", file=sys.stderr)
        return 1

    for case in cases:
        spec = load_case(case)
        expected = spec.get("benchmark", {})
        default_expected_exit = int(expected.get("expected_exit", 0))
        expected_by_check = expected.get("expected_by_check", {})
        if not isinstance(expected_by_check, dict):
            failures.append(f"{case}: benchmark.expected_by_check must be a mapping")
            expected_by_check = {}
        checks = expected.get("checks", ["spec_schema_check.py", "spec_net_graph_check.py"])
        if not isinstance(checks, list):
            failures.append(f"{case}: benchmark.checks must be a list")
            continue
        working_directory = expected.get("working_directory", "caller")
        working_directories = {
            "caller": None,
            "skill-root": skill_root,
            "case-directory": case.parent,
        }
        if working_directory not in working_directories:
            failures.append(
                f"{case}: benchmark.working_directory must be one of {', '.join(working_directories)}"
            )
            continue
        command_cwd = working_directories[working_directory]
        print(f"CASE {case.name}")
        for check in checks:
            try:
                check_name, command, check_expected_exit = check_command(check, scripts_dir, case)
            except ValueError as error:
                failures.append(f"{case.name}: {error}")
                continue
            expected_exit = int(check_expected_exit if check_expected_exit is not None else expected_by_check.get(check_name, default_expected_exit))
            environment = {**os.environ, "KICAD_PCB_TEST_MODE": "1"}
            completed = subprocess.run(command, text=True, capture_output=True, cwd=command_cwd, env=environment)
            print(completed.stdout, end="")
            if completed.stderr:
                print(completed.stderr, end="", file=sys.stderr)
            if completed.returncode != expected_exit:
                failures.append(f"{case.name} {' '.join(command[1:])}: expected exit {expected_exit}, got {completed.returncode}")

        transaction_proposals = expected.get("transaction_proposals", [])
        if transaction_proposals and not isinstance(transaction_proposals, list):
            failures.append(f"{case}: benchmark.transaction_proposals must be a list")
            transaction_proposals = []
        for item in transaction_proposals:
            if not isinstance(item, dict):
                failures.append(f"{case}: transaction proposal entries must be mappings")
                continue
            try:
                proposal_path = resolve_case_path(case, item.get("proposal"))
            except ValueError as error:
                failures.append(f"{case}: {error}")
                continue
            expected_exit = int(item.get("expected_exit", 0))
            command = [sys.executable, str(scripts_dir / "batch_transaction_runner.py"), str(case), str(proposal_path)]
            environment = {**os.environ, "KICAD_PCB_TEST_MODE": "1"}
            completed = subprocess.run(command, text=True, capture_output=True, cwd=command_cwd, env=environment)
            print(completed.stdout, end="")
            if completed.stderr:
                print(completed.stderr, end="", file=sys.stderr)
            if completed.returncode != expected_exit:
                failures.append(
                    f"{case.name} batch_transaction_runner.py {proposal_path.name}: expected exit {expected_exit}, got {completed.returncode}"
                )

    if failures:
        print("Golden benchmark: FAIL")
        for failure in failures:
            print(f"ISSUE: {failure}")
        return 1
    print("Golden benchmark: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
