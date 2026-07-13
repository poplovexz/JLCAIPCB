#!/usr/bin/env python3
"""Generate configured multilayer boards and verify their Specctra DSN layer sets."""

from __future__ import annotations

import argparse
import copy
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pcbnew
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "configs" / "multilayer_regression.yaml"
DSN_LAYER = re.compile(r'^\s*\(layer\s+(?:"([^"]+)"|([^\s()]+))', re.MULTILINE)


def mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def sequence(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return data


def repo_path(value: Any) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else REPO_ROOT / path


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True)


def require_success(command: list[str], label: str) -> None:
    completed = run(command)
    if completed.returncode:
        raise AssertionError(
            f"{label} failed with exit {completed.returncode}\n{completed.stdout}{completed.stderr}"
        )


def fixture_spec(
    base_spec: dict[str, Any],
    regression: dict[str, Any],
    case_id: str,
    copper_layers: int,
    allowed_layer: str,
    output_dir: Path,
) -> dict[str, Any]:
    spec = copy.deepcopy(base_spec)
    spec["project"]["name"] = str(regression["project_name_template"]).format(case_id=case_id)
    spec["project"]["output_dir"] = str(output_dir)
    spec["project"]["artifacts_dir"] = str(output_dir.parent / "artifacts")
    spec["board"]["layers"]["copper"] = copper_layers
    batch = mapping(regression.get("batch"))
    selected_net = str(batch["selected_net"])
    spec["routing"] = {
        "batches": [
            {
                "id": str(batch["id"]),
                "state": "planned",
                "nets": [selected_net],
            }
        ],
        "fallback_net_class": str(batch["fallback_net_class"]),
        "net_constraints": {
            selected_net: {
                "net_class": str(batch["fallback_net_class"]),
                "allowed_layers": [allowed_layer],
            }
        },
    }
    return spec


def write_spec(path: Path, spec: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")


def export_command(
    regression: dict[str, Any],
    spec_path: Path,
    board_path: Path,
    output_path: Path,
) -> list[str]:
    batch = mapping(regression.get("batch"))
    return [
        sys.executable,
        str(repo_path(regression["exporter"])),
        "--spec",
        str(spec_path),
        "--batch-id",
        str(batch["id"]),
        "--input",
        str(board_path),
        "--output",
        str(output_path),
        "--locked-class",
        str(batch["locked_class"]),
    ]


def dsn_layers(path: Path) -> set[str]:
    return {quoted or bare for quoted, bare in DSN_LAYER.findall(path.read_text(encoding="utf-8"))}


def run_positive_cases(
    regression: dict[str, Any],
    base_spec: dict[str, Any],
    temporary_root: Path,
) -> dict[str, dict[str, Path]]:
    generated: dict[str, dict[str, Path]] = {}
    for case in sequence(regression.get("cases")):
        if not isinstance(case, dict):
            raise ValueError(f"regression case must be a mapping: {case}")
        case_id = str(case["id"])
        copper_layers = int(case["copper_layers"])
        highest_inner_layer = str(case["highest_inner_layer"])
        case_root = temporary_root / case_id
        output_dir = case_root / "project"
        spec_path = case_root / "spec.yaml"
        spec = fixture_spec(base_spec, regression, case_id, copper_layers, highest_inner_layer, output_dir)
        write_spec(spec_path, spec)
        generator = [
            sys.executable,
            str(repo_path(regression["generator"])),
            *[str(item) for item in sequence(regression.get("generator_args"))],
            str(spec_path),
        ]
        require_success(generator, f"{case_id} generation")
        board_path = output_dir / f"{spec['project']['name']}.kicad_pcb"
        board = pcbnew.LoadBoard(str(board_path))
        if int(board.GetCopperLayerCount()) != copper_layers:
            raise AssertionError(f"{case_id} board copper layer count mismatch")
        enabled_layers = {
            str(board.GetLayerName(layer)) for layer in board.GetEnabledLayers().CuStack()
        }
        if len(enabled_layers) != copper_layers or highest_inner_layer not in enabled_layers:
            raise AssertionError(
                f"{case_id} enabled copper layers do not contain {highest_inner_layer}: {sorted(enabled_layers)}"
            )
        dsn_path = case_root / "routing.dsn"
        require_success(
            export_command(regression, spec_path, board_path, dsn_path),
            f"{case_id} DSN export",
        )
        exported_layers = dsn_layers(dsn_path)
        if not enabled_layers <= exported_layers or highest_inner_layer not in exported_layers:
            raise AssertionError(
                f"{case_id} DSN copper layers mismatch: expected {sorted(enabled_layers)}, got {sorted(exported_layers)}"
            )
        generated[case_id] = {"board": board_path, "spec": spec_path}
    return generated


def run_negative_case(
    regression: dict[str, Any],
    base_spec: dict[str, Any],
    generated: dict[str, dict[str, Path]],
    case: dict[str, Any],
    temporary_root: Path,
) -> None:
    case_id = str(case["id"])
    board_case = str(case["board_case"])
    case_root = temporary_root / "negative" / case_id
    spec_path = case_root / "spec.yaml"
    spec = fixture_spec(
        base_spec,
        regression,
        case_id,
        int(case["copper_layers"]),
        str(case["allowed_layer"]),
        case_root / "project",
    )
    write_spec(spec_path, spec)
    completed = run(
        export_command(
            regression,
            spec_path,
            generated[board_case]["board"],
            case_root / "routing.dsn",
        )
    )
    output = completed.stdout + completed.stderr
    if completed.returncode == 0:
        raise AssertionError(f"{case_id} unexpectedly exported a DSN")
    if str(case["expected_error"]) not in output:
        raise AssertionError(f"{case_id} missing expected error; output was:\n{output}")


def run_regression(config_path: Path) -> None:
    config = load_yaml(config_path)
    regression = mapping(config.get("regression"))
    base_spec = load_yaml(repo_path(regression["base_spec"]))
    with tempfile.TemporaryDirectory(prefix="multilayer-generation-dsn-") as temporary:
        temporary_root = Path(temporary)
        generated = run_positive_cases(regression, base_spec, temporary_root)
        negative = mapping(regression.get("negative_cases"))
        negative_cases = sequence(negative.get("invalid_layer_counts"))
        negative_cases.extend(
            mapping(negative.get(name))
            for name in ["board_spec_mismatch", "disabled_allowed_layer"]
        )
        for case in negative_cases:
            run_negative_case(regression, base_spec, generated, mapping(case), temporary_root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", nargs="?", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    try:
        run_regression(args.config.resolve())
    except Exception as error:
        print("multilayer generation/DSN regression: FAIL")
        print(f"- {error}")
        return 1
    print("multilayer generation/DSN regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
