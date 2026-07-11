# Block Architecture Stage

Use this reference after requirement intake is confirmed and before component sourcing, symbols, footprints, schematic connectivity, or KiCad generation.

## Contents

- Stage boundary
- Architecture contract
- Beginner ownership
- Deferred unknowns
- Gate and report

## Stage Boundary

Translate confirmed real-world intent into a directed block graph. Do not select exact parts or declare symbols, footprints, pins, pads, nets, placement, routes, or trace geometry inside `architecture`.

This stage decides:

- Power entry, power domains, conversion boundaries, sharing, and backfeed intent.
- Functional blocks and directed power/data/control relationships.
- External interfaces and connectors that must remain accessible.
- High-current, high-speed, RF, sensitive, noisy, thermal, and external-cable risk paths.
- Protection intent at each external or power boundary.
- Failure safe states, programming/debug/recovery, and test observability.
- Generic sourcing constraints for every required on-board block, without naming an exact part.
- Which professional unknowns may be deferred and which stage they block.

The architecture YAML is the design truth. The Markdown/Mermaid report is derived for human review.

## Architecture Contract

Use stable generic IDs. A minimal shape is:

```yaml
validation:
  architecture:
    required: true

architecture:
  revision: 1
  source_intake_revision: 1
  current_target: local-mvp
  state: ready
  summary: "Plain-language description of the selected block architecture."
  practical_choices_confirmed: true
  practical_choice_confirmation:
    status: confirmed
    confirmed_by: user
    architecture_revision: 1
    source_intake_revision: 1
    architecture_sha256: "<Confirmation SHA256 shown by architecture_report.py>"
    user_response_summary: "Confirmed the power style, exposed connector, and system boundary."
  technical_decision_owner: codex
  outputs:
    report_path: artifacts/architecture/<project>/architecture.md

  blocks:
    - id: POWER_IN
      category: power_entry
      role: "Receive external power"
      scope: onboard
      power_domains: [LOGIC_POWER]
      required: true
      selection_constraints:
        - id: POWER_INPUT_CAPABILITY
          kind: power
          statement: "Provide the declared power domain and protection intent."
          required: true
          required_before: component-sourcing
          criteria:
            - {attribute: functions, operator: contains, value: protected_power_entry}
    - id: CONTROL_CORE
      category: controller
      role: "Run control behavior"
      scope: onboard
      power_domains: [LOGIC_POWER]
      required: true
      selection_constraints:
        - id: CONTROL_FUNCTION_CAPABILITY
          kind: function
          statement: "Implement the requested control and recovery behavior."
          required: true
          required_before: component-sourcing
          criteria:
            - {attribute: functions, operator: contains, value: programmable_control}
    - id: EXTERNAL_IO
      category: interface
      role: "Expose the external interface"
      scope: onboard
      power_domains: [LOGIC_POWER]
      required: true
      selection_constraints:
        - id: EXTERNAL_INTERFACE_CAPABILITY
          kind: interface
          statement: "Provide the exposed interface and boundary protection."
          required: true
          required_before: component-sourcing
          criteria:
            - {attribute: functions, operator: contains, value: protected_external_interface}
    - {id: EXTERNAL_DEVICE, category: external_device, role: "Represent the off-board device", scope: external, power_domains: [], required: true}

  block_edges:
    - {id: EDGE_POWER_CONTROL, from: POWER_IN, to: CONTROL_CORE, kind: power, direction: source_to_sink}
    - {id: EDGE_POWER_IO, from: POWER_IN, to: EXTERNAL_IO, kind: power, direction: source_to_sink}
    - {id: EDGE_CONTROL_IO, from: CONTROL_CORE, to: EXTERNAL_IO, kind: data, direction: bidirectional}
    - {id: EDGE_IO_EXTERNAL, from: EXTERNAL_IO, to: EXTERNAL_DEVICE, kind: data, direction: bidirectional}

  power_domains:
    - id: LOGIC_POWER
      source_block: POWER_IN
      consumer_blocks: [CONTROL_CORE, EXTERNAL_IO]
      voltage_class: regulated_low_voltage
      current_class: low
      sharing: dedicated
      backfeed_policy: blocked
      protection_intent: required
      required_before: local-mvp

  interfaces:
    - id: EXTERNAL_LINK
      from: EXTERNAL_IO
      to: EXTERNAL_DEVICE
      kind: data
      direction: bidirectional
      external: true
      speed_class: low
      voltage_domain: LOGIC_POWER
      risk_tags: [external_cable, user_accessible]
      required_before: local-mvp

  external_connectors:
    - id: CONNECTOR_EXTERNAL
      block_id: EXTERNAL_IO
      interface_ids: [EXTERNAL_LINK]
      exposure: user_accessible
      hot_plug: "no"
      protection_intent: required
      rationale: "The interface leaves the board and is accessible."
      required_before: local-mvp

  risk_paths:
    - id: RISK_EXTERNAL_CABLE
      kind: external_cable
      block_ids: [EXTERNAL_IO, EXTERNAL_DEVICE]
      interface_ids: [EXTERNAL_LINK]
      power_domain_ids: []
      connector_ids: [CONNECTOR_EXTERNAL]
      reason: "The signal crosses the board boundary."
      constraints: ["Keep boundary protection close to the connector."]
      required_before: local-mvp

  protection_intents:
    - {id: PROTECT_POWER, boundary_id: LOGIC_POWER, threats: [overcurrent, reverse_polarity], disposition: required, strategy_classes: [current_limit, polarity_control], rationale: "Protect the power entry.", required_before: local-mvp}
    - {id: PROTECT_EXTERNAL, boundary_id: CONNECTOR_EXTERNAL, threats: [electrostatic_discharge], disposition: required, strategy_classes: [clamp], rationale: "Protect the user-accessible interface.", required_before: local-mvp}

  failure_states:
    - {id: FAIL_EXTERNAL_DISCONNECT, trigger: "External device is disconnected", affected_blocks: [EXTERNAL_IO, CONTROL_CORE], safe_state: "Keep outputs inactive and continue reporting status.", required_before: local-mvp}

  test_and_debug:
    - {id: TEST_CONTROL, kind: programming, target_blocks: [CONTROL_CORE], disposition: required, rationale: "The control block needs a recovery path.", required_before: local-mvp}

  requirement_coverage:
    - {requirement_path: intake.user_intent, block_ids: [CONTROL_CORE, EXTERNAL_IO], rationale: "These blocks implement the requested behavior."}
    - {requirement_path: intake.success_criteria, block_ids: [CONTROL_CORE, EXTERNAL_DEVICE], rationale: "These blocks produce the observable result."}
    - {requirement_path: intake.system_boundary, block_ids: [EXTERNAL_IO, EXTERNAL_DEVICE], rationale: "These blocks define the board boundary."}

  hazard_coverage: []
  open_decisions: []
```

