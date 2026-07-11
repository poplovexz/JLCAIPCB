# Supply Chain First

Read this reference after the confirmed block architecture passes and before symbols, footprints, pin maps, schematic connectivity, or layout are locked.

## Stage Contract

Use this order:

```text
confirmed requirements
-> ready block architecture
-> sourcing context
-> component-role decomposition and cost allocation
-> bounded candidate batches
-> hard-constraint rejection
-> selected-set compatibility and aggregate budget
-> deterministic ranking
-> transactional part lock
-> symbol/footprint import
-> verified library/pin-map binding
```

This is a no-KiCad stage. Component references may exist as role skeletons, but exact symbols, footprints, pins, pads, nets, placement, and routes must not drive the selection.

The main `specs/<project>.yaml` stores only the sourcing context, generic procurement requirements, artifact paths, final lock summary, and selected component bindings. Candidate lists, captured evidence, ranking details, and the complete lock stay under `project.artifacts_dir` so they do not consume the normal Codex context.

## Sourcing Context

Declare `sourcing.schema_version`, `sourcing.context`, `sourcing.roles`, `sourcing.requirements`, `sourcing.compatibility_constraints`, and `sourcing.artifacts`.

Use `assets/sourcing-stage-template.yaml` and `assets/candidate-manifest-template.yaml` as data-shape templates. Replace every `TODO`; the gates intentionally reject unfilled template values.

The context must define:

- procurement region and currency;
- board order quantity and attrition rate;
- maximum candidates per requirement and maximum search rounds;
- stock and lock freshness limits;
- suppliers/assemblers with allowed domains and component part-number fields;
- whether PCBA is enabled and whether assembly-side availability is mandatory.
- an explicit `project.root_dir` for working-directory-independent paths;
- `cost_budget`, allocated by Codex from the confirmed user budget: component caps, whole-order cap, and included PCB/assembly/shipping/tax estimate lines.

The user supplies only a practical per-board or whole-order limit. Codex allocates the cost categories. An unknown budget may continue to local MVP, but cannot close production sourcing.

Decompose physical functions into `sourcing.roles`. Roles may share a component, but must cover protection intents, external connectors, power domains, and test/debug paths; dependencies must be acyclic. This prevents one generic module capability from hiding omitted support or protection functions.

Every procurement requirement must define:

- a generic requirement ID;
- the component references covered by the requirement;
- exact quantity per board;
- assembly disposition;
- architecture block and constraint IDs;
- component role IDs;
- optional component-level machine criteria.

`quantity_per_board` must equal the number of component references in the requirement. Group all references expected to use the same supplier part into one requirement so stock is counted once; split mixed values, packages, or ratings into separate requirements. Every procured component must belong to exactly one requirement.

Run:

```bash
python3 <skill>/scripts/sourcing_context_check.py --require specs/<project>.yaml
```

## Machine Constraints

Every required architecture `selection_constraint` needs one or more `criteria` entries. A criterion uses an attribute, operator, value, and unit when numeric.

```yaml
selection_constraints:
  - id: INPUT_VOLTAGE_LIMIT
    kind: voltage
    statement: Tolerate the declared input envelope.
    required: true
    required_before: component-sourcing
    criteria:
      - attribute: absolute_input_voltage_max
        operator: gte
        value: 12
        unit: V
```

Candidate capabilities carry values and evidence IDs:

```yaml
capabilities:
  absolute_input_voltage_max:
    value: 18
    unit: V
    evidence_ids: [DATASHEET_EVIDENCE]
```

Supported operators, conditional/compound criteria, ranges, Decimal unit conversion, and explicit derating/margin multipliers live in `assets/sourcing-stage-policy.yaml`. Extend policy data instead of adding board-specific comparisons to Python.

## Candidate Acquisition

Codex collects current candidate data with the available browser, supplier API, MCP, or project adapter. The skill does not hardcode a supplier endpoint because authentication, regions, page structures, and APIs can change.

Write the normalized candidate manifest to `sourcing.artifacts.candidates`. Each architecture-derived requirement gets one batch containing:

- bounded query history and a stop reason;
- manufacturer, MPN, supplier part number, and supplier URL;
- package name, KiCad footprint candidate, pin count, body dimensions, and package confidence;
- stock, MOQ, order multiple, observation time, price, and lifecycle state;
- separate PCBA-provider stock, placement support, and library class when assembly is requested;
- manufacturer datasheet, supplier snapshot, and assembly snapshot evidence;
- distinct local evidence files, SHA256 digests, capture method, final URL, media type, byte size, timestamps, and content-verifiable assertions;
- machine-readable capabilities linked to evidence IDs.

All local evidence remains under `project.artifacts_dir`. Production providers use bundled trust profiles; project policy overrides and rewound clocks are forbidden. The test evidence profile works only with the explicit test environment. This raises evidence integrity but is not cryptographic proof that a remote page was honest.

