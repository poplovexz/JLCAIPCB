# Spec Freeze Stage

Use this stage after requirement intake, block architecture, bounded part selection, and required library binding are complete. It turns the first four stages into one local frozen product handoff package and is the final no-KiCad gate before generation.

## Contents

- Handoff package and local storage
- Authoritative data and freeze candidate contract
- Preview, transaction, and deterministic verification
- Invalidation, change requests, and downstream output binding

## Handoff Package

The package has four responsibilities and no duplicated design authority:

- `specs/<project>.yaml` is the machine source of truth for generation and gates.
- `product-baseline.md` is a deterministic human/Codex product document generated from that Spec.
- `spec-freeze.yaml` binds the Spec, generated product document, policies, executors, and upstream evidence by SHA256.
- Existing intake, architecture, sourcing, and library evidence remains the audit archive. It is not normal downstream context.

The product document starts with `PCB Build Brief`, followed by detailed generated sections for requirements, architecture, sourcing/library identity, formal PCB design inputs, verification, evidence, and change control. It is a view, not a second editable source. Never hand-maintain it.

After freeze, a downstream PCB task reads in this order:

1. Read `PCB Build Brief` only.
2. Read the frozen Spec sections owned by the current module or gate.
3. Open a bound architecture report, part-lock, library manifest, or raw capture only for an audit, freshness check, substitution, or approved change.

Do not search old chat, questionnaires, or web pages to fill a missing downstream decision. Record a change request against requirements, architecture, sourcing, or formal Spec ownership; resolve it upstream; regenerate the product document; and freeze a new revision. This prevents context-dependent design intent from bypassing machine checks.

## Local Storage

Keep all freeze data local:

- `specs/<project>.yaml` stores the small `spec_freeze` status/index block.
- `<project.artifacts_dir>/spec-freeze/<project>/product-baseline.md` stores the generated product handoff.
- `<project.artifacts_dir>/spec-freeze/<project>/spec-freeze.yaml` stores the immutable manifest.
- `<project.artifacts_dir>/spec-freeze/<project>/downstream-manifest.yaml` binds generated KiCad, ERC/DRC, and fabrication files to that freeze.
- The manifest binds the current spec, generated product baseline, trusted policy/renderers, architecture report, production part lock, and production library-binding manifest by SHA256.
- No network service, account, browser session, or cloud database is required.

`project.root_dir` is mandatory for frozen specs so paths resolve identically from Codex, hooks, CI, or another working directory. The manifest must remain under `project.artifacts_dir`.

## Authoritative Data

Do not create a parallel `selected_parts` section. Keep one source for each responsibility:

- `sourcing.part_lock`: selected manufacturer/supplier identity, package, evidence, quantity, and cost decision.
- `components`: instantiated design references and electrical pad-to-net declarations.
- `sourcing.downstream_binding`: verified local symbol, footprint, and complete pin-to-pad binding.

The freeze manifest binds all three. Changing any one makes the freeze stale.

## Freeze Candidate Contract

A local-MVP freeze candidate contains machine-readable requirements, confirmed architecture, power domains, KiCad library inputs, board constraints, manufacturing intent, components, nets, exact expected net graph, connectivity batches, verification dispositions, and structured TODO items.

A production candidate additionally contains a current transactional part lock and ready downstream library/pin-map binding. Production policy cannot be weakened from inside the project spec.

Board constraints use `board.constraint_dispositions`. Every required area is either:

- `defined`, with a rationale and references to actual machine-readable spec paths; or
- `not_applicable`, with a rationale.

Freeze constraints, not final layout results. Exact component coordinates and route geometry are required only when the chosen generator or a later layout gate needs them; they are not inherently required to describe the frozen design intent.

Manufacturing intent declares `target`, `mode`, positive `quantity`, and `required_outputs`. `bare-pcb` requires Gerber and drill. `pcba` also requires BOM and position output. Cost may remain a conservative estimate at this stage when its quantity, scope, margin, source, and confidence are recorded.

Open pre-fabrication TODO work must remain blocking. A non-blocking or not-applicable disposition needs an owner and rationale. Only trusted post-fabrication categories may remain open without blocking AI package generation.

## Transaction

Run:

```bash
python3 <skill>/scripts/product_baseline.py --preview specs/<project>.yaml
python3 <skill>/scripts/spec_freeze_transaction.py specs/<project>.yaml
python3 <skill>/scripts/spec_freeze_transaction.py --apply specs/<project>.yaml
python3 <skill>/scripts/spec_freeze_check.py --before-generation specs/<project>.yaml
python3 <skill>/scripts/product_baseline.py --check --require specs/<project>.yaml
```

The preview is explicitly marked non-frozen and never substitutes for the transaction. Use `--production` for an explicit production freeze review. The transaction:

1. Acquires a per-spec freeze lock.
2. Validates the freeze contract before expensive work.
3. Runs the policy-defined intake, beginner, architecture, closure, part-selection, readiness, verification, production-schema, exact-net, and connectivity-batch gates in order.
4. Stops at the first failed gate and does not run KiCad.
5. Rejects a spec changed while checks were running.
6. Renders the product baseline from the same checked in-memory Spec.
7. Writes the product baseline, manifest, and `spec_freeze` index in one durable transaction.

Do not hand-author a `frozen` status. Only a passing transaction may write it.

## Invalidation

The canonical digest covers the complete spec except the self-referential `spec_freeze` block and benchmark-only metadata. Any requirement, architecture, power, sourcing, library, component, net, connectivity, board, manufacturing, verification, TODO, or validation-policy change invalidates the freeze. The checker also deterministically rerenders `product-baseline.md`; missing, edited, stale, or differently rendered content blocks generation.

Changing a bound report, part-lock file, or library-binding manifest also invalidates it. A bundled policy update invalidates old freezes so projects are rechecked against the current standard.

After KiCad generation, `spec_output_binding.py --write-generated` records the generated project hashes. After ERC/DRC and fabrication export, `--write-release` records the reports and every file below the project fabrication directory. `--check-release` rejects a changed, missing, extra, or old output set. The standard export step cleans the spec-derived fabrication directory before exporting so obsolete Gerbers cannot survive into a new package.

JLCPCB package and release manifests also record the current freeze binding. Production/order-ready `jlcpcb_gate.py` first validates Spec Freeze, then rejects package or release manifests from another revision.

After an invalidation:

1. Return to `freeze-candidate` behavior; do not generate or reuse release outputs.
2. Resolve the failed upstream stage.
3. Rerun the dry run and apply transaction.
4. Regenerate KiCad and all downstream checks/exports from the new freeze.

Refreshing stock, price, or lifecycle evidence without changing the selected identity may be handled by the sourcing freshness workflow. Any substitution, package change, pin-map change, or changed design assumption is an upstream design change and requires a new lock/binding/freeze revision.

The migrated `run_flow.py` writes both downstream phases automatically. The pre-final hook and `final_gate.py` check the release phase and must not create or refresh it themselves.

Legacy/demo specs without a lifecycle stage remain compatible and receive a no-op warning. New `local-mvp`, production, and order-ready flows must pass Spec Freeze before generation.
