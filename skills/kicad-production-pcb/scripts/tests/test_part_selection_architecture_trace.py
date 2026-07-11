#!/usr/bin/env python3
"""Focused regression checks for architecture-to-part traceability."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from _pcb_skill_checks import CheckResult, load_spec  # noqa: E402
from part_selection_check import check_part_selection  # noqa: E402


def expect_issue(name: str, spec: dict, expected: str) -> list[str]:
    result = CheckResult()
    sourcing = spec.get("sourcing", {}) if isinstance(spec.get("sourcing"), dict) else {}
    part_lock = sourcing.get("part_lock", {}) if isinstance(sourcing.get("part_lock"), dict) else {}
    check_part_selection(spec, result, force=True, as_of=part_lock.get("locked_at"))
    if any(expected in issue for issue in result.issues):
        print(f"{name}: PASS")
        return []
    return [f"{name}: expected issue containing {expected!r}; got {result.issues}"]


def traced_components(spec: dict) -> list[dict]:
    return [item for item in spec["components"] if isinstance(item.get("architecture_trace"), dict)]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: test_part_selection_architecture_trace.py <passing-part-selection-spec>", file=sys.stderr)
        return 2
    base = load_spec(Path(argv[1]))
    failures: list[str] = []

    no_architecture = copy.deepcopy(base)
    no_architecture.pop("architecture", None)
    failures.extend(
        expect_issue(
            "part selection without architecture",
            no_architecture,
            "architecture prerequisite: architecture section is required",
        )
    )

    missing_trace = copy.deepcopy(base)
    missing_trace_component = traced_components(missing_trace)[0]
    missing_trace_ref = str(missing_trace_component.get("ref"))
    missing_trace_component.pop("architecture_trace", None)
    failures.extend(
        expect_issue(
            "selected part without block trace",
            missing_trace,
            f"{missing_trace_ref} missing architecture_trace",
        )
    )

    unknown_constraint = copy.deepcopy(base)
    traced_components(unknown_constraint)[0]["architecture_trace"]["constraint_ids"] = ["UNKNOWN_CONSTRAINT"]
    failures.extend(
        expect_issue(
            "selected part with unknown constraint",
            unknown_constraint,
            "references unknown architecture constraint",
        )
    )

    missing_block_coverage = copy.deepcopy(base)
    coverage: dict[str, list[dict]] = {}
    for item in traced_components(missing_block_coverage):
        for block_id in item["architecture_trace"].get("block_ids", []):
            coverage.setdefault(str(block_id), []).append(item)
    uncovered_block, covering_components = next(
        (block_id, items) for block_id, items in coverage.items() if len(items) == 1
    )
    covering_components[0].setdefault("assembly", {})["dnp"] = True
    failures.extend(
        expect_issue(
            "required block without selected part",
            missing_block_coverage,
            f"required architecture block has no selected component coverage: {uncovered_block}",
        )
    )

    virtual_coverage = copy.deepcopy(base)
    virtual_coverage_map: dict[str, list[dict]] = {}
    for item in traced_components(virtual_coverage):
        for block_id in item["architecture_trace"].get("block_ids", []):
            virtual_coverage_map.setdefault(str(block_id), []).append(item)
    virtual_block, virtual_components = next(
        (block_id, items) for block_id, items in virtual_coverage_map.items() if len(items) == 1
    )
    virtual_components[0].setdefault("assembly", {})["virtual"] = True
    failures.extend(
        expect_issue(
            "virtual component cannot cover physical block",
            virtual_coverage,
            f"required architecture block has no selected component coverage: {virtual_block}",
        )
    )

    if failures:
        for failure in failures:
            print(f"ISSUE: {failure}")
        return 1
    print("part-selection architecture trace regressions: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