The required orderable quantity is:

```text
ceil(board_quantity * quantity_per_board * (1 + attrition_rate))
-> raise to MOQ
-> raise to order multiple
```

An observed stock value of zero remains zero. LCSC/distributor stock and JLCPCB/assembler placement availability are separate required observations for PCBA parts.

## Batch Validation And Ranking

Run candidate collection by architecture-derived requirement, but validate the complete set before locking anything:

```bash
python3 <skill>/scripts/candidate_batch_check.py --require specs/<project>.yaml
python3 <skill>/scripts/candidate_rank_check.py --require specs/<project>.yaml
```

Hard constraints run before ranking. A malformed, stale, unsupported, out-of-stock, evidence-mismatched, or capability-mismatched candidate is rejected and its reasons are recorded. It cannot win through a high soft score.

Before ranking, the complete selected set must satisfy architecture-linked voltage/protocol/interface constraints. Cost uses the applicable quantity price tier, MOQ/order-multiple waste, and declared non-component estimates; component and order totals must fit the confirmed budget. Ranking uses stock margin, assembly class, landed purchase cost, evidence completeness, and package confidence. Candidate ID is the deterministic tie breaker.

If a required batch has no qualified candidate:

1. Do not modify the main spec.
2. Stop after the configured search-round limit.
3. Report the unsatisfied criterion and rejected candidate reasons.
4. Propose a new architecture candidate only when the confirmed architecture is infeasible.
5. Never silently weaken an architecture constraint or change the confirmed use case.

## Transactional Part Lock

Dry-run the complete transaction first:

```bash
python3 <skill>/scripts/part_lock_transaction.py specs/<project>.yaml
```

Apply only after every required batch has a qualified candidate:

```bash
python3 <skill>/scripts/part_lock_transaction.py --apply specs/<project>.yaml
```

The transaction recomputes checks and ranking, then writes the ranking, immutable lock, advisory alternatives, and identity/package candidates with a durable recovery journal. It requires `ruamel.yaml` for lossless spec round trips and fails closed if unavailable. A partial lock is forbidden; alternatives are never automatic substitutions.

The lock binds:

- architecture SHA256;
- sourcing-context SHA256;
- candidate-manifest SHA256;
- ranking SHA256;
- selected candidate and component references;
- manufacturer/MPN/supplier identity;
- physical package, suggested footprint candidate, and assembly disposition;
- evidence digests, score, and order quantity;
- compatibility results, applied price, MOQ waste, component total, and order estimate;
- lock timestamp and freshness window.

Run before symbol/footprint import:

```bash
python3 <skill>/scripts/part_lock_check.py --require specs/<project>.yaml
python3 <skill>/scripts/part_selection_check.py --require specs/<project>.yaml
```

`part_selection_check.py` invokes the lock check itself, so migrated `run_flow.py` integrations that already call it automatically receive the new sourcing gate.

After import, create a manifest from `assets/library-binding-manifest-template.yaml`. Every locked ref needs hashed project-local `.kicad_sym`/`.kicad_mod` files, verified package/pin count, and a full pin-to-pad map tied to locked evidence. The gate parses both KiCad S-expressions and requires their actual pin/pad sets to equal the map. Then run:

```bash
python3 <skill>/scripts/library_binding_transaction.py specs/<project>.yaml artifacts/.../library-binding.yaml
python3 <skill>/scripts/library_binding_transaction.py --apply specs/<project>.yaml artifacts/.../library-binding.yaml
python3 <skill>/scripts/part_selection_check.py --require --before-generation specs/<project>.yaml
```

## Relock And Invalidation

Changing the architecture, sourcing context, candidate evidence, ranking, or lock file invalidates the corresponding SHA binding.

When a different candidate is selected, the transaction marks `sourcing.downstream_binding.status: invalidated`. Do not run KiCad generation until the next symbol/footprint/pin-map stage refreshes its evidence against the new part-lock SHA.

Refreshing stock evidence for the same selected identity may update the lock without invalidating symbol/footprint work, but every downstream binding must point to the current lock SHA.

## Meaning Of PASS

A sourcing PASS proves that:

- all architecture-derived procurement requirements have qualified candidates;
- machine constraints were evaluated against evidence-linked capabilities;
- quantities include board count, per-board usage, attrition, MOQ, and order multiple;
- component roles and selected-set compatibility cover the architecture;
- selected purchase cost and non-component estimates fit the confirmed budget;
- supplier stock and PCBA support evidence were fresh at the recorded time;
- ranking was deterministic;
- selected spec fields match an immutable part lock.

After library binding, PASS also proves that recorded symbol/footprint/pin-map artifacts match the current lock. It still does not reserve inventory, guarantee future price, prove functional behavior, or replace web DFM and the final quote. Recheck fresh stock and quoted total before order-ready release.
