#!/usr/bin/env python3
"""Run spec-configured Freerouting candidate commands without mutating the main PCB by default."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pcb_skill_checks import CheckResult, get_path, load_spec, load_yaml, print_result, sha256_file, string_value  # noqa: E402
from _layout_stage import check_evidence as check_layout_evidence  # noqa: E402
from _routing_stage import check_contract  # noqa: E402


def mapping_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def policy_path(spec: dict[str, Any]) -> Path:
    configured = get_path(spec, "validation.freerouting.policy_file")
    if string_value(configured):
        path = Path(str(configured))
        return path if path.is_absolute() else Path.cwd() / path
    return Path(__file__).resolve().parents[1] / "assets" / "freerouting-routing-policy.yaml"


def load_policy(spec: dict[str, Any]) -> dict[str, Any]:
    try:
        return load_yaml(policy_path(spec))
    except (OSError, ValueError, yaml.YAMLError):
        return {}


def routing_config(spec: dict[str, Any]) -> dict[str, Any]:
    return mapping_value(mapping_value(spec.get("routing")).get("freerouting"))


def enabled(spec: dict[str, Any], force: bool = False) -> bool:
    return force or routing_config(spec).get("enabled") is True


def project_name(spec: dict[str, Any], spec_path: Path) -> str:
    value = get_path(spec, "project.name")
    return str(value).strip() if string_value(value) else spec_path.stem


def artifacts_dir(spec: dict[str, Any]) -> Path:
    value = get_path(spec, "project.artifacts_dir")
    return Path(str(value)) if string_value(value) else Path("artifacts")


def resolve_path(spec_path: Path, value: Any, default: Path | None = None) -> Path | None:
    if not string_value(value):
        return default
    path = Path(str(value))
    if path.is_absolute():
        return path
    candidate = Path.cwd() / path
    if candidate.exists():
        return candidate
    return (spec_path.parent / path).resolve()


def command_list(value: Any) -> list[str]:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return [str(item) for item in value]
    if isinstance(value, str) and value.strip():
        return shlex.split(value)
    return []


def render_command(command: list[str], context: dict[str, Any]) -> list[str]:
    rendered: list[str] = []
    for item in command:
        rendered.append(item.format(**context))
    return rendered


def run_command(command: list[str], log_path: Path, timeout_seconds: int) -> int:
    with log_path.open("w", encoding="utf-8") as log:
        try:
            completed = subprocess.run(command, text=True, stdout=log, stderr=subprocess.STDOUT, timeout=timeout_seconds)
            return completed.returncode
        except subprocess.TimeoutExpired:
            log.write(f"\nTIMEOUT after {timeout_seconds} seconds\n")
            return 124


def file_entry(path: Path, role: str) -> dict[str, Any]:
    entry: dict[str, Any] = {"role": role, "path": str(path), "exists": path.exists()}
    if path.exists() and path.is_file():
        entry["sha256"] = sha256_file(path)
        entry["size"] = path.stat().st_size
    return entry


def default_pcb_in(spec: dict[str, Any]) -> Path | None:
    output_dir = get_path(spec, "project.output_dir")
    name = get_path(spec, "project.name")
    if string_value(output_dir) and string_value(name):
        return Path(str(output_dir)) / f"{name}.kicad_pcb"
    return None


def make_batch_input(source: Path, target: Path, nets: set[str], result: CheckResult) -> None:
    try:
        import pcbnew
    except Exception as error:
        result.issue(f"pcbnew is required to create isolated routing candidates: {error}")
        return
    board = pcbnew.LoadBoard(str(source))
    for item in list(board.GetTracks()):
        if str(item.GetNetname()) in nets:
            board.Remove(item)
    target.parent.mkdir(parents=True, exist_ok=True)
    board.Save(str(target))


def run_candidates(spec: dict[str, Any], spec_path: Path, result: CheckResult, force: bool = False) -> dict[str, Any]:
    config = routing_config(spec)
    active = enabled(spec, force=force)
    details: dict[str, Any] = {"enabled": active, "candidates": []}
    if not active:
        result.warning("freerouting candidate runner not enabled for this spec")
        return details

    policy = load_policy(spec)
    defaults = mapping_value(policy.get("candidate_defaults"))
    attempts = int(config.get("attempts", defaults.get("attempts", 1)))
    if attempts < 1:
        result.issue("routing.freerouting.attempts must be at least 1")
        return details

    max_attempts = int(defaults.get("max_attempts", attempts))
    if attempts > max_attempts:
        result.issue(f"routing.freerouting.attempts exceeds policy max_attempts {max_attempts}")
        return details
    timeout_seconds = int(config.get("timeout_seconds", defaults["timeout_seconds"]))
    if timeout_seconds < 1:
        result.issue("routing.freerouting.timeout_seconds must be positive")
        return details
    run_template = command_list(config.get("run_command"))
    if not run_template:
        result.issue("routing.freerouting.run_command must be declared; do not hardcode Freerouting invocation in the skill")
        return details
    if not string_value(config.get("tool_version")):
        result.issue("routing.freerouting.tool_version must record the observed Freerouting version")
        return details

    name = project_name(spec, spec_path)
    root = resolve_path(
        spec_path,
        config.get("candidate_output_dir"),
        artifacts_dir(spec) / "freerouting_candidates" / name,
    )
    assert root is not None
    allowed_root = artifacts_dir(spec).resolve()
    if not root.resolve().is_relative_to(allowed_root):
        result.issue(f"routing.freerouting.candidate_output_dir must stay under project.artifacts_dir: {allowed_root}")
        return details
    root.mkdir(parents=True, exist_ok=True)
    pcb_in = resolve_path(spec_path, config.get("pcb_in"), default_pcb_in(spec))
    contract_result = CheckResult()
    check_contract(spec, spec_path, contract_result, True)
    check_layout_evidence(spec, spec_path, contract_result, True)
    result.issues.extend(contract_result.issues)
    result.warnings.extend(contract_result.warnings)
    batch_id = str(config.get("batch_id", ""))
    batch = next((item for item in list_value(get_path(spec, "routing.batches")) if isinstance(item, dict) and item.get("id") == batch_id), None)
    if not batch:
        result.issue("routing.freerouting.batch_id must select one declared routing batch")
    elif str(batch.get("method")) != "freerouting" or str(batch.get("state")) != "planned":
        result.issue("selected Freerouting batch must use method freerouting and state planned")
    if not pcb_in or not pcb_in.is_file():
        result.issue("routing baseline PCB is missing")
    if not result.ok():
        return details
    source_path = pcb_in
    source_sha256 = sha256_file(source_path)
    preflight = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "autoroute_preflight_check.py"), "--require", str(spec_path)], text=True, capture_output=True)
    if preflight.returncode:
        result.issue("autoroute preflight failed: " + " | ".join((preflight.stdout + preflight.stderr).splitlines()[-8:]))
        return details
    isolated_pcb = root / f"{name}_{batch_id}_input.kicad_pcb"
    make_batch_input(pcb_in, isolated_pcb, set(str(net) for net in list_value(batch.get("nets"))), result)
    if not result.ok():
        return details
    pcb_in = isolated_pcb
    dsn = resolve_path(spec_path, config.get("dsn_file"), root / f"{name}.dsn")
    export_template = command_list(config.get("export_dsn_command"))
    import_template = command_list(config.get("import_ses_command"))
    if not import_template:
        result.issue("routing.freerouting.import_ses_command is required so candidates can be hard-gated as KiCad PCBs")
        return details
    seed_base = int(config.get("seed", 1))
    if export_template and dsn:
        context = {
            "candidate": "export",
            "attempt": 0,
            "seed": seed_base,
            "dsn": str(dsn),
            "ses": "",
            "pcb_in": str(pcb_in or ""),
            "pcb_out": "",
            "output_dir": str(root),
        }
        command = render_command(export_template, context)
        status = run_command(command, root / "export_dsn.log", timeout_seconds)
        details["export_dsn_status"] = status
        if status != 0:
            result.issue(f"export_dsn_command failed with exit {status}; see {root / 'export_dsn.log'}")
            return details
    if dsn and not dsn.exists():
        result.issue(f"Missing DSN input for Freerouting candidate run: {dsn}")
        return details

    consecutive_failures = 0
    passing_candidates = 0
    best_score: float | None = None
    no_improvement = 0
    no_improvement_limit = int(config.get("max_no_improvement", defaults["max_no_improvement"]))
    failure_limit = int(config.get("max_consecutive_failures", defaults["max_consecutive_failures"]))
    for attempt in range(1, attempts + 1):
        candidate = f"candidate_{attempt:02d}"
        candidate_dir = root / candidate
        candidate_dir.mkdir(parents=True, exist_ok=True)
        seed = seed_base + attempt - 1
        ses = resolve_path(spec_path, config.get("ses_template"), candidate_dir / f"{name}_{candidate}.ses")
        pcb_out = resolve_path(spec_path, config.get("pcb_out_template"), candidate_dir / f"{name}_{candidate}.kicad_pcb")
        context = {
            "candidate": candidate,
            "attempt": attempt,
            "seed": seed,
            "dsn": str(dsn or ""),
            "ses": str(ses or ""),
            "pcb_in": str(pcb_in or ""),
            "pcb_out": str(pcb_out or ""),
            "output_dir": str(candidate_dir),
        }
        command = render_command(run_template, context)
        status = run_command(command, candidate_dir / "freerouting.log", timeout_seconds)
        entry = {
            "candidate": candidate,
            "seed": seed,
            "command": command,
            "exit_code": status,
            "log": str(candidate_dir / "freerouting.log"),
            "files": [
                file_entry(dsn, "dsn") if dsn else {"role": "dsn", "exists": False},
                file_entry(ses, "ses") if ses else {"role": "ses", "exists": False},
            ],
        }
        if status == 0 and ses and not ses.is_file():
            status = 2
            entry["exit_code"] = status
            result.warning(f"{candidate} reported success but did not create SES output: {ses}")
        if status == 0:
            import_command = render_command(import_template, context)
            import_status = run_command(import_command, candidate_dir / "import_ses.log", timeout_seconds)
            entry["import_command"] = import_command
            entry["import_exit_code"] = import_status
            entry["import_log"] = str(candidate_dir / "import_ses.log")
            entry["files"].append(file_entry(pcb_out, "pcb_out") if pcb_out else {"role": "pcb_out", "exists": False})
            if import_status != 0:
                result.warning(f"{candidate} import_ses_command failed with exit {import_status}; see {candidate_dir / 'import_ses.log'}")
            elif not pcb_out or not pcb_out.is_file():
                result.warning(f"{candidate} import reported success but did not create a candidate PCB")
                import_status = 2
                entry["import_exit_code"] = import_status
        elif status != 0:
            result.warning(f"{candidate} Freerouting command failed with exit {status}; see {candidate_dir / 'freerouting.log'}")
        details["candidates"].append(entry)
        failed = status != 0 or int(entry.get("import_exit_code", 0)) != 0
        if not failed and pcb_out:
            command_evidence = candidate_dir / "command-evidence.json"
            command_evidence.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            candidate_manifest = {
                "schema_version": 1, "project_name": name, "candidate_id": candidate, "batch_id": batch_id,
                "base_spec_sha256": sha256_file(spec_path), "routing_revision": get_path(spec, "routing.revision"),
                "candidate_pcb": str(pcb_out), "generator": "freerouting", "tool_version": str(config["tool_version"]),
                "command_evidence": str(command_evidence), "command_evidence_sha256": sha256_file(command_evidence),
            }
            manifest_file = candidate_dir / "candidate.yaml"
            manifest_file.write_text(yaml.safe_dump(candidate_manifest, sort_keys=False), encoding="utf-8")
            entry["candidate_manifest"] = str(manifest_file)
            gate = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "routing_candidate_transaction.py"), str(spec_path), str(manifest_file), "--json"], text=True, capture_output=True)
            entry["hard_gate_exit_code"] = gate.returncode
            if gate.returncode == 0:
                payload = json.loads(gate.stdout)
                score = float(payload["details"]["score"])
                entry["score"] = score
                passing_candidates += 1
                if best_score is None or score < best_score:
                    best_score = score
                    no_improvement = 0
                else:
                    no_improvement += 1
            else:
                failed = True
                result.warning(f"{candidate} failed routing candidate hard gates")
        consecutive_failures = consecutive_failures + 1 if failed else 0
        if consecutive_failures >= failure_limit:
            details["stopped_early"] = f"{consecutive_failures} consecutive candidate failures"
            break
        if no_improvement >= no_improvement_limit:
            details["stopped_early"] = f"{no_improvement} passing candidates without score improvement"
            break

    if sha256_file(source_path) != source_sha256:
        result.issue("active PCB changed during isolated Freerouting candidate generation")
    if passing_candidates == 0:
        result.issue("Freerouting produced no hard-gate passing KiCad PCB candidates")
    details["best_score"] = best_score

    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "spec": str(spec_path),
        "project": name,
        "details": details,
    }
    manifest_path = root / "candidate_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    details["manifest"] = str(manifest_path)
    return details


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run spec-configured Freerouting candidate attempts.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--require", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv[1:])

    result = CheckResult()
    details: dict[str, Any] = {}
    try:
        details = run_candidates(load_spec(args.spec), args.spec, result, force=args.require)
    except Exception as error:
        result.issue(str(error))

    if args.json_output:
        payload = {
            "check": "freerouting_candidate_runner",
            "ok": result.ok(),
            "issues": result.issues,
            "warnings": result.warnings,
            "details": details,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if result.ok() else 1
    if details.get("manifest"):
        print(f"freerouting_candidate_runner manifest: {details['manifest']}")
    return print_result("freerouting_candidate_runner", result, False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
