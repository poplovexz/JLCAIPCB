# Installation and Codex Integration

## 1. Install the Personal Codex Skill

The default installer copies the skill to `${CODEX_HOME:-$HOME/.codex}/skills/kicad-production-pcb`:

```bash
./install.sh
```

Restart Codex so the skill registry is refreshed. Verify:

```bash
test -f "${CODEX_HOME:-$HOME/.codex}/skills/kicad-production-pcb/SKILL.md"
python3 skills/kicad-production-pcb/scripts/run_golden_benchmark.py
```

The installer refuses to replace a different existing skill unless `--force` is supplied. Review the diff before forcing an update.

## 2. Install Into a KiCad Project

```bash
./install.sh --project /path/to/project
```

This installs:

```text
<project>/codex-skills/kicad-production-pcb/
<project>/hooks/pre-final-pcb-flow.sh
<project>/templates/AGENTS.jlcaipcb.md
```

It does not overwrite the project's existing `AGENTS.md`. Merge the relevant instructions from `templates/AGENTS.jlcaipcb.md` into the project `AGENTS.md` after reviewing paths and commands.

## 3. Hook Contract

`hooks/pre-final-pcb-flow.sh` is a project workflow gate invoked by Codex before a final production or order-ready claim. It is not an undocumented native Codex lifecycle event.

The target project must provide these adapters:

```text
scripts/generate_project.py
scripts/run_flow.py
scripts/kicad_check.py
scripts/final_gate.py
scripts/package_jlcpcb.py             # when manufacturing.target is jlcpcb
scripts/jlcpcb_gate.py                # when manufacturing.target is jlcpcb
scripts/release_jlcpcb.py             # when release is configured
```

The bundled skill owns generic stage gates. The project owns KiCad generation and manufacturer-specific adapters because those implementations depend on the project's schema and output contract.

Run the hook explicitly:

```bash
hooks/pre-final-pcb-flow.sh specs/<project>.yaml --require-checks --require-fab
```

Override the installed skill scripts path when needed:

```bash
KICAD_PRODUCTION_PCB_SKILL_SCRIPTS="$HOME/.codex/skills/kicad-production-pcb/scripts" \
  hooks/pre-final-pcb-flow.sh specs/<project>.yaml --require-checks --require-fab
```

## 4. Project Layout

Recommended minimum layout:

```text
AGENTS.md
specs/<project>.yaml
scripts/
projects/<project>/
artifacts/
codex-skills/kicad-production-pcb/
hooks/pre-final-pcb-flow.sh
```

Keep paths, thresholds, manufacturer rules, commands, and project names in the spec or policy files. Do not hardcode board-specific values in shared gates.

## 5. Starting in Codex

For a new board:

```text
Use $kicad-production-pcb. I am a beginner. Start from requirement intake only. My use case is: ...
```

For an existing project:

```text
Use $kicad-production-pcb to inspect the current spec and evidence, report the current stage, and continue only from the first failed gate.
```

Codex must not enter KiCad generation before requirement confirmation, architecture, sourcing, and Spec Freeze are current.

## 6. Updating

```bash
git pull --ff-only
./install.sh --force
./install.sh --project /path/to/project --force
```

Rerun the golden benchmark and the target project's strict flow after every update.

## 7. External Web DFM

Browser MCP may continue directly when the JLCPCB/JLCDFM session is already authenticated. Login, CAPTCHA, QR, SMS, 2FA, account selection, payment, and order submission require explicit user action. Imported evidence must remain bound to the exact release fingerprint.
