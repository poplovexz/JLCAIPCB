#!/usr/bin/env python3
"""Exercise dry-run, atomic promotion, and import containment checks."""

from __future__ import annotations

import copy
import sys
import tempfile
from pathlib import Path

import yaml

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
from _part_lock import replace_files_transactionally  # noqa: E402
from _pcb_skill_checks import CheckResult, sha256_file  # noqa: E402
from library_import_transaction import prepare  # noqa: E402


def write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="library-import-transaction-") as temporary:
        root = Path(temporary)
        artifacts = root / "artifacts"
        candidate = artifacts / "candidate"
        candidate.mkdir(parents=True)
        source = artifacts / "raw-source.json"
        symbol = candidate / "fixture.kicad_sym"
        footprint = candidate / "Fixture.kicad_mod"
        source.write_text('{"source":"fixture"}\n', encoding="utf-8")
        symbol.write_text('(kicad_symbol_lib (version 20231120) (generator "fixture"))\n', encoding="utf-8")
        footprint.write_text('(footprint "Fixture" (version 20240108) (generator "fixture") (layer "F.Cu"))\n', encoding="utf-8")
        destination_symbol = root / "project" / "lib" / symbol.name
        destination_footprint = root / "project" / "lib" / "Fixture.pretty" / footprint.name
        destination_symbol.parent.mkdir(parents=True)
        destination_symbol.write_text("old symbol\n", encoding="utf-8")
        transaction = {
            "schema_version": 1,
            "project_name": "fixture",
            "source": {
                "identity": "fixture-source", "evidence_id": "SOURCE", "evidence_sha256": sha256_file(source),
                "file": str(source), "sha256": sha256_file(source),
            },
            "tool": {"name": "fixture-importer", "version": "1", "command": ["fixture-importer", "fixture-source"]},
            "candidate_directory": str(candidate),
            "outputs": [
                {"role": "symbol", "candidate_file": str(symbol), "destination_file": str(destination_symbol), "sha256": sha256_file(symbol)},
                {"role": "footprint", "candidate_file": str(footprint), "destination_file": str(destination_footprint), "sha256": sha256_file(footprint)},
            ],
            "provenance_output": str(artifacts / "import-provenance.json"),
            "journal": str(artifacts / "import-journal.json"),
        }
        transaction_path = artifacts / "transaction.yaml"
        write_yaml(transaction_path, transaction)
        spec = {"project": {"name": "fixture", "root_dir": ".", "artifacts_dir": "artifacts"}}
        spec_path = root / "spec.yaml"
        write_yaml(spec_path, spec)

        result = CheckResult()
        replacements, journal, _ = prepare(spec, spec_path, transaction_path, result)
        if result.issues:
            failures.append(f"valid import transaction failed: {result.issues}")
        if destination_symbol.read_text(encoding="utf-8") != "old symbol\n":
            failures.append("dry-run changed an active library file")
        if journal is not None and result.ok():
            replace_files_transactionally(replacements, journal)
        if destination_symbol.read_bytes() != symbol.read_bytes() or destination_footprint.read_bytes() != footprint.read_bytes():
            failures.append("atomic promotion did not install candidate outputs")
        if not (artifacts / "import-provenance.json").is_file():
            failures.append("atomic promotion did not write provenance")

        escaped = copy.deepcopy(transaction)
        escaped["outputs"][0]["destination_file"] = str(root.parent / "escaped.kicad_sym")
        write_yaml(transaction_path, escaped)
        result = CheckResult()
        prepare(spec, spec_path, transaction_path, result)
        if not any("project.root_dir" in issue for issue in result.issues):
            failures.append(f"destination escape was not rejected: {result.issues}")

        tampered = copy.deepcopy(transaction)
        tampered["outputs"][1]["sha256"] = "0" * 64
        write_yaml(transaction_path, tampered)
        result = CheckResult()
        prepare(spec, spec_path, transaction_path, result)
        if not any("missing or stale" in issue for issue in result.issues):
            failures.append(f"tampered candidate was not rejected: {result.issues}")

    if failures:
        print("library import transaction tests: FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("library import transaction tests: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
