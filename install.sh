#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
codex_home="${CODEX_HOME:-$HOME/.codex}"
project_dir=""
force="no"

usage() {
  echo "Usage: ./install.sh [--codex-home PATH] [--project PATH] [--force]" >&2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --codex-home)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      codex_home="$2"
      shift 2
      ;;
    --project)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      project_dir="$2"
      shift 2
      ;;
    --force)
      force="yes"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

install_tree() {
  local source="$1"
  local target="$2"
  if [ -e "$target" ] && ! diff -qr "$source" "$target" >/dev/null 2>&1 && [ "$force" != "yes" ]; then
    echo "Refusing to replace different existing content: $target (use --force after review)" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$target")"
  rm -rf "$target"
  cp -a "$source" "$target"
}

install_tree "$repo_root/skills/kicad-production-pcb" "$codex_home/skills/kicad-production-pcb"
echo "Installed Codex skill: $codex_home/skills/kicad-production-pcb"

if [ -n "$project_dir" ]; then
  project_dir="$(mkdir -p "$project_dir" && cd "$project_dir" && pwd)"
  install_tree "$repo_root/skills/kicad-production-pcb" "$project_dir/codex-skills/kicad-production-pcb"
  mkdir -p "$project_dir/hooks" "$project_dir/templates"
  if [ -e "$project_dir/hooks/pre-final-pcb-flow.sh" ] && ! cmp -s "$repo_root/hooks/pre-final-pcb-flow.sh" "$project_dir/hooks/pre-final-pcb-flow.sh" && [ "$force" != "yes" ]; then
    echo "Refusing to replace different project hook: $project_dir/hooks/pre-final-pcb-flow.sh" >&2
    exit 1
  fi
  cp "$repo_root/hooks/pre-final-pcb-flow.sh" "$project_dir/hooks/pre-final-pcb-flow.sh"
  cp "$repo_root/templates/AGENTS.jlcaipcb.md" "$project_dir/templates/AGENTS.jlcaipcb.md"
  chmod +x "$project_dir/hooks/pre-final-pcb-flow.sh"
  echo "Installed project skill and hook: $project_dir"
  echo "Review templates/AGENTS.jlcaipcb.md and merge it into the project's AGENTS.md."
fi
