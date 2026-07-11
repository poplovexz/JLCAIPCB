# Autoroute Input Validity

Freerouting is useful only after the KiCad project is a valid autorouting input. It must not be used as a repair loop for missing libraries, broken symbol links, bad footprint links, stale netlists, or severe board-rule failures.

## Required Order

1. Pass `layout_stage_gate.py --check-evidence` for the current unrouted layout.
2. Generate or inspect the KiCad project from the current spec.
3. Run `autoroute_preflight_check.py`.
4. Only if it passes, export DSN or create a temporary unrouted candidate copy.
5. Run Freerouting candidates on disposable files.
6. Score candidates, then merge only a candidate that passes the normal KiCad/ERC/DRC/final gates.

## What This Gate Blocks

- Missing `.kicad_sch` or `.kicad_pcb` inputs.
- Project-local symbol or footprint library paths declared in `sym-lib-table` or `fp-lib-table` but missing on disk.
- ERC categories showing missing symbol libraries or broken footprint links.
- DRC categories showing footprint library issues, shorts, crossing tracks, or clearance failures.
- Unconnected items outside the selected routing batch/remaining planned batches. Planned unrouted nets are expected input, not an automatic preflight failure.

If this gate fails, do not delete routes, export DSN, or run Freerouting. Report the exact blocker and fix the project/spec/library inputs first.

## Direct KiCad Projects

For a project directory that was not created by the spec flow, run:

```bash
python3 <skill>/scripts/autoroute_preflight_check.py --project-dir projects/<name>
```

This gives a fast answer: whether the project is a safe input for autorouting experiments.

## Spec-Driven Projects

For a normal spec-driven flow, run:

```bash
python3 <skill>/scripts/autoroute_preflight_check.py specs/<project>.yaml
```

The check is inactive by default unless `routing.freerouting.enabled: true`, `validation.autoroute_preflight.required: true`, or `--require` is used. This keeps ordinary non-autorouted boards from paying the cost of extra KiCad CLI runs.
