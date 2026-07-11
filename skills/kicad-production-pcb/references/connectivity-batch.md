# Connectivity Batch Reference

Use this reference when creating, debugging, or reviewing generated schematic connectivity.

## Goal

Codex must not debug PCB connectivity one wire at a time. The unit of work is a module-level connectivity batch, verified against an expected net graph.

## Required Data Model

Represent connectivity in the spec with modules, batches, and expected nets.

```yaml
modules:
  - id: power_input
    components: [J1, F1, D1, C1]
    provides: [VIN, GND]
    consumes: []

  - id: regulator_3v3
    components: [U2, C2, C3]
    provides: [+3V3]
    consumes: [VIN, GND]

connectivity_batches:
  - id: power_input
    module: power_input
    required_nets: [VIN, GND]
    required_pins: [J1.1, J1.2, F1.1, F1.2, D1.1, D1.2, C1.1, C1.2]

expected_net_graph:
  nets:
    VIN:
      class: power
      source: J1.1
      pins: [J1.1, F1.1]
    +3V3:
      class: power
      source: U2.OUT
      pins: [U2.OUT, C3.1]
    GND:
      class: ground
      shared: true
      pins: [J1.2, D1.2, C1.2, U2.GND, C3.2]
```

Use stable net names. Do not let Codex invent alternate spellings such as `3V3`, `+3.3V`, and `VCC3V3` in the same project.

## Batch Apply Protocol

For each batch:

1. Create a temporary spec copy.
2. Apply only one connectivity batch.
3. Generate a temporary KiCad project.
4. Export or parse the generated netlist.
5. Compare actual netlist to `expected_net_graph`.
6. Merge only if the batch passes.
7. Discard the temporary spec/project if the batch fails.

Never stack a new batch on top of a failed batch.

## Rollback Rules

Rollback source data, not hand-edited KiCad files.

- The rollback unit is the temporary spec/batch.
- The generated KiCad project is disposable during batch validation.
- Do not manually edit `.kicad_sch` to "fix" a failed batch unless the generator itself is being fixed.
- If rollback cannot restore the previous spec state, stop and report a blocked status.

## Validation Layers

Run the cheapest checks first:

1. Spec graph check: required modules, nets, pins, sources, and upstream nets exist.
2. Generated named-net check: exact Spec names and pins vs actual KiCad netlist.
3. Schematic-stage check: full pin disposition, power roles, mandatory paths, and strict ERC.
4. PCB generation and full board checks only after schematic evidence passes.

Do not run the full matrix to debug one connectivity batch.

## Failure Classes

Classify failures explicitly:

- `missing_pin_mapping`
- `missing_symbol_pin_number`
- `symbol_footprint_pad_mismatch`
- `net_name_mismatch`
- `expected_pin_unconnected`
- `unexpected_short`
- `missing_upstream_net`
- `missing_power_source`
- `ground_island`
- `pin_type_conflict`
- `module_boundary_violation`
- `rollback_failed`

The final blocked report must include the batch ID, failure class, affected refs/pins/nets, and the next required action.

## Wire and Label Policy

Use net labels for:

- MCU and module pins.
- Cross-module signals.
- Repeated connector channels.
- Power and ground nets that span modules.

Use direct wires only for short, obvious local connections where crossings cannot be ambiguous.

The net graph is the design truth. Wires and labels are KiCad implementation details, but every declared net must receive a generated global label so its KiCad name remains identical to the Spec.
