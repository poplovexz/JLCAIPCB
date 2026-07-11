#!/usr/bin/env python3
"""Exercise isolated whole-batch candidate validation and atomic route locking."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _layout_stage import run_after_generation as accept_layout  # noqa: E402
from _pcb_skill_checks import CheckResult, sha256_file  # noqa: E402
from _routing_stage import route_snapshot  # noqa: E402
from test_layout_stage import GENERATOR  # noqa: E402
from test_routing_stage import routed_spec  # noqa: E402


RUNNER = SCRIPTS_DIR / "routing_candidate_transaction.py"


def generate(root: Path, spec: Path, mode: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(GENERATOR), mode, str(spec)], cwd=root, text=True, capture_output=True)


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="routing-candidate-") as temporary:
        root = Path(temporary)
        spec = routed_spec(root)
        spec["routing"]["batches"][0].update({"method": "freerouting", "state": "planned"})
        spec["routing"]["freerouting"] = {"enabled": True}
        spec_path = root / "spec.yaml"
        spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
        generate(root, spec_path, "--layout-only")
        result = CheckResult()
        accept_layout(spec, spec_path, GENERATOR, result)
        generate(root, spec_path, "--board-only")

        candidate_spec = copy.deepcopy(spec)
        candidate_spec["routes"][0]["waypoints_mm"] = [{"x": 14, "y": 13}]
        candidate_spec["project"]["output_dir"] = "artifacts/candidate-project"
        candidate_spec_path = root / "candidate-spec.yaml"
        candidate_spec_path.write_text(yaml.safe_dump(candidate_spec, sort_keys=False), encoding="utf-8")
        generated = generate(root, candidate_spec_path, "--board-only")
        candidate_pcb = root / "artifacts" / "candidate-project" / "layout_fixture.kicad_pcb"
        command_evidence = root / "artifacts" / "candidate-command.json"
        command_evidence.write_text(json.dumps({"command": ["fixture"], "exit_code": 0, "import_exit_code": 0}), encoding="utf-8")
        manifest = root / "artifacts" / "candidate.yaml"
        manifest.write_text(yaml.safe_dump({
            "schema_version": 1, "project_name": "layout_fixture", "candidate_id": "CANDIDATE_A", "batch_id": "SIGNALS",
            "base_spec_sha256": sha256_file(spec_path), "routing_revision": 1,
            "candidate_pcb": "artifacts/candidate-project/layout_fixture.kicad_pcb", "generator": "fixture",
            "tool_version": "fixture-1", "command_evidence": "artifacts/candidate-command.json",
            "command_evidence_sha256": sha256_file(command_evidence),
        }, sort_keys=False), encoding="utf-8")
        if generated.returncode:
            failures.append(f"candidate generation failed: {generated.stdout} {generated.stderr}")
        else:
            before = spec_path.read_bytes()
            dry = subprocess.run([sys.executable, str(RUNNER), str(spec_path), str(manifest), "--json"], cwd=root, text=True, capture_output=True)
            if dry.returncode:
                failures.append(f"valid candidate dry-run failed: {dry.stdout} {dry.stderr}")
            if spec_path.read_bytes() != before:
                failures.append("candidate dry-run changed the active Spec")
            command_evidence.write_text(json.dumps({"command": ["tampered"], "exit_code": 0, "import_exit_code": 0}), encoding="utf-8")
            tampered = subprocess.run([sys.executable, str(RUNNER), str(spec_path), str(manifest), "--json"], cwd=root, text=True, capture_output=True)
            if tampered.returncode == 0 or "evidence hash is stale" not in tampered.stdout:
                failures.append("tampered command evidence was not rejected")
            command_evidence.write_text(json.dumps({"command": ["fixture"], "exit_code": 0, "import_exit_code": 0}), encoding="utf-8")
            applied = subprocess.run([sys.executable, str(RUNNER), str(spec_path), str(manifest), "--apply", "--json"], cwd=root, text=True, capture_output=True)
            if applied.returncode:
                failures.append(f"valid candidate apply failed: {applied.stdout} {applied.stderr}")
            else:
                locked = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
                artifact = root / locked["routing"]["route_lock"]["artifact"]["path"]
                if not artifact.is_file() or sha256_file(artifact) != locked["routing"]["route_lock"]["artifact"]["sha256"]:
                    failures.append("route lock artifact was not atomically bound")
                regenerated = generate(root, spec_path, "--board-only")
                result = CheckResult()
                metrics = route_snapshot(root / "project" / "layout_fixture.kicad_pcb", result)
                if regenerated.returncode or result.issues or metrics.get("segment_count") != 2:
                    failures.append(f"route lock did not regenerate accepted copper: {regenerated.stdout} {regenerated.stderr} {result.issues}")

    if failures:
        print("routing candidate transaction tests: FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("routing candidate transaction tests: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
