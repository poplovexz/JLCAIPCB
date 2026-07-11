# Beginner Intake Reference

Use this reference when the user describes only a usage scenario or cannot answer professional electrical questions.

## Contents

- Intake contract
- Question and evidence rules
- Safety screening
- Confirmation state transition
- Conservative MVP behavior

## Intake Contract

The user describes what the board should do. Codex extracts a requirements snapshot; it does not start component selection or KiCad work in this stage.

Keep the desired destination separate from the current workflow state. A user may want an `order-ready` result while the current state remains `requirements-only`.

Use this structure for a beginner intake artifact:

```yaml
intake:
  project_name: "Working title"
  user_intent: "Plain-language real-world function."
  input_style: use_case_only
  use_environment: "Known environment or Unknown."
  connected_devices: "Known devices or Unknown."
  power_source: "Known source or Unknown."
  size_or_mechanical: "Known limits or Unknown."
  manufacturing_intent: "Prototype, bare PCB, PCBA, or Unknown."
  success_criteria: "Observable behavior that means the board works."
  system_boundary: "What belongs on the board and what remains external."
  failure_consequence: "Practical consequence of failure or Unknown."
  desired_end_target: order-ready
budget_intent:
  status: unknown
  scope: unknown
  currency: CNY
  target_amount: null
  maximum_amount: null
  quantity_basis: null
  includes: [components, pcb-fabrication, assembly]
  priority: balanced
  user_statement: "The user is not sure yet; keep cost visible during design."
  allow_mvp_without_limit: true
safety_screening:
  level: standard
  hazards: []
  rationale: "No elevated-risk use was described."
  blocks_automatic_assumptions: false
evidence_inputs:
  available: []
  requested:
    - type: label_photo
      reason: "A label can identify a device the user cannot name."
      required_before: draft-spec
beginner:
  input_style: use_case_only
  question_strategy: practical_minimum
  round: 1
  max_rounds: 2
  max_questions: 5
  resolution_mode: ask_user
  questions:
    - id: q-power-source
      category: power_source
      question: "How would you prefer to power it?"
      choices: ["USB", "Battery", "External adapter", "Not sure"]
      recommended_choice: "External adapter"
      allow_unknown: true
      reason: "This selects the first power architecture."
      blocks: architecture
  deferred_professional_topics: []
missing_information:
  must_ask_now: []
  can_defer: []
  blocks_mvp: []
  blocks_production: []
  blocks_order_ready: []
safe_assumptions:
  - id: safe-mvp-stage
    assumption: "Keep the first implementation at local MVP."
    reason: "The input is scenario-only."
    risk: "It cannot be described as production-ready."
    stage_allowed: local-mvp
unsafe_assumptions:
  - id: unsafe-electrical-ratings
    unknown: "Exact electrical and production ratings."
    why_not_guess: "They affect safety, footprints, and manufacturing."
    required_before: production-package
confirmation:
  status: pending
  confirmed_by: none
  intake_revision: 1
  confirmed_revision: 0
  user_response_summary: "Awaiting user confirmation."
decision:
  current_target: requirements-only
  can_create_spec: false
  can_generate_kicad: false
  can_run_erc_drc: false
  next_action: "Ask the practical questions and request confirmation."
```

## Question And Evidence Rules

- Ask no more than five questions per round and no more than two rounds.
- Ask only architecture-changing practical questions. Use a policy-defined category such as function, quantity, power source, interfaces, environment, mechanical, manufacturing, success criteria, safety consequence, or system boundary.
- Capture a plain-language board or prototype-order budget. Offer practical ranges plus `Not sure`; do not ask a beginner to split PCB, BOM, setup, or extended-library fees.
- Give two to six contextual choices, include an explicit unknown/not-sure choice, recommend one concrete choice, and allow the user to answer unknown.
- When the user cannot identify a device, request a photo, label, connector image, product link, manual, enclosure drawing, or dimension photo instead of asking for part numbers.
- Move professional details into unsafe assumptions or deferred topics. The bilingual forbidden-term vocabulary lives in `assets/requirement-intake-policy.yaml`, not in the checker code.

## Safety Screening

Classify the scenario as `standard`, `elevated`, `safety-critical`, or `unknown`. Elevated, safety-critical, and unknown scenarios block automatic safety assumptions. Record the practical hazard without asking the beginner to size protection components.

Safety screening identifies the verification level; it does not perform electrical design. Risk terms and levels are policy data so projects can extend them without changing Python code.

## Confirmation State Transition

1. First report: `pending`, `requirements-only`, and all three permission flags are false.
2. After an explicit user confirmation: set `status: confirmed`, `confirmed_by: user`, and `confirmed_revision` equal to `intake_revision`. Set `can_create_spec: true`; keep KiCad/ERC/DRC permissions false while the draft spec is being created.
3. If any confirmed intake content changes, increment `intake_revision`, reset confirmation to pending, and ask the user to confirm again.
4. After the confirmed intake has been converted into a sufficient MVP spec, set the current target and generation permissions for the next stage. `--before-generation` still blocks requirements-only, draft, unconfirmed, or permission-denied input.

Never treat silence, an AI recommendation, or an AI-written summary as user confirmation.

## Conservative MVP Behavior

- Prefer a draft or `local-mvp` when professional data is unavailable.
- An unknown budget may continue only as requirements/draft/local MVP. Before a production-package target, convert it into a currency, quantity basis, included-cost scope, and maximum amount; sourcing later computes the component portion rather than asking the user to estimate it.
- Prefer external load power over powering unknown loads from data connectors.
- Add protection, filtering, and measurement provisions where uncertainty is high.
- Keep unresolved current, thermal, sourcing, package, enclosure, and DFM decisions as structured production blockers.
- Run local generation and ERC/DRC only after intake confirmation, spec closure, net graph, and connectivity batch gates permit it.
- Never claim production-ready or order-ready from a beginner intake alone.
