#!/usr/bin/env python3
"""Exercise exact-inventory production manifest binding."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import yaml

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
import production_manifest_gate as production_manifest  # noqa: E402
from _pcb_skill_checks import CheckResult  # noqa: E402


def main() -> int:
    failures: list[str] = []
    original = production_manifest.check_local_evidence
    production_manifest.check_local_evidence = lambda spec, spec_path, result, force: None
    try:
        with tempfile.TemporaryDirectory(prefix="production-manifest-") as temporary:
            root = Path(temporary)
            spec = {
                "project": {"name": "production_fixture", "stage": "production-package", "root_dir": ".", "output_dir": "project", "artifacts_dir": "artifacts"},
                "manufacturing": {"jlcpcb": {"package": {"output_dir": "package", "manifest": "package.json"}, "release": {"output_dir": "release", "manifest": "release.json"}}},
            }
            spec_path = root / "spec.yaml"
            spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
            for path in [root / "project/production_fixture.kicad_sch", root / "project/production_fixture.kicad_pcb", root / "artifacts/local-validation/production_fixture/local-validation-manifest.yaml", root / "artifacts/fab/production_fixture/gerbers/board.gbr", root / "package/package.json", root / "package/upload.zip", root / "release/release.json", root / "release/SHA256SUMS"]:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"fixture {path.name}\n", encoding="utf-8")
            report_dir = root / "artifacts/local-validation/production_fixture"
            input_hashes = {
                "spec": production_manifest.sha256_file(spec_path),
                "schematic": production_manifest.sha256_file(root / "project/production_fixture.kicad_sch"),
                "pcb": production_manifest.sha256_file(root / "project/production_fixture.kicad_pcb"),
            }
            for filename in ["final-gate.json", "jlcpcb-gate.json"]:
                (report_dir / filename).write_text(json.dumps({"ok": True, "issues": [], "inputs": {role: {"sha256": digest} for role, digest in input_hashes.items()}}), encoding="utf-8")

            result = CheckResult()
            production_manifest.write_manifest(spec, spec_path, result)
            if result.issues:
                failures.append(f"valid production manifest failed: {result.issues}")
            result = CheckResult()
            production_manifest.check_manifest(spec, spec_path, result)
            if result.issues:
                failures.append(f"fresh production manifest failed: {result.issues}")
            (root / "package/upload.zip").write_text("changed\n", encoding="utf-8")
            result = CheckResult()
            production_manifest.check_manifest(spec, spec_path, result)
            if not any("inventory is stale" in issue for issue in result.issues):
                failures.append("changed production package did not invalidate production manifest")
            production_manifest.write_manifest(spec, spec_path, CheckResult())
            (root / "package/unmanifested.tmp").write_text("stale\n", encoding="utf-8")
            result = CheckResult()
            production_manifest.check_manifest(spec, spec_path, result)
            if not any("inventory is stale" in issue for issue in result.issues):
                failures.append("unmanifested package file did not invalidate production manifest")
            (root / "package/unmanifested.tmp").unlink()
            production_manifest.write_manifest(spec, spec_path, CheckResult())
            (root / "release/unmanifested.tmp").write_text("stale\n", encoding="utf-8")
            result = CheckResult()
            production_manifest.check_manifest(spec, spec_path, result)
            if not any("inventory is stale" in issue for issue in result.issues):
                failures.append("unmanifested release file did not invalidate production manifest")
    finally:
        production_manifest.check_local_evidence = original

    if failures:
        print("production manifest tests: FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("production manifest tests: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