## Beginner Ownership

Require the user to confirm only practical behavior: function, device count, power source style, exposed connectors, size, and mounting. Set `technical_decision_owner: codex` for AI-owned topology decisions supported by requirements and evidence.

Never assign a `technical` open decision to a beginner user. Use owner `codex`, `evidence`, or `engineer`. Ask the user only when the unresolved choice changes visible behavior or the safe system boundary.

The intake confirmation and architecture confirmation are separate. Render the architecture report with `practical_choices_confirmed: false`, show the practical choices to the user, then record `practical_choice_confirmation` only after an explicit response. Copy the report's `Confirmation SHA256`, set `practical_choices_confirmed: true`, and regenerate the report. The digest excludes the confirmation fields themselves, so recording the confirmation does not change the value being confirmed. When intake includes `budget_intent`, architecture requirement coverage must trace it to the blocks whose topology and sourcing dominate cost.

## Deferred Unknowns

Use `unknown` or `unresolved` only with `required_before` and a matching unresolved `open_decisions[].blocks` stage. Use `component-sourcing` when an answer is required before candidate search and `part-lock` when it is required before the final part is locked. An unknown may continue only when its blocking stage is later than the gate's effective target.

For example, a production sourcing detail may remain deferred during `local-mvp`, but a power-path decision marked `required_before: local-mvp` blocks local generation.

When requirements change, increment the intake revision and regenerate the architecture. When sourcing cannot satisfy the architecture, create a revised architecture candidate; do not silently mutate the confirmed use case.

## Boundary And Sourcing Handoff

Every electrical/data/control edge that crosses from an on-board or mixed block to an external block must have a matching `interfaces[]` entry with `external: true`. That interface must be assigned to exactly one connector whose `block_id` is the board-side endpoint. Every external interface needs an external-cable risk path even when the connector is marked `internal_service`; the connector must retain its explicit protection disposition.

Before selecting parts, every required on-board or mixed block must declare at least one generic `selection_constraints[]` item. Each required constraint also needs machine-readable `criteria` with an attribute, operator, value, and unit when numeric; keep these as capability limits rather than exact part identities. Selected sourcing requirements and components later reference the constraint IDs.

```yaml
architecture_trace:
  block_ids: [CONTROL_CORE]
  constraint_ids: [CONTROL_FUNCTION_CAPABILITY]
```

Constraint statements containing unresolved markers such as `TODO`, `TBD`, or `UNKNOWN` fail the architecture gate. `sourcing_context_check.py` additionally rejects missing or non-machine-readable criteria, and `part_selection_check.py` fails unless every required block, constraint, and procured component has transactional part-lock coverage. Explicit `manual_fit`/`hand_assembly` parts still need sourcing requirements; pure DNP or virtual placeholders are exempt. The report path must resolve under `project.artifacts_dir`; absolute paths or `..` traversal outside that root are rejected.

## Gate And Report

Run before sourcing:

```bash
python3 <skill>/scripts/architecture_report.py specs/<project>.yaml
python3 <skill>/scripts/architecture_gate.py --before-sourcing specs/<project>.yaml
```

Run before KiCad generation:

```bash
python3 <skill>/scripts/architecture_gate.py --before-generation specs/<project>.yaml
```

Passing proves that the declared block graph, board boundary, sourcing constraints, user-confirmation binding, and risk dispositions are structurally complete for the current target. It does not prove component availability, detailed electrical correctness, SI/PI/EMC performance, thermal behavior, or physical function.
