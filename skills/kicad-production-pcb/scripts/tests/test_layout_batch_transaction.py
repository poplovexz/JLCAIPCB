#!/usr/bin/env python3
"""Exercise complete-batch placement candidates and atomic Spec application."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_layout_stage import GENERATOR, base_spec  # noqa: E402
from _pcb_skill_checks import sha256_file  # noqa: E402


RUNNER = SCRIPTS_DIR / "layout_batch_transaction.py"


def run(root: Path, spec: Path, candidate: Path, apply: bool = False) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(RUNNER), str(spec), str(candidate), "--generator", str(GENERATOR), "--json"]
    if apply:
        command.append("--apply")
    return subprocess.run(command, cwd=root, text=True, capture_output=True)


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="layout-batch-") as temporary:
        root = Path(temporary)
        spec_path = root / "spec.yaml"
        spec_path.write_text(yaml.safe_dump(base_spec(root), sort_keys=False), encoding="utf-8")
        candidate_path = root / "artifacts" / "candidate.yaml"
        candidate_path.parent.mkdir(parents=True)
        candidate = {
            "schema_version": 1, "project_name": "layout_fixture", "candidate_id": "SHIFTED_CORE", "batch_id": "CORE",
            "base_spec_sha256": sha256_file(spec_path), "layout_revision": 1,
            "placements": [
                {"ref": "R1", "position_mm": {"x": 9, "y": 10, "rotation": 0, "side": "top"}},
                {"ref": "R2", "position_mm": {"x": 21, "y": 10, "rotation": 0, "side": "top"}},
            ],
        }
        candidate_path.write_text(yaml.safe_dump(candidate, sort_keys=False), encoding="utf-8")
        before = spec_path.read_bytes()
        completed = run(root, spec_path, candidate_path)
        if completed.returncode:
            failures.append(f"valid layout candidate failed: {completed.stdout} {completed.stderr}")
        if spec_path.read_bytes() != before:
            failures.append("layout candidate dry-run changed the active Spec")
        completed = run(root, spec_path, candidate_path, apply=True)
        if completed.returncode:
            failures.append(f"layout candidate apply failed: {completed.stdout} {completed.stderr}")
        else:
            applied = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
            positions = {item["ref"]: item["position_mm"]["x"] for item in applied["components"]}
            if positions != {"R1": 9, "R2": 21}:
                failures.append(f"layout candidate was not atomically applied: {positions}")
            if "CORE" not in applied["layout"].get("selected_candidates", {}):
                failures.append("applied layout candidate lacks selected-candidate evidence")

        stale_spec = root / "stale-spec.yaml"
        stale_spec.write_bytes(before)
        partial = dict(candidate)
        partial["base_spec_sha256"] = sha256_file(stale_spec)
        partial["candidate_id"] = "PARTIAL"
        partial["placements"] = partial["placements"][:1]
        partial_path = root / "artifacts" / "partial.yaml"
        partial_path.write_text(yaml.safe_dump(partial, sort_keys=False), encoding="utf-8")
        completed = run(root, stale_spec, partial_path)
        if completed.returncode == 0 or "complete declared placement batch" not in completed.stdout:
            failures.append(f"partial batch was not rejected: {completed.stdout} {completed.stderr}")

    if failures:
        print("layout batch transaction tests: FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("layout batch transaction tests: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
