#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "Usage: hooks/pre-final-pcb-flow.sh specs/<project>.yaml [--require-checks] [--require-fab] [--allow-violations]" >&2
  exit 2
fi

spec=""
for arg in "$@"; do
  if [[ "$arg" != -* ]]; then
    spec="$arg"
    break
  fi
done

if [ -z "$spec" ]; then
  echo "Cannot find specs/<project>.yaml argument" >&2
  exit 2
fi

lock_path="$(python3 -c 'import pathlib, sys, yaml; spec_path=pathlib.Path(sys.argv[1]); spec=yaml.safe_load(spec_path.open(encoding="utf-8")) or {}; project=spec.get("project", {}); artifacts_dir=pathlib.Path(str(project.get("artifacts_dir", "artifacts"))); project_name=str(project.get("name", spec_path.stem)); path=artifacts_dir / "locks" / f"{project_name}.lock"; path.parent.mkdir(parents=True, exist_ok=True); print(path)' "$spec")"
exec 9>"$lock_path"
echo "Acquiring PCB flow lock: $lock_path"
flock 9

skill_scripts_dir="${KICAD_PRODUCTION_PCB_SKILL_SCRIPTS:-codex-skills/kicad-production-pcb/scripts}"
requirement_intake_gate="$skill_scripts_dir/requirement_intake_gate.py"
flow_state_gate="$skill_scripts_dir/flow_state_gate.py"
beginner_intake_check="$skill_scripts_dir/beginner_intake_check.py"
architecture_report="$skill_scripts_dir/architecture_report.py"
architecture_check="$skill_scripts_dir/architecture_gate.py"
closure_check="$skill_scripts_dir/spec_closure_check.py"
part_selection_check="$skill_scripts_dir/part_selection_check.py"
readiness_check="$skill_scripts_dir/readiness_preflight.py"
verification_check="$skill_scripts_dir/verification_preflight.py"
fabrication_capability_check="$skill_scripts_dir/fabrication_capability_gate.py"
schema_check="$skill_scripts_dir/spec_schema_check.py"
net_graph_check="$skill_scripts_dir/spec_net_graph_check.py"
batch_check="$skill_scripts_dir/connectivity_batch_check.py"
autoroute_preflight_check="$skill_scripts_dir/autoroute_preflight_check.py"
route_score_check="$skill_scripts_dir/route_score_check.py"
freeze_check="$skill_scripts_dir/spec_freeze_check.py"
output_binding_check="$skill_scripts_dir/spec_output_binding.py"
schematic_stage_check="$skill_scripts_dir/schematic_stage_gate.py"
package_binding_stage_check="$skill_scripts_dir/package_binding_stage_gate.py"
layout_stage_check="$skill_scripts_dir/layout_stage_gate.py"
routing_stage_check="$skill_scripts_dir/routing_stage_gate.py"
local_validation_check="$skill_scripts_dir/local_validation_gate.py"
production_manifest_check="$skill_scripts_dir/production_manifest_gate.py"
evidence_manifest_check="$skill_scripts_dir/evidence_manifest_check.py"

for check_script in "$requirement_intake_gate" "$flow_state_gate" "$beginner_intake_check" "$architecture_report" "$architecture_check" "$closure_check" "$part_selection_check" "$readiness_check" "$verification_check" "$fabrication_capability_check" "$schema_check" "$net_graph_check" "$autoroute_preflight_check" "$route_score_check" "$freeze_check" "$output_binding_check" "$schematic_stage_check" "$package_binding_stage_check" "$layout_stage_check" "$routing_stage_check" "$local_validation_check" "$production_manifest_check" "$evidence_manifest_check"; do
  if [ ! -f "$check_script" ]; then
    echo "Missing kicad-production-pcb preflight script: $check_script" >&2
    exit 2
  fi
done

flow_state_json="$(python3 "$flow_state_gate" --report-only --json "$spec")"
production_required="$(printf '%s' "$flow_state_json" | python3 -c 'import json, sys; print("yes" if json.load(sys.stdin)["details"].get("production_required") else "no")')"
strict_required="$(printf '%s' "$flow_state_json" | python3 -c 'import json, sys; print("yes" if json.load(sys.stdin)["details"].get("strict_violations_required") else "no")')"
order_ready_required="$(printf '%s' "$flow_state_json" | python3 -c 'import json, sys; print("yes" if json.load(sys.stdin)["details"].get("order_ready_required") else "no")')"
allow_violations="no"
continue_on_check_fail="no"
for arg in "$@"; do
  if [ "$arg" = "--allow-violations" ]; then
    allow_violations="yes"
  fi
  if [ "$arg" = "--continue-on-check-fail" ]; then
    continue_on_check_fail="yes"
  fi
