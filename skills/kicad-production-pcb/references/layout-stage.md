# PCB Layout Stage

Use this stage after current schematic and package-binding evidence, and before any route, DSN, or Freerouting candidate. Layout means mechanical geometry, component placement, keepouts, copper/return intent, and route constraints; it is not routing.

## Required Order

1. Freeze a machine-readable layout contract and placement candidate in the Spec.
2. Generate an unrouted PCB with `generate_project.py --layout-only`.
3. Inspect the actual `.kicad_pcb`, not only Spec declarations.
4. Write current layout-stage evidence.
5. Only then generate routes, export DSN, or run Freerouting.

If a placement candidate changes `components[].position_mm`, `side`, board outline, zones, or keepouts, apply it transactionally, rerun Spec Freeze, and regenerate affected schematic/package/layout evidence. Never patch a frozen PCB directly.

## Layout Contract

Modern MVP and production specs declare `layout.schema_version`, `state`, `revision`, `coordinate_system`, `constraints`, and `placement_batches`. Every component has `position_mm.{x,y,rotation,side}` and belongs to exactly one ordered batch.

Every required constraint area is either `defined` with concrete item IDs or `not_applicable` with a rationale. Concrete data belongs in the matching collection rather than prose:

- `board.outline.loops` for closed polygon loops;
- mechanical footprint refs for mounting holes, slots, fiducials, and tooling features;
- fixed placements and external connector edge/orientation contracts;
- footprint/copper/track/via keepout polygons;
- named copper zones and return-path intent;
- proximity and separation constraints;
- high-current, high-speed, antenna, thermal, and assembly constraints.

Use placement batches in this order: mechanical anchors, external connectors, antenna/absolute keepouts, power/protection, critical IC clusters, sensitive/high-speed blocks, thermal structures, then ordinary parts. Generate disposable whole-batch candidates and score them; do not move one component at a time on the active board.

## Gate Commands

```bash
python3 <skill>/scripts/layout_stage_gate.py --before-generation specs/<project>.yaml
python3 scripts/generate_project.py --layout-only specs/<project>.yaml
python3 <skill>/scripts/layout_stage_gate.py --after-generation --generator scripts/generate_project.py specs/<project>.yaml
python3 <skill>/scripts/layout_stage_gate.py --check-evidence specs/<project>.yaml
```

The after-generation check requires an unrouted board. The evidence check accepts a later routed board only when its normalized layout fingerprint still matches the accepted layout.

## PASS Meaning

PASS means the generated board implements the frozen outline, side/orientation/position, named zones and keepouts, fixed anchors, region rules, proximity/separation rules, board containment, and non-waived footprint overlap constraints. It also proves required high-current/high-speed/RF/thermal/return-path intents are structurally bound to layout objects.

PASS does not prove routed impedance, final current density, final return continuity, EMC, temperature, enclosure fit, or assembler orientation. Those require post-routing gates, web DFM, simulation, or post-fabrication evidence as appropriate.
