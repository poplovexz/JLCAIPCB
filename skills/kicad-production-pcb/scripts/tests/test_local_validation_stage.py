#!/usr/bin/env python3
"""Exercise strict JSON local validation and stale/failed evidence behavior."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _local_validation as local_validation  # noqa: E402
from _pcb_skill_checks import CheckResult  # noqa: E402


def command_result(command: list[str], fail: bool = False) -> dict:
    if command[1:] == ["version"]:
        return {"command": command, "exit_code": 0, "stdout": "10.0.0", "stderr": ""}
    output = Path(command[command.index("--output") + 1])
    payload = {"sheets": [{"violations": [{}] if fail else []}]} if command[1:3] == ["sch", "erc"] else {"violations": [{}] if fail else [], "unconnected_items": [], "schematic_parity": []}
    output.write_text(__import__("json").dumps(payload), encoding="utf-8")
    return {"command": command, "exit_code": 5 if fail else 0, "stdout": "", "stderr": ""}


def main() -> int:
    failures: list[str] = []
    original = local_validation.prerequisite_checks
    original_run = local_validation.run_command
    local_validation.prerequisite_checks = lambda spec, spec_path, result: None
    local_validation.run_command = command_result
    try:
        with tempfile.TemporaryDirectory(prefix="local-validation-") as temporary:
            root = Path(temporary)
            spec = {"project": {"name": "validation_fixture", "stage": "local-mvp", "root_dir": ".", "output_dir": "project", "artifacts_dir": "artifacts", "kicad_major_required": 10}}
            spec_path = root / "spec.yaml"
            spec_path.write_text("project:\n  name: validation_fixture\n", encoding="utf-8")
            project = root / "project"
            project.mkdir()
            (project / "validation_fixture.kicad_sch").write_text("schematic\n", encoding="utf-8")
            (project / "validation_fixture.kicad_pcb").write_text("board\n", encoding="utf-8")
            result = CheckResult()
            details = local_validation.run_validation(spec, spec_path, result)
            if result.issues or details.get("status") != "passed":
                failures.append(f"strict local validation failed: {result.issues}")
            result = CheckResult()
            local_validation.check_evidence(spec, spec_path, result)
            if result.issues:
                failures.append(f"fresh local validation evidence failed: {result.issues}")

            board = root / "project" / f"{spec['project']['name']}.kicad_pcb"
            board.write_bytes(board.read_bytes() + b"\n")
            result = CheckResult()
            local_validation.check_evidence(spec, spec_path, result)
            if not any("inputs do not match" in issue for issue in result.issues):
                failures.append("changed PCB did not invalidate local validation evidence")

            local_validation.run_command = lambda command: command_result(command, fail=command[1:] != ["version"])
            result = CheckResult()
            details = local_validation.run_validation(spec, spec_path, result)
            if result.ok() or details.get("status") != "failed":
                failures.append("unrouted board did not write failed local validation evidence")
            result = CheckResult()
            local_validation.check_evidence(spec, spec_path, result)
            if not any("status/schema" in issue for issue in result.issues):
                failures.append("failed run left an authoritative passing local validation manifest")
    finally:
        local_validation.prerequisite_checks = original
        local_validation.run_command = original_run

    if failures:
        print("local validation stage tests: FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("local validation stage tests: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
