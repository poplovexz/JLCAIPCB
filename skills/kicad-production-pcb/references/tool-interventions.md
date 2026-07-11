# Tool Interventions Reference

Use this reference before using external PCB tools. The current `specs.yaml` flow remains authoritative.

## Component Import

Preferred order:

1. JLCImport for LCSC/JLCPCB-oriented symbol, footprint, and 3D model import.
2. `easyeda2kicad.py` as fallback for EasyEDA/LCSC conversion.

Rules:

- Import into a project-local library path.
- Import into a disposable candidate directory first; use `library_import_transaction.py` to dry-run and atomically promote all outputs plus provenance only after their checks pass.
- Add explicit `kicad.symbol_libraries` and `kicad.footprint_libraries` mappings.
- Record manufacturer, MPN, LCSC/JLC part, source URL, datasheet, package, and assembly status in the spec.
- Record raw source identity/hash and locked evidence ID, converter name/version/command, output hashes, an independent datasheet pin table, semantic symbol pins, exact pad/manufacturing-layer geometry, body height, 3D disposition, placement origin, polarity, orientation offset, and source locators.
- Treat imported libraries as inputs, not proof.

## Panelization

Use KiKit only when panelization is requested.

Rules:

- Start from a single board that already passes local gates.
- Read array, rails, tabs, V-cuts, mouse bites, fiducials, tooling holes, spacing, and output paths from the spec.
- Keep panel outputs separate from single-board outputs.
- Run panel checks separately.

## Advisory Review

Use kicad-happy only as a non-blocking second reviewer.

Rules:

- Run after local KiCad outputs exist.
- Triage findings as confirmed issue, false positive, or TODO.
- Never let advisory review override ERC, DRC, final_gate, JLCPCB gate, or external order-ready evidence.

## KiBot

Do not use KiBot as the default export path.

Use it only for explicit CI/CD, GitHub Actions, documentation automation, or migration comparison tasks. Keep KiBot outputs separate until equivalence with the current `run_flow.py` path is proven.

## Code-First and AI PCB Projects

Use projects such as atopile/faebryk, SKiDL, Circuit-Synth, Diode Zener, SchGen, Circuitron, KiCad MCP, and external AI autorouters as references for:

- modularity;
- typed interfaces;
- reusable blocks;
- net graph representations;
- structured tool APIs;
- benchmark-driven evaluation.

Do not replace the current spec-driven source of truth unless the user explicitly requests a migration and equivalent gates are proven.