done
flow_args=()
if [ "$strict_required" = "yes" ]; then
  flow_args+=("--strict-violations")
fi
if [ "$allow_violations" = "yes" ]; then
  flow_args+=("--allow-violations")
fi
if [ "$continue_on_check_fail" = "yes" ]; then
  flow_args+=("--continue-on-check-fail")
fi
python3 "$requirement_intake_gate" --before-generation "$spec"
python3 "$flow_state_gate" "${flow_args[@]}" "$spec"
if [ "$production_required" = "yes" ]; then
  python3 "$freeze_check" --before-generation --production "$spec"
else
  python3 "$freeze_check" --before-generation "$spec"
fi
python3 "$beginner_intake_check" "$spec"
python3 "$architecture_report" --if-required "$spec"
python3 "$architecture_check" --before-generation "$spec"
if [ "$production_required" = "yes" ]; then
  python3 "$closure_check" --strict "$spec"
  python3 "$part_selection_check" --require --before-generation "$spec"
  python3 "$readiness_check" --production "$spec"
  python3 "$verification_check" --strict "$spec"
  python3 "$schema_check" --production "$spec"
else
  python3 "$closure_check" "$spec"
  python3 "$part_selection_check" --before-generation "$spec"
  python3 "$readiness_check" "$spec"
  python3 "$verification_check" "$spec"
  python3 "$schema_check" "$spec"
fi
python3 "$fabrication_capability_check" --check-outputs "$spec"
python3 "$net_graph_check" --exact "$spec"
python3 "$net_graph_check" --generated --strict-names "$spec"
python3 "$schematic_stage_check" --check-evidence "$spec"
python3 "$package_binding_stage_check" --check-evidence "$spec"
python3 "$layout_stage_check" --check-evidence "$spec"
python3 "$routing_stage_check" --check-evidence "$spec"
python3 "$local_validation_check" --check-evidence "$spec"
python3 "$autoroute_preflight_check" "$spec"
python3 "$route_score_check" "$spec"
if [ ! -f "$batch_check" ]; then
  echo "Missing kicad-production-pcb preflight script: $batch_check" >&2
  exit 2
fi
python3 "$batch_check" --auto-require --generated --generator-arg=--schematic-only "$spec"
python3 "$output_binding_check" --check-release "$spec"

report_dir="$(python3 -c 'import pathlib, sys, yaml; s=yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}; p=s.get("project", {}); print(pathlib.Path(str(p.get("artifacts_dir", "artifacts"))) / "local-validation" / str(p.get("name", pathlib.Path(sys.argv[1]).stem)))' "$spec")"
mkdir -p "$report_dir"
python3 scripts/final_gate.py --report-output "$report_dir/final-gate.json" "$@"

target="$(python3 -c 'import sys, yaml; print((yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}).get("manufacturing", {}).get("target", ""))' "$spec")"
if [ "$target" = "jlcpcb" ]; then
  python3 scripts/package_jlcpcb.py "$spec"
  has_source_snapshot="$(python3 -c 'import sys, yaml; spec=yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}; print("yes" if isinstance(spec.get("manufacturing", {}).get("jlcpcb", {}).get("assembly", {}).get("source_snapshot"), dict) else "no")' "$spec")"
  if [ "$has_source_snapshot" = "yes" ]; then
    python3 scripts/lcsc_source_snapshot.py "$spec"
  fi
  has_release="$(python3 -c 'import sys, yaml; spec=yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}; print("yes" if isinstance(spec.get("manufacturing", {}).get("jlcpcb", {}).get("release"), dict) else "no")' "$spec")"
  if [ "$has_release" = "yes" ]; then
    python3 scripts/release_jlcpcb.py "$spec"
  fi
  jlc_args=(--report-output "$report_dir/jlcpcb-gate.json")
  if [ "$production_required" = "yes" ]; then
    jlc_args+=(--production)
  fi
  if [ "$order_ready_required" = "yes" ]; then
    jlc_args+=(--order-ready)
  fi
  python3 scripts/jlcpcb_gate.py "${jlc_args[@]}" "$spec"
  if [ "$order_ready_required" = "yes" ]; then
    python3 "$evidence_manifest_check" --order-ready "$spec"
  fi
  if [ "$production_required" = "yes" ]; then
    python3 "$production_manifest_check" --write "$spec"
    python3 "$production_manifest_check" --check "$spec"
  fi
  has_order_status="$(python3 -c 'import sys, yaml; spec=yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}; print("yes" if isinstance(spec.get("manufacturing", {}).get("jlcpcb", {}).get("release", {}).get("order_review", {}).get("status_report"), dict) else "no")' "$spec")"
  if [ "$has_order_status" = "yes" ]; then
    python3 scripts/jlcpcb_order_status.py "$spec"
  fi
fi
