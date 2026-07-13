---
name: kicad-production-pcb
description: Use when generating, starting, migrating, reviewing, checking, exporting, or gating KiCad PCB projects, including PCB specs.yaml, confirmed requirement intake, block architecture gates, bounded component candidates, machine-readable sourcing constraints, deterministic ranking and transactional part locks, local transactional Spec Freeze manifests, spec closure, transactional batch validation, requirements and power/current gates, TODO blockers, SI/PI/EMC/thermal risk prechecks, KiCad schematic/board files, connectivity batches, net graph validation, Freerouting candidates and route scoring, ERC, DRC, Gerber, drill, BOM, pick and place, JLCPCB/嘉立创 PCBA release, order-ready evidence, fabrication readiness, production PCB workflows, clarifying user intent before generation, LCSC/JLCPCB component import, KiKit panelization, evidence verification, or non-blocking KiCad design review.
---

# KiCad Production PCB

Use this skill for KiCad PCB work that must be checkable and fabrication-oriented.

## Invocation / Startup

When a user says to "use the kicad-production-pcb skill", "start a PCB design", "continue the PCB flow", "migrate the PCB flow", or "make a JLCPCB/嘉立创 PCBA project", treat this skill as the entrypoint.

## Intake Gate

Before inspecting templates, choosing a spec, generating files, or running validation, collect and confirm the starting information. This is mandatory for new boards and optional only when the user is clearly continuing an existing named spec.

For a new board, the first output must be a Requirement Intake Report. This stage must not run KiCad, ERC, DRC, Freerouting, component import, JLCPCB upload, or fabrication export.

For beginner or use-case-only input, read `references/beginner-intake.md` and use its complete report schema. The intake must:

- Keep `intake.desired_end_target` separate from `decision.current_target`.
- Capture success criteria, system boundary, failure consequence, and a practical safety classification.
- Capture a plain-language per-board or prototype-order budget, included cost scope, quantity basis, and cost priority. `Not sure` may continue only to requirements/draft/local MVP; production-package requires a confirmed limit.
- Ask at most five contextual multiple-choice questions per round and at most two rounds. Every question must offer an unknown/not-sure choice and a concrete recommendation.
- Request photos, labels, links, manuals, or dimension evidence when the user cannot name a device or constraint.
- Put professional unknowns into structured assumptions and blockers instead of asking the beginner for implementation parameters.
- Remain `requirements-only` with spec/KiCad/ERC/DRC permissions false until the user explicitly confirms the current intake revision.

Save the report under a project-derived artifact path such as `artifacts/intake/<project>.yaml`. Do not treat silence or an AI-authored recommendation as user confirmation. If confirmed intake content changes, increment its revision and reset confirmation to pending.

If required intake information affects the next step, ask the user before proceeding. Do not silently invent project scope from a vague request.

Run `python3 <skill>/scripts/requirement_intake_gate.py <intake-report>.yaml` on saved intake reports. In migrated repositories, `run_flow.py` and `hooks/pre-final-pcb-flow.sh` must run `requirement_intake_gate.py --before-generation specs/<project>.yaml`; this is no-op for legacy specs without an intake report, but it blocks generation when a spec/report says the work is still in requirements-only or draft-spec intake.

After confirmation, restate the intake briefly and classify the current task as one of:

- `requirements-only`: collect intent and produce questions/spec outline, no KiCad generation.
- `draft-spec`: create or update `specs/<project>.yaml`, but do not claim production readiness.
- `local-mvp`: generate KiCad files and run local ERC/DRC/export gates after requirements are sufficient.
- `production-package`: local fabrication/JLCPCB package gates only.
- `order-ready`: requires external JLCPCB/JLCDFM evidence after local production gates pass.

Only a user-confirmed intake revision may advance to the Clarification Gate. A confirmed intake permits draft spec creation; it does not by itself permit KiCad generation or production claims. If the intake is incomplete, stop with `BLOCKED: intake incomplete` and ask only the next practical questions allowed by the round budget.

## Clarification Gate

Before creating or modifying `specs/<project>.yaml`, decide whether the request is a simple known demo board or a real product board.

Simple known demo boards may proceed from an existing passing spec when the user explicitly asks for a flow test, tutorial, or minimal LED/resistor-style board.

Real product boards must start with a requirements pass. Treat a board as real/complex if it includes any MCU/module, data interface, battery, external load, high-current rail, wireless module, power conversion, protection circuit, enclosure constraint, PCBA sourcing, or JLCPCB order-ready claim.

For real/complex boards:

1. Extract the user's use case into a short intent summary.
2. Identify missing decisions that affect schematic, power, PCB geometry, BOM, PCBA sourcing, safety margin, or fabrication.
3. Ask the smallest set of high-impact questions needed to continue. Use beginner-friendly wording based on use case; include electrical terms only when needed.
4. If the user is beginner/use-case-only and cannot answer professional parameters, create a conservative beginner MVP profile instead of blocking immediately. Keep the result as `local-mvp` or draft, encode assumptions in `assumption_profile`, and put production-impacting unknowns into structured TODO items.
5. Stop and report `BLOCKED: requirements incomplete` only when the missing decision prevents even a conservative MVP architecture, or when the user requests production/order-ready output without the required professional data. Do not run the validation matrix, open JLCPCB, or attempt order-ready evidence while blocked.
6. After the user answers, update/create the spec and include unresolved assumptions in `TODO.md`. If an unresolved item affects safe fabrication, component choice, or production ordering, keep the result as MVP/draft and do not claim production-ready.

Mandatory question areas for complex boards:

- Function: what the board controls/senses, expected channels, and success behavior.
- Power: input source, voltage range, peak/current budget, whether a data connector may power loads, protection needs, and connector type.
- Key parts: exact MCU/module variant, load/sensor models, voltage/current ratings, packages, and acceptable substitutes.
- Interfaces: data role, programming/debug method, buttons, indicators, wireless, external connectors, cable orientation, and pinout.
- Mechanical: board size limits, mounting holes, enclosure/standoff constraints, connector placement, and keepout areas.
- Manufacturing: bare PCB vs JLCPCB PCBA, hand-soldered/DNP parts, target quantity, assembly side, LCSC/JLC part requirements, and order review evidence.
- Risk review: thermal/current margin, ESD/reverse-polarity/fuse needs, noise/EMI, datasheet uncertainty, and anything requiring manual confirmation.

For MCU/module boards that drive external loads, never proceed to production/order-ready unless the load count, load voltage, peak/stall/inrush current or model, power connector/current rating, controller/load power sharing, programming/debug approach, exact controller/module variant, assembly plan, board size/mounting constraints, and protection requirements are known. For beginner MVP, use conservative assumptions and structured blocking TODO items for the unknown professional decisions.

If the user explicitly says to proceed with assumptions, state the assumptions first and ask for confirmation when they affect current, voltage, safety, connector choice, or production ordering. Do not silently assume those values.

## Tool Intervention Model

Keep `specs.yaml` as the source of truth. Do not replace the core flow with atopile, faebryk, SKiDL, KiBot, kicad-happy, or an imported EasyEDA/JLC project. External tools may only add evidence, libraries, panel outputs, or advisory review around the spec-driven KiCad flow.

Use this order:

1. Requirement Intake Gate and Clarification Gate define user intent; if the user is beginner/use-case-only, the Beginner Use-Case Gate downgrades the target to conservative MVP before professional checks. Requirement intake is a no-KiCad stage.
2. Block Architecture Gate converts the confirmed intake into a directed module/power/interface/risk graph, generic sourcing constraints, an architecture-specific user confirmation, and a derived report. This is still a no-KiCad, no-exact-part stage.
3. JLC/LCSC supply-chain-first selection locks part identities, physical packages, evidence, selected-set compatibility, and aggregate estimated cost after architecture passes; footprints remain candidates here.
4. Component import creates project-local libraries, then a verified transaction binds symbols, footprints, packages, and pin maps to the current part lock.
5. The formal spec records requirements, architecture, power domains, locked-part references, library bindings, nets, connectivity batches, board constraints, manufacturing target, verification, and TODO dispositions as the sole machine truth.
6. Flow, closure, readiness, SI/PI/EMC/thermal risk, schema, exact-net, and connectivity gates validate one freeze candidate without running the main KiCad generator.
7. `spec_freeze_transaction.py` atomically stores a generated `product-baseline.md`, the local frozen index, and SHA-bound manifest; `spec_freeze_check.py` blocks generation after any input, document, evidence, renderer, or trusted-policy change.
8. Downstream work reads the baseline's `PCB Build Brief`; generate and independently gate the schematic before PCB generation, then bind exact named nets, strict ERC, and generator evidence.
9. Run the Package Binding Stage after strict ERC, then the PCB Layout Stage; only current schematic, package-binding, and actual-layout evidence unlock routing.
10. Autoroute input validity must pass before route deletion, DSN export, or Freerouting. Freerouting may generate disposable routing candidates after the accepted layout exists; route scoring chooses candidates, but only KiCad/ERC/DRC/final gates can accept them.
11. KiCad ERC/DRC/export/final/JLCPCB gates remain authoritative.
12. Chrome MCP may drive JLCPCB/JLCDFM web review only after local production gates pass; if the browser is already logged in, continue directly, otherwise stop and ask the user to log in.
13. KiKit may create panelized derivatives after a single board passes local gates.
14. kicad-happy may run as a non-blocking second review after local outputs exist.
15. KiBot remains a future CI/export option and must not replace `run_flow.py` unless the user explicitly asks for a CI migration.

## Reference Routing

Keep this file as the short orchestration layer. Load detailed references only when the task needs them:

- Read `references/connectivity-batch.md` before implementing, debugging, or reviewing schematic connectivity, unresolved nets, unconnected pins, module interfaces, `expected_net_graph`, or batch rollback.
- Read `references/beginner-intake.md` before creating or repairing a spec when the user only describes a usage scenario, says they are a beginner, or cannot answer professional electrical parameters.
- Read `references/architecture-stage.md` before creating, changing, reviewing, or advancing a block-level architecture, power tree, module graph, external connector boundary, high-current/high-speed classification, protection intent, failure state, or test/debug plan.
- Read `references/supply-chain-first.md` before creating, repairing, or reviewing a JLCPCB/嘉立创 production, PCBA, or order-ready spec where parts, LCSC/JLC sourcing, package choice, DNP/manual-fit disposition, or stock evidence affects the design.
- Read `references/spec-freeze-stage.md` before freezing, unfreezing, migrating, handing off the first four stages, generating/repairing `product-baseline.md`, or debugging a formal Spec, stale freeze SHA, or generation-before-freeze failure.
- Read `references/schematic-generation-stage.md` before generating, debugging, reviewing, or gating schematic pins, no-connects, power roles/flags, protection paths, exact KiCad net names, strict ERC, or schematic evidence.
- Read `references/package-binding-stage.md` before importing or validating symbol semantics, datasheet pin maps, footprint pad geometry, package dimensions, placement origins, Pin 1/polarity, rotation offsets, bottom-side transforms, or BOM/CPL identity.
- Read `references/layout-stage.md` before defining, generating, changing, reviewing, scoring, or accepting board outlines, mechanical anchors, placement batches, connector direction, regions, keepouts, copper zones, critical proximity/separation, high-current/high-speed/RF/return/thermal layout intent, or routing unlock evidence.
- Read `references/routing-stage.md` before defining, generating, deleting, changing, scoring, importing, locking, or accepting PCB tracks, vias, routing batches, per-net constraints, route-lock artifacts, or any autorouting candidate.
- Read `references/fabrication-capability-stage.md` before generating or approving multilayer physical stackups, controlled-impedance geometry/evidence, blind/buried/microvias, backdrill, or manufacturer capability evidence.
- Read `references/local-validation-stage.md` before running, changing, reviewing, or accepting local ERC/DRC, unconnected, fabrication, package, release, final-gate, JLC production, or production-manifest evidence.
- Read `references/external-dfm-stage.md` before opening manufacturer web DFM, importing browser evidence, or claiming order-ready.
- Read `references/autoroute-input-validity.md` before deleting routes, exporting DSN, running Freerouting, or deciding whether Freerouting can repair a board.
- Read `references/freerouting-candidate-routing.md` before adding, running, scoring, debugging, or accepting Freerouting automatic routing candidates, route cost functions, rip-up/retry attempts, or autorouter command integration.
- Read `references/verification-gates.md` before claiming or designing ERC/DRC/DFM/SI/PI/EMC/thermal verification behavior.
- Read `references/evidence-manifest.md` before making production-ready/order-ready claims or changing evidence, release, hash, or gate result behavior.
- Read `references/tool-interventions.md` before using JLCImport, `easyeda2kicad.py`, KiKit, kicad-happy, KiBot, or external AI/EDA tools.

## Connectivity Batch Gate

For complex boards or any schematic with unresolved connectivity, do not iterate one wire at a time as the primary strategy. If a spec is real, complex, production-track, or above the spec-declared/default connectivity complexity threshold, missing `connectivity_batches` is a blocker. Default stage and complexity rules live in `assets/connectivity-batch-policy.yaml`; per-project overrides belong in spec data such as `validation.connectivity_batch_policy_file` or `validation.connectivity_complexity`, not in code.

Use module-level batches and proposal transactions:

1. Define module interfaces, upstream nets, provided nets, consumed nets, components, and required nets in the spec.
2. Define or derive an `expected_net_graph` from `components[].pads`, module interfaces, and connectivity batches.
3. For new connectivity work, write an additive proposal file first, not a direct edit to the working spec. A proposal may add nets, components, routes, expected net graph entries, connectivity batches, or extend existing expected net graph pins. It must not update, replace, or delete existing design decisions.
4. Run `spec_patch_check.py`, then `batch_transaction_runner.py` to apply the proposal to a temporary spec/project, generate KiCad, and compare the generated netlist against the expected graph.
5. Merge only passing proposal transactions into the working spec with `batch_transaction_runner.py --apply`; discard failed temporary batches.
6. Stop after repeated or structural failures and report the failure class.

Use net labels for large MCU/module connectivity and cross-module signals. Use direct wires only where generated point-to-point wires are clear and cannot create ambiguous crossings. The net graph is the design truth; wires and labels are implementation details.

## Beginner Use-Case Gate

When the user can only describe the use case, Codex must translate that scenario into a conservative MVP spec instead of asking for professional parameters first.

Use `references/beginner-intake.md` for the detailed pattern. The spec must declare `user_profile`, `project.stage: local-mvp` or draft, and `assumption_profile` with `production_claim_allowed: false`, `order_ready_allowed: false`, and `professional_review_required: true`. Professional unknowns become assumptions plus structured TODO items; they block production/order-ready claims, not beginner MVP exploration.

Run `python3 <skill>/scripts/beginner_intake_check.py specs/<project>.yaml` before spec closure. This check is no-op for ordinary specs, but when a beginner/use-case-only profile is declared it fails if the spec tries to claim production/order-ready or omits the required downgrade fields.

## Flow State Gate

Run `flow_state_gate.py` before generation, ERC/DRC, export, final gate, JLCPCB package, route deletion, web evidence, or matrix validation.

This gate makes the lifecycle state machine explicit:

- Draft and local MVP may run local generation and checks, but must not be described as production-ready.
- Production-package, production, release, fabrication, order-ready, and order-ready-blocked states require strict ERC/DRC violation exits.
- Production/order-ready states forbid `--allow-violations` and `--continue-on-check-fail`.
- JLCPCB production states require `scripts/jlcpcb_gate.py --production`; order-ready states also require `--order-ready` after web evidence is imported.

`run_flow.py` and `hooks/pre-final-pcb-flow.sh` must call this gate automatically and use its state to choose strict/production flags. Do not rely on the user or Codex remembering those flags manually.

## Block Architecture Gate

After intake confirmation, read `references/architecture-stage.md`. Write a directed block graph under `architecture` in the spec and generate its declared Markdown/Mermaid report with `architecture_report.py`. Do not put exact parts, symbols, footprints, pins, pads, nets, placement, routes, or trace geometry inside the architecture section.

Run `architecture_gate.py --before-sourcing` before exact component selection and `architecture_gate.py --before-generation` before KiCad. The gate validates intake revision binding, architecture-specific practical-choice confirmation, block graph connectivity, board-boundary interfaces, connector endpoint ownership, power paths, generic sourcing constraints, high-current/high-speed risk coverage, protection intent, failure states, test/debug paths, requirement/hazard coverage, beginner decision ownership, and stage-scoped unknowns. Architecture reports must stay under `project.artifacts_dir`.

Every electrical/data/control block edge crossing to an external block must have an external interface and exactly one board-side connector. Before sourcing, every required on-board/mixed block must declare generic `selection_constraints`; do not put exact parts in those constraints. Use `component-sourcing` and `part-lock` as `required_before`/open-decision stages when the distinction matters.

Professional unknowns may be deferred only to a later target through matching structured open decisions. If sourcing cannot satisfy a ready architecture, create a revised architecture candidate; do not silently change the confirmed use case.

## Supply Chain First Gate

After architecture passes, read `references/supply-chain-first.md`. Do not enter KiCad until this sequence succeeds:

1. Declare bounded `sourcing.context`, component roles, architecture-derived requirements, cross-part compatibility, cost allocation, and artifact paths; run `sourcing_context_check.py --require`.
2. For JLCPCB assembly, use the `jlcpcb-search` MCP first: check `database_status`, refresh the catalog when stale, use `search_components` for bounded discovery, and call `get_component_details` for every shortlisted LCSC part. Save normalized tool inputs/results with timestamps and hashes under `project.artifacts_dir`. Use direct supplier/browser capture only when the MCP is unavailable or lacks a required assertion.
3. Collect bounded candidate batches and independent hashed capture evidence under `project.artifacts_dir`; run `candidate_batch_check.py --require`.
4. Run `candidate_rank_check.py --require`; hard constraints, role coverage, compatibility, and aggregate cost fail before soft ranking.
5. Dry-run and apply `part_lock_transaction.py`; all requirements lock together or none are written.
6. Run `part_selection_check.py --require` before library work. After import/pin-map verification, apply `library_binding_transaction.py`, then require `part_selection_check.py --require --before-generation`.

The composite check reruns architecture, candidates, selected-set compatibility/cost, ranking/lock freshness, component bindings, and architecture trace. A changed part invalidates downstream library evidence. PASS proves bound timestamped evidence and deterministic selection, not reserved stock or web DFM acceptance.

## Spec Freeze Gate

After sourcing and required library binding, read `references/spec-freeze-stage.md`. Complete the formal spec, optionally preview with `product_baseline.py --preview`, then dry-run and apply `spec_freeze_transaction.py`. It atomically stores the generated product handoff, immutable manifest, and small index under local spec-declared paths.

Do not add a duplicate `selected_parts` source. The freeze binds `sourcing.part_lock`, `components`, and `sourcing.downstream_binding`. Every board constraint area must be defined with references or explicitly not applicable with a rationale. Manufacturing must declare target, bare-PCB/PCBA mode, quantity, and outputs; conservative cost estimates are allowed when their basis and margin are recorded.

`run_flow.py` and the pre-final hook must run `spec_freeze_check.py --before-generation`. New local-MVP, production, and order-ready specs cannot enter KiCad without a current freeze; legacy/demo specs without a lifecycle stage remain a no-op. Later work starts from `PCB Build Brief`, never old chat/raw research; missing decisions return through a change request to the owning upstream stage. `spec_output_binding.py` binds generated/release files; any bound input change requires refreeze and regeneration.

## Schematic Generation Gate

After Spec Freeze, read `references/schematic-generation-stage.md`. Every actual symbol pin must be connected or receive a real no-connect marker; required power nets declare source/sink or return roles; power flags bind to physical source pins; production protection intents bind to checked series/shunt path assertions.

Generate only the schematic first. Require exact Spec-to-KiCad net names and component pins, then run strict ERC and write current SHA evidence with `schematic_stage_gate.py`. Do not generate a PCB, DSN, route candidate, DRC, fabrication output, or JLCPCB package before this evidence passes.

Symbols beyond the declared generator capability, including unsupported multi-unit symbols, must fail before generation. Never omit units, pins, protection elements, or net names to obtain PASS.

## Package Binding Gate

After strict schematic evidence and before `--board-only`, read `references/package-binding-stage.md` and run `package_binding_stage_gate.py --before-board`. The gate is mandatory for current downstream part bindings and may not be replaced by ERC/DRC.

Require an independently stored manufacturer-datasheet pin table, semantic datasheet-to-symbol-to-footprint mappings, exact physical pad and manufacturing-layer evidence, body height/3D disposition, explicit orientation/polarity/placement-origin contracts, an independent assembly/supplier orientation source for PCBA, and transactional import provenance. Final and JLCPCB production gates must rerun `--check-evidence`; any changed Spec, policy, library, pin table, geometry, raw import source, or converter output invalidates the evidence.

## PCB Layout Gate

After current schematic and package-binding evidence, read `references/layout-stage.md`. Generate an unrouted board with `generate_project.py --layout-only`, then run `layout_stage_gate.py --after-generation`. Do not create routes, export DSN, or invoke Freerouting until `layout_stage_gate.py --check-evidence` passes.

Modern MVP and production layouts must declare ordered whole-component placement batches and machine-readable dispositions for outline, mechanical anchors, external connectors, keepouts, critical proximity, high-current, high-speed, RF/antenna, power copper, return paths, thermal paths, and assembly. Inspect the actual `.kicad_pcb` for placement/side/orientation, board containment, non-waived Courtyard overlap, regions, named KiCad rule areas/zones, proximity/separation, and connector direction.

Use `layout_batch_transaction.py` for placement experiments. It generates and scores an isolated whole-batch candidate; `--apply` may update the active Spec only after the candidate passes. Applying a candidate invalidates the old Spec Freeze and downstream evidence, which must be regenerated. Never move one footprint at a time on the active generated board as the primary layout process.

## PCB Routing Gate

After current layout evidence, read `references/routing-stage.md` and run `routing_stage_gate.py --before-routing`. Modern MVP and production specs must declare ordered complete-net routing batches and exact per-net layer, width, via, length, reference-path, topology, and differential constraints where applicable.

Treat manual, generated, and Freerouting output as candidates under the same hard gates. Critical power/high-current/high-speed/differential/RF/analog batches are routed and locked before low-risk autorouting. Never delete all active-board routes. Candidate preparation may remove only one declared unlocked batch from an isolated copy while preserving every other net and the accepted layout fingerprint.

Freerouting search is explicit and bounded; `run_flow.py` must not start it. Use `freerouting_candidate_runner.py` with Spec-declared commands, timeout, attempt, consecutive-failure, and no-improvement limits. Then use `routing_candidate_transaction.py` to reject non-batch changes, layout/netlist mutations, non-connectivity DRC violations, final unconnected items, and per-net violations before scoring. `--apply` writes a SHA-bound route artifact and route lock atomically; refreeze and regenerate before acceptance.

After routed generation run `routing_stage_gate.py --after-generation` and `--check-evidence`. Final/JLC gates must require current routing evidence. A score ranks hard-gate PASS candidates only and never substitutes for DRC, SI/PI/EMC/thermal analysis, or functional proof.

## Local Validation Gate

After routing, run `local_validation_gate.py --run` and `--check-evidence`. Modern flows require strict JSON ERC/DRC, zone refill, zero violations, and zero unconnected items bound to current Spec/schematic/PCB hashes. `final_gate.py` and JLC production must consume this evidence; text regex or non-empty reports are insufficient.

Fabrication output is clean-built. JLC package directories reject unmanifested entries; release regeneration preserves only declared external evidence and rejects all other stale files. Production stages must save passing final/JLC gate JSON reports and pass `production_manifest_gate.py --write` plus `--check` to bind the exact fab/package/release inventory.

## Component Import Gate

Use this gate when the spec needs LCSC/JLCPCB/EasyEDA parts and the required KiCad symbol, footprint, or 3D model is not already present in trusted project libraries.

Preferred order:

1. Try JLCImport first when available because it targets JLCPCB/LCSC lookup and can import symbol, footprint, and 3D model data.
2. Use `easyeda2kicad.py` as a fallback or compatibility path when JLCImport is unavailable or unsuitable.
3. Do not hand-draw a footprint before checking whether a reliable LCSC/JLCPCB/EasyEDA import path exists, unless the part is custom or unavailable.

How it intervenes:

- Before generation, map the chosen component in `specs.yaml` with manufacturer/MPN/LCSC or JLC part number, datasheet/source URL, footprint package, assembly status, and polarity where relevant.
- Run the import tool into a project-local library path, not an implicit global-only library.
- Add the generated `.kicad_sym` and `.pretty` paths to `kicad.symbol_libraries` and `kicad.footprint_libraries`.
- Import into a disposable candidate directory, dry-run `library_import_transaction.py`, then use `--apply` to promote the complete library set and provenance atomically only after validation.
- Regenerate `library_audit.json/md` through the existing flow and keep SHA256/size evidence.
- Record raw source identity/hash and locked evidence ID, converter name/version/command, output hashes, independent datasheet pin table, symbol pin semantics, footprint pad/manufacturing-layer geometry, body dimensions/height, 3D disposition, placement origin, polarity/orientation, and source locators in schema-2 binding evidence.
- Record unresolved import uncertainty in `TODO.md`; unresolved symbol/footprint/pinout uncertainty blocks production-ready claims.

The imported library is an input, not a proof. ERC/DRC, package gates, source snapshots, BOM/CPL checks, and manual datasheet review still apply.

## Panelization Gate

Use KiKit only when the user asks for panelization, multiple copies per manufacturing panel, rails, tabs, mouse bites, V-cuts, tooling holes, fiducials, or small-board batch production.

How it intervenes:

- Start from the already-generated single-board `.kicad_pcb`; the single board remains the source design.
- Read panel parameters from the spec, such as array count, spacing, rails, tabs, breakaway style, fiducials, tooling holes, and panel output paths.
- Generate panelized KiCad board and panel fabrication outputs in separate spec-declared artifact paths.
- Run DRC/export/package checks on the panel output as a separate artifact; do not let a passing panel hide a failing single board.
- Keep single-board Gerbers and panel Gerbers clearly separated in release manifests and upload manifests.

Do not run KiKit by default. If panel rules are missing, stop and ask for panel requirements instead of inventing manufacturing panel geometry.

## Advisory Review Gate

Use kicad-happy only as a non-blocking second reviewer after local KiCad files and fabrication outputs exist.

How it intervenes:

- Run it after ERC/DRC/export/final/JLCPCB local gates, or during a review-only task.
- Treat findings as advisory. Triage them into confirmed issues, false positives, or TODO review items.
- Confirmed issues may lead to spec/generator fixes and another local gate run.
- False positives must not fail final_gate, JLCPCB gate, or order-ready gate.
- Include useful advisory output in review notes when it catches risk that KiCad ERC/DRC does not cover, such as decoupling, thermal, EMC, pinout review, or manufacturing review.

kicad-happy cannot make a board production-ready and cannot override KiCad ERC/DRC, final_gate, JLCPCB production gate, or web-side order-ready evidence.

## KiBot Policy

Do not use KiBot as the default export path. The current authoritative export path is `run_flow.py` plus `export_fab.py`, `package_jlcpcb.py`, `release_jlcpcb.py`, and the gate scripts.

Use KiBot only when the user explicitly asks for GitHub Actions, CI/CD, documentation automation, or a comparison/migration away from the current export pipeline. In that case, keep outputs separate until equivalence with the existing gates is proven.

## JLCPCB Web DFM Evidence Gate

Use Chrome MCP for JLCPCB/嘉立创/JLCDFM web-side review only after local `scripts/jlcpcb_gate.py --production specs/<project>.yaml` passes.

Treat this as the External DFM and Order-Ready Stage. Require a current production manifest before upload, exact release-role fingerprints with fail-closed missing-file handling, and optional spec-defined `evidence_max_age_hours`. After all evidence passes, rerun the order-ready and evidence gates and rewrite/check the production manifest to bind the final evidence inventory.

How it intervenes:

- Open the spec-declared JLCPCB/JLCDFM page or the standard upload page only after the release bundle, upload manifest, Gerber/drill zip, BOM, CPL, and local production gate are current.
- Inspect whether the browser session is already logged in. If the account/session is active or the page auto-logs in, continue the upload/review flow without asking the user to log in again.
- If the browser shows a login, password, CAPTCHA, QR, SMS, 2FA, or account-selection step, stop and ask the user to complete it manually. Do not request, store, type, or infer credentials or verification codes. Resume only after the user says the login is complete.
- After login, upload the current release files from the spec-declared release/upload manifest, not arbitrary old files.
- Capture web evidence for every spec-declared `order_review.required_items`, such as DFM result, BOM mapping, CPL placement/rotation preview, polarity handling, DNP handling, and order parameter review.
- Import captured screenshots/PDFs/responses with `scripts/jlcpcb_evidence.py` using `--result passed` only when the web result visibly passes.
- Run `scripts/jlcpcb_gate.py --production --order-ready specs/<project>.yaml` and `python3 <skill>/scripts/evidence_manifest_check.py --order-ready specs/<project>.yaml`.

If any upload file changes after web review, treat the evidence as stale and repeat the web review. Chrome MCP can provide order-ready web evidence. Physical bring-up, load tests, thermal measurements, USB real-device tests, and enclosure fit checks are post-fabrication validation records; they are not prerequisites for AI to finish the production package, and they must not be used to block Gerber/BOM/CPL/JLCDFM preparation.

## Post-Fabrication Validation Boundary

Keep the AI pre-fabrication flow separate from post-fabrication validation.

Pre-fabrication hard blockers include evidence Codex or web tooling can collect before ordering: datasheets, LCSC/JLCPCB sourcing, symbol-footprint-pinmap, power/current budget, connectivity batches, ERC/DRC, Gerber/drill/BOM/CPL, local JLCPCB gates, and JLCDFM web evidence.

Post-fabrication validation items must be recorded under `post_fab_validation_plan` or explicit TODO categories such as `post_fab_validation`, `post_fab_bringup`, `post_fab_lab_test`, and `post_fab_mechanical_fit`. Legacy categories like `bringup` or `lab_test` count as post-fabrication only when the TODO also declares `phase: post_fab`. These items may remain open while generating a production package or order-ready evidence. They only block claims such as function-validated, bring-up-passed, lab-validated, or mass-production-proven.

Run `python3 <skill>/scripts/todo_phase_report.py specs/<project>.yaml` after any evidence/TODO update. If a TODO says `before fabrication`, `KiCad review`, `datasheet`, `pinmap`, `source`, `package`, `power`, `JLCDFM`, or `web_dfm`, it remains a pre-fabrication blocker even if it was accidentally categorized as `bringup`.

Do not ask the user to perform post-fabrication validation before the board is manufactured. Do not report post-fab TODO as the reason to stop AI evidence collection, packaging, ERC/DRC, or JLCDFM preparation.

For an existing repository:

1. Inspect the current project for `specs/`, `scripts/run_flow.py`, `scripts/kicad_check.py`, `scripts/jlcpcb_gate.py`, `configs/pcb_validation_matrix.yaml`, and existing sample specs.
2. Run the Intake Gate unless the user is continuing a specific existing spec with a clear requested stage.
3. Run the Clarification Gate before choosing a spec template. If blocked, return the missing-question list and stop.
4. If the flow exists, start from the closest existing spec and create or update `specs/<project>.yaml`.
5. Use the Connectivity Batch Gate when schematic connectivity, modules, net labels, wires, or unconnected items are in scope.
6. Use the Component Import Gate when selected parts require JLCPCB/LCSC/EasyEDA symbol, footprint, or 3D model import.
7. If the flow does not exist, ask whether to migrate the minimal spec-driven flow from a known source such as `/mnt/e/PCB`; if the user already requested migration, copy the smallest needed scripts/configs/spec templates and adapt paths without hardcoding.
8. For a new JLCPCB PCBA board, prefer starting from the closest existing passing PCBA spec in the repository, then replace project data, nets, components, placements, routes, assembly metadata, release outputs, and evidence requirements.
9. Run the local gates before any web claim. For web/order-ready work, use the JLCPCB Web DFM Evidence Gate: open JLCPCB/JLCDFM only after local `--production` passes, continue directly if the browser is already logged in, otherwise pause for user login, then upload release Gerber zip/BOM/CPL, save screenshots/responses, import evidence with `scripts/jlcpcb_evidence.py`, and run `scripts/jlcpcb_gate.py --production --order-ready`.
10. Use KiKit, kicad-happy, or KiBot only according to their gates above.

## Required Workflow

0. Start with the Requirement Intake Gate, then the Clarification Gate. If the user only gives a usage scenario, apply the Beginner Use-Case Gate and create a conservative MVP spec profile before asking professional questions. Intake- or requirements-blocked work stops before KiCad generation or validation.
1. Read `references/architecture-stage.md`, create the block-level architecture and generic sourcing constraints in `specs/<project>.yaml`, generate its declared report with `architecture_report.py`, obtain explicit user confirmation of practical choices using the report's confirmation SHA256, regenerate the report, and pass `architecture_gate.py --before-sourcing`. Only then run the Supply Chain First Gate; do not lock exact parts, symbols, footprints, pin maps, schematic connectivity, or layout before architecture passes.
2. Do not hardcode board sizes, rules, footprints, positions, routes, output paths, or required KiCad versions in code. Read them from the spec.
3. For production or JLCPCB/LCSC local-library projects, declare `kicad.symbol_libraries` and `kicad.footprint_libraries` in the spec to map each KiCad library nickname to its exact `.kicad_sym` or `.pretty` path; use `symbol_root` and `footprint_root` only as fallbacks.
4. In migrated projects, `scripts/run_flow.py` and `hooks/pre-final-pcb-flow.sh` must acquire a blocking per-spec flow lock before reading or writing generated KiCad files, checks, exports, packages, releases, or evidence. Derive the lock path from `project.artifacts_dir` and `project.name` with a fallback to `artifacts/locks/<spec-stem>.lock`; do not run two flows for the same spec concurrently.
5. Before generation, run the bundled skill executors from this skill's `scripts/` directory against the target spec. In migrated projects, `scripts/run_flow.py` and `hooks/pre-final-pcb-flow.sh` must automatically run these preflight checks from `codex-skills/kicad-production-pcb/scripts` or the `KICAD_PRODUCTION_PCB_SKILL_SCRIPTS` environment override:
   - `python3 <skill>/scripts/flow_state_gate.py specs/<project>.yaml`; production/order-ready state must make `run_flow.py` use strict ERC/DRC and production JLCPCB gates automatically.
   - `python3 <skill>/scripts/requirement_intake_gate.py --before-generation specs/<project>.yaml`; this blocks KiCad generation when the saved intake decision says KiCad/ ERC/DRC is not allowed yet.
   - `python3 <skill>/scripts/beginner_intake_check.py specs/<project>.yaml`
   - `python3 <skill>/scripts/architecture_report.py --if-required specs/<project>.yaml`; this refreshes the derived module diagram when architecture is required and no-ops for legacy/simple specs.
   - `python3 <skill>/scripts/architecture_gate.py --before-generation specs/<project>.yaml`; this is no-op for legacy/simple specs, but confirmed-intake, explicit-architecture, and production-stage specs must have a ready architecture.
   - `python3 <skill>/scripts/spec_closure_check.py specs/<project>.yaml`
   - `python3 <skill>/scripts/part_selection_check.py --before-generation specs/<project>.yaml`
   - `python3 <skill>/scripts/readiness_preflight.py specs/<project>.yaml`
	   - `python3 <skill>/scripts/verification_preflight.py specs/<project>.yaml`; production adds `--strict`.
	   - `python3 <skill>/scripts/fabrication_capability_gate.py --before-generation specs/<project>.yaml`; it is a no-op for untriggered legacy boards and fails closed when multilayer, controlled impedance, or special-via technology lacks current SHA-bound manufacturer evidence.
   - `python3 <skill>/scripts/spec_schema_check.py specs/<project>.yaml`; production adds `--production`.
   - `python3 <skill>/scripts/spec_net_graph_check.py --exact specs/<project>.yaml`
   - `python3 <skill>/scripts/connectivity_batch_check.py --auto-require --generated specs/<project>.yaml`. This fails when a real/complex/production-track spec needs batches but does not declare them; when batches exist, it creates disposable per-batch KiCad projects under the spec-derived artifacts directory, compares generated netlists, and removes passing temporary projects unless `--keep-temp` is explicitly used.
   - Immediately after intake/flow-state, run `python3 <skill>/scripts/spec_freeze_check.py --before-generation specs/<project>.yaml`; production adds `--production`. Stop before expensive architecture/sourcing/connectivity reruns when the local freeze is missing or stale; no-op only for legacy/draft specs.
6. For real/complex boards or production claims, run machine-readable spec closure and intent checks when the spec declares the corresponding data or when using strict production review:
   - `python3 <skill>/scripts/spec_closure_check.py --strict specs/<project>.yaml` when converting a draft into a real production-track project.
   - `python3 <skill>/scripts/readiness_preflight.py --production specs/<project>.yaml` for explicit production review.
   - `python3 <skill>/scripts/requirements_gate.py --strict specs/<project>.yaml` after `requirements` is populated.
   - `python3 <skill>/scripts/power_budget_check.py --strict specs/<project>.yaml` after `power_domains` is populated.
   - `python3 <skill>/scripts/todo_blocker_check.py --production specs/<project>.yaml` before claiming production-ready or order-ready.
   - `python3 <skill>/scripts/todo_phase_report.py specs/<project>.yaml` after TODO evidence classification, to show which remaining items are current AI blockers and which are post-fabrication validation plan items.
7. For declared SI/PI/EMC/thermal checks, run `python3 <skill>/scripts/verification_preflight.py specs/<project>.yaml`; use `--strict` only when a spec must declare all four risk areas. For multilayer, controlled-impedance, or special-via work, also pass `fabrication_capability_gate.py` using the detailed contract in `references/fabrication-capability-stage.md`. Passing the generic verification preflight alone permits only `SI/PI/EMC/thermal risk precheck passed` language; impedance claims require matching calculation evidence and manufacturer capability claims require matching manufacturer-source evidence.
8. If schematic connectivity is in scope, do not edit the working spec directly. Create an additive proposal under a project-local proposals directory, run `python3 <skill>/scripts/spec_patch_check.py specs/<project>.yaml <proposal>.yaml`, then run `python3 <skill>/scripts/batch_transaction_runner.py specs/<project>.yaml <proposal>.yaml`; only after that passes may you run the same command with `--apply`. Use `spec_diff_guard.py` if a spec was changed outside the transaction runner.
9. If schematic connectivity is in scope, run the Connectivity Batch Gate before generation and avoid one-wire-at-a-time debugging.
10. If needed, run the Component Import Gate and update the spec with explicit local library mappings before generation.
11. Complete `assets/spec-freeze-fragment-template.yaml`, dry-run `spec_freeze_transaction.py`, apply it, and pass `spec_freeze_check.py --before-generation`. Do not hand-author frozen status.
12. Generate and gate only the schematic, then run package binding and the unrouted layout stage. Run `routing_stage_gate.py --before-routing` before routed board generation.
13. For explicit Freerouting development, pass autoroute preflight, run bounded isolated candidates, and hard-gate/apply one whole batch with `routing_candidate_transaction.py`; refreeze after application. Normal `run_flow.py` never launches candidate searches.
14. Generate the routed board from Spec routes or a SHA-bound route lock, run `routing_stage_gate.py --after-generation` and `--check-evidence`, preserve current package/layout evidence, then run `spec_output_binding.py --write-generated`.
15. Run checks with `python3 scripts/kicad_check.py specs/<project>.yaml`.
    - For production/order-ready flow state, `run_flow.py` must automatically run `kicad_check.py --strict-violations`.
16. Export fabrication files with `python3 scripts/export_fab.py specs/<project>.yaml`, then run `spec_output_binding.py --write-release`; the pre-final hook and `final_gate.py` require `--check-release` to pass.
17. Run the final gate with `python3 scripts/final_gate.py --require-checks --require-fab specs/<project>.yaml`.
18. For JLCPCB/嘉立创-targeted boards, `run_flow.py` and `hooks/pre-final-pcb-flow.sh` automatically run `python3 scripts/package_jlcpcb.py specs/<project>.yaml`; if `manufacturing.jlcpcb.release` exists they also run `python3 scripts/release_jlcpcb.py specs/<project>.yaml`; then they run `python3 scripts/jlcpcb_gate.py specs/<project>.yaml`, adding `--production` or `--order-ready` when `flow_state_gate.py` requires it.
19. For order-ready evidence, use Chrome MCP to perform the JLCPCB Web DFM Evidence Gate when requested or when web evidence is missing. If already logged in, continue; if not, stop for user login and resume after the user confirms login. Then run `python3 <skill>/scripts/evidence_manifest_check.py --order-ready specs/<project>.yaml` after importing web-side evidence.
20. If panelization is requested, run the Panelization Gate only after the single-board local gates pass.
21. If advisory review is requested or useful, run the Advisory Review Gate only after local KiCad outputs exist.
22. When using external mature KiCad/JLCPCB reference cases, keep them under `external/kicad-cases/` and regenerate `docs/external_case_inventory.json/md` with `scripts/external_case_inventory.py`; source URL, commit, license files when present, key file hashes, and production signals such as Gerber zip, BOM, CPL/positions, Fabrication Toolkit options, IPC netlist, and designators must be traceable. When `configs/external_jlcpcb_case_audit.yaml` exists, run `scripts/external_jlcpcb_case_audit.py` to verify downloaded complete Fabrication Toolkit bundles, including config-defined Gerber zip required groups, and preserve BOM/CPL/designators consistency warnings. External reference cases with known missing bundle artifacts may only be accepted through config-declared `expected_incomplete_cases`; they must not count toward the complete bundle baseline. Treat `jlcpcb_pcba_package_evidence` and `jlcpcb_fabrication_toolkit_bundle_evidence` as local file evidence only, not as JLCDFM/JLCPCB web-side order approval.
23. For multi-board progress checks, use a spec-defined matrix such as `configs/pcb_validation_matrix.yaml` with `python3 scripts/run_pcb_matrix.py configs/pcb_validation_matrix.yaml`; expected failures such as missing real `--order-ready` evidence must be declared explicitly and reported as blocked, not silently ignored. Do not run the matrix as a substitute for answering missing requirements; run single-spec checks first and reserve matrix runs for regression after the single board has a confirmed spec.
24. For PCBA release/order supervision, use `python3 scripts/jlcpcb_order_status.py specs/<project>.yaml` when the spec declares `manufacturing.jlcpcb.release.order_review.status_report`; `run_flow.py` and `hooks/pre-final-pcb-flow.sh` should refresh it automatically. It must report local `--production` status separately from external `--production --order-ready` evidence status.
25. If `run_flow.py`, `release_jlcpcb.py`, or any packaging/export step is rerun after web-side evidence was imported, rerun `scripts/jlcpcb_gate.py --production --order-ready` and the bundled `evidence_manifest_check.py --order-ready`; if evidence fingerprints are stale, re-import evidence with `scripts/jlcpcb_evidence.py` only after confirming the new release files still match the saved JLCDFM/JLCPCB review, or perform a fresh web review.
26. If any command fails unexpectedly, fix the spec or generator and rerun. Do not claim success from partial output.

## Stop Conditions

Stop and report a blocked status instead of continuing when:

- The Intake Gate cannot determine the project goal, use case, target stage, or manufacturing target well enough to choose the next action.
- `requirement_intake_gate.py` fails, or the intake decision still says `can_generate_kicad: false` / `can_run_erc_drc: false` when generation or validation is being attempted.
- The Clarification Gate finds missing requirements for a real/complex board and no beginner conservative MVP profile has been declared.
- `flow_state_gate.py` fails, especially when production/order-ready work is attempted without strict ERC/DRC or with bypass flags.
- `beginner_intake_check.py` fails for beginner/use-case-only work.
- `architecture_gate.py` fails, the architecture is not `ready`, its intake/practical-choice confirmation is stale, a boundary edge lacks its external interface/board-side connector, the block graph/power path is incomplete, sourcing constraints are missing, a required risk/protection/test path is missing, or an unresolved decision blocks the current phase target.
- `spec_closure_check.py` fails for a real/complex board or production-track spec.
- `spec_freeze_transaction.py` fails, or `spec_freeze_check.py` reports a missing/stale local freeze. Do not run KiCad or reuse downstream release files until the current candidate is frozen again.
- `part_selection_check.py` fails for production sourcing, role/compatibility/cost closure, or `--before-generation` library/pin-map binding. Fix the failed stage or downgrade to draft/local MVP.
- `requirements_gate.py --strict`, `power_budget_check.py --strict`, or `todo_blocker_check.py --production` fails for a real/complex production claim.
- `verification_preflight.py` fails for declared SI/PI/EMC/thermal risk prechecks.
- `fabrication_capability_gate.py` fails because the physical stackup is incomplete, its thickness/layer order is inconsistent, controlled-impedance evidence does not match the exact stackup and geometry, a special-via process lacks a deliverable disposition, or manufacturer evidence is missing, stale, hash-mismatched, unsupported, or from an untrusted source.
- A real/complex/production-track spec does not declare `connectivity_batches`, or a connectivity batch fails expected net graph comparison, has missing upstream/provided nets, or cannot be rolled back cleanly.
- Component import is needed but the part number, package, source URL, or imported pin/footprint mapping cannot be verified.
- `layout_stage_gate.py` fails, an unrouted layout contains tracks, placement batches are incomplete, a footprint leaves its permitted board/region, a non-waived Courtyard overlap exists, a keepout/zone is missing, a fixed/connector orientation is wrong, or layout evidence is stale before routing.
- `routing_stage_gate.py` fails because routing batches do not exactly cover electrical nets, per-net constraints are incomplete, actual copper violates constraints, route-lock/evidence is stale, or final routing is not reproducible.
- `autoroute_preflight_check.py` fails before candidate route deletion, DSN export, or Freerouting. Planned unrouted nets may remain; unexpected connectivity, library, geometry, short, crossing, or clearance failures must be fixed first.
- Freerouting is requested but DSN/SES paths, candidate output paths, or the spec-declared `run_command`/export/import commands are missing.
- `route_score_check.py --require` fails for an accepted Freerouting candidate or routed production PCB.
- Panelization is requested but array count, rail/tab method, spacing, tooling, fiducials, or panel output target is unknown.
- `TODO.md` contains unresolved pre-fabrication electrical, component, datasheet, current, thermal, mechanical, or sourcing decisions that affect fabrication safety or production ordering. Post-fabrication bring-up/lab/fit validation TODO items do not block AI production-package generation.
- `scripts/jlcpcb_gate.py --production --order-ready` fails only because web-side JLCPCB/JLCDFM evidence is missing.
- JLCPCB/JLCDFM login, CAPTCHA, SMS, 2FA, account selection, payment, or credential handling requires user action.
- The same gate fails for the same reason twice in one turn or one goal run.
- A validation command would rerun the full matrix before a single target spec has passed its local checks.

Blocked reports must include:

- Current stage: requirements, architecture, local KiCad, production package, or order-ready evidence.
- The exact missing decisions or evidence.
- The next user action needed.
- What was not run because the work is blocked.

Do not keep retrying commands to "make progress" when the remaining issue is missing user intent, missing external web evidence, or a manual engineering decision.

## JLCPCB / 嘉立创 Gate

JLCPCB values must come from `manufacturing.jlcpcb` in the spec. Do not hardcode fabrication thresholds in scripts.

For current minimal production-flow tests, specs use conservative 2-layer 1oz rules:

- `min_track_width_mm >= 0.20`
- `min_clearance_mm >= 0.20`
- `min_via_diameter_mm >= 0.60`
- `min_via_drill_mm >= 0.30`
- required Gerbers include F/B copper, F/B mask, F/B silkscreen, and Edge_Cuts
- drill, BOM, and position/CPL CSV must exist
- `package_jlcpcb.py` must produce a Gerber/drill upload zip and manifest under `manufacturing.jlcpcb.package.output_dir`; the manifest must include SHA256/size entries for package artifacts and each file inside the Gerber/drill zip. If `manufacturing.jlcpcb.package.required_zip_groups` is declared, `scripts/jlcpcb_gate.py` must verify each spec-defined group such as copper, solder mask, silkscreen, board outline, and drill is present in the upload zip by the declared suffix/name rules.
- If `manufacturing.jlcpcb.assembly.enabled: true`, package generation must also produce JLCPCB-formatted BOM/CPL from spec-defined column names and component assembly fields. For current KiCad 10 / JLCPCB Fabrication Toolkit compatible output, declare `manufacturing.jlcpcb.assembly.format_profile_file` and `format_profile`; the profile must require BOM columns `Designator, Footprint, Quantity, Value, LCSC Part #` and CPL columns `Designator, Mid X, Mid Y, Rotation, Layer`. When `manufacturing.jlcpcb.assembly.bom.grouping.enabled: true`, identical parts must be grouped by spec-declared semantic fields, `Designator` must contain the grouped refs, and `Quantity` must equal the grouped ref count.
- If `manufacturing.jlcpcb.assembly.fabrication_toolkit_options.enabled: true`, package generation must write the spec-defined `fabrication-toolkit-options.json`, include it in the JLCPCB package manifest, copy it into the release bundle with the declared `release_role`, include that role in `UPLOAD_MANIFEST`, and include the role in `order_review.fingerprint_roles` so web-side evidence is invalidated when the Fabrication Toolkit export options change.
- In production mode, assembly metadata requirements such as part field, required component fields, part-number regex, source URL regex, and source URL part matching must be declared in `manufacturing.jlcpcb.assembly` and enforced by `scripts/jlcpcb_gate.py --production`
- In production mode, if PCBA assembly declares `source_snapshot`, run `scripts/lcsc_source_snapshot.py specs/<project>.yaml` before release generation; the snapshot must verify LCSC page fields such as part number, manufacturer, MPN, packaging, and datasheet URL against spec assembly metadata. If `source_snapshot.require_datasheet_url: true`, the snapshot must include the observed LCSC datasheet URL and the production gate must compare it against each assembled component's spec `datasheet`. If `source_snapshot.require_datasheet_files: true`, the snapshot must download the LCSC datasheet PDFs using spec-defined output paths/templates/signatures, and the production gate must verify file existence, size, SHA256, signature, and release bundle roles. If `source_snapshot.require_in_stock: true`, the snapshot must include observed LCSC stock and the production gate must compare it against the spec-declared order quantity field. If `source_snapshot.max_age_hours` is declared, the production gate must reject stale snapshots.
- In production mode, assembled components should declare `assembly.footprint_package_token`; it must match `assembly.lcsc_packaging` and appear in the KiCad footprint name so package/footprint mismatches are caught before JLCPCB upload.
- For PCBA releases that use downloaded LCSC/EasyEDA local libraries, the spec must still provide explicit `kicad.symbol_libraries` and `kicad.footprint_libraries`, assembled refs must declare JLCPCB/LCSC sourcing metadata, and through-hole or manually fitted local-library connectors must be marked `assembly.dnp: true` so they are excluded from BOM/CPL/designators.
- In production mode, if PCBA assembly declares `format_profile_file`, it must also declare `format_profile_release_role`; the profile file must be copied into the release bundle, listed in `SHA256SUMS` and `release_manifest.json`, and included in `order_review.fingerprint_roles`.
- In production mode, polarized assembly refs must be declared in `manufacturing.jlcpcb.assembly.polarized_refs`; every listed component must set `assembly.polarized: true` and satisfy spec-defined `required_polarity_fields`, including pad/net mapping and expected CPL rotation when configured.
- In production mode, JLCPCB CPL coordinate frame must be declared in `manufacturing.jlcpcb.assembly.cpl.coordinate_frame`; when using `board_origin`, package generation must subtract the spec board origin and the gate must validate CPL coordinates against `0..board.size_mm`.
- In production mode, if `order_parameters.options` declares an assembly side, `manufacturing.jlcpcb.assembly.cpl.order_parameter_side_field` and `order_parameter_side_values` must map that order option to allowed CPL `Layer` values; the gate must reject top/bottom order-side mismatches.
- In production mode, `scripts/jlcpcb_gate.py` must also validate BOM/CPL rows and assembly audit content: assembled refs must match non-DNP spec refs exactly, DNP refs must be absent, BOM values/footprints/parts must match the spec, CPL coordinates/rotation must be numeric with coordinates inside the board outline, and `ASSEMBLY_AUDIT.json` must match the spec/BOM/CPL package data.
- If `manufacturing.jlcpcb.review_artifacts` exists, `export_fab.py` must export the declared schematic/assembly PDF files, and `scripts/jlcpcb_gate.py` must verify they exist and are non-empty.
- If `manufacturing.jlcpcb.required_fab_outputs.ipc_netlist: true`, the spec must declare `manufacturing.jlcpcb.ipc_netlist.enabled`, `output_dir`, and `filename`; `export_fab.py` must export an IPC-D-356 netlist and `scripts/jlcpcb_gate.py` must verify it exists and is non-empty.
- If `manufacturing.jlcpcb.order_parameters` exists, `release_jlcpcb.py` must generate `ORDER_PARAMETERS.json/md` from spec and board data, and `scripts/jlcpcb_gate.py --production` must verify declared required order options are present.
- If `manufacturing.jlcpcb.assembly.designators.enabled: true`, `package_jlcpcb.py` must generate the spec-declared Fabrication Toolkit-compatible `ref:quantity` designators file, exclude DNP refs when configured, include it in the package manifest, and `scripts/jlcpcb_gate.py --production` must verify refs, quantities, format, and package-manifest hash/size.
- If `manufacturing.jlcpcb.release` exists, `release_jlcpcb.py` must create a release bundle with copied upload files, ERC/DRC reports, spec/TODO, declared review PDFs, `ORDER_PARAMETERS.json/md` when configured, `ASSEMBLY_AUDIT.json/md` when assembly is enabled, `SHA256SUMS`, release manifest, and an order review checklist. For assembly releases, `ASSEMBLY_AUDIT.json/md` must include per-component sourcing metadata, BOM row, CPL row, placement, rotation, and DNP refs.
- If `manufacturing.jlcpcb.assembly.source_snapshot` exists, the generated LCSC source snapshot JSON/MD must be included in the release bundle, upload manifest when configured, and evidence fingerprint roles.
- If `manufacturing.jlcpcb.release.upload_manifest` exists, `release_jlcpcb.py` must generate `UPLOAD_MANIFEST.json/md` from spec-declared upload roles and release file hashes, and `scripts/jlcpcb_gate.py --production` must verify it matches `release_manifest.json`.
- `scripts/jlcpcb_gate.py` must verify release integrity: every `release_manifest.json` file entry exists, its SHA256 matches the actual file, `SHA256SUMS` contains the same digest, and `SHA256SUMS` has no extra unmanifested files.
- `scripts/jlcpcb_gate.py` must verify upload manifest integrity: upload manifest files are included in the release manifest/checksums, required spec-declared roles are present, and every upload manifest file SHA256 matches the release manifest.
- External JLCPCB/JLCDFM evidence must be imported with `scripts/jlcpcb_evidence.py`; do not fake evidence files or mark review items complete without the actual web-side result.
- `release_jlcpcb.py` must preserve an existing valid `evidence_manifest.json` and `evidence/` directory when regenerating a release bundle; if the existing evidence manifest is invalid, fail instead of overwriting audit evidence.
- `scripts/jlcpcb_gate.py` must verify imported evidence integrity: evidence IDs are known, evidence files exist, and evidence SHA256 values match `evidence_manifest.json`.
- Imported evidence must be tied to the current release fingerprint declared by `order_review.fingerprint_roles`; if any fingerprinted release file changes, old evidence must fail the gate and be re-imported after a new JLCDFM/JLCPCB review.
- Treat stale evidence after release regeneration as a successful safety hook, not as something to bypass. Rebind evidence only when the regenerated Gerber/drill zip, BOM, CPL, designators, options, review PDFs, source snapshots, and other fingerprinted files are materially the same as the web-reviewed release; otherwise upload the new release and collect fresh JLCDFM/JLCPCB screenshots/responses.
- Evidence imported for order-ready use must include an explicit `--result passed`; `failed` and `blocked` evidence are audit records and must not satisfy `--order-ready`.
- Evidence imported for order-ready use must satisfy spec-declared `required_evidence_fields`, the release-level `evidence_source_url_pattern`, any required-item-specific `evidence_source_url_pattern` override, `required_evidence_file_extensions`, and each required item's `evidence_type`.
- If bundled LCSC datasheet PDFs are included in the release, order-ready evidence should include a spec-declared datasheet review item proving value/package/polarity/electrical assumptions were manually checked against those PDFs; production gate must verify the configured datasheet review item exists in `order_review.required_items`, and every datasheet PDF release role is in `UPLOAD_MANIFEST` and `order_review.fingerprint_roles`.
- `scripts/jlcpcb_gate.py --production` is only local PCBA package readiness. `scripts/jlcpcb_gate.py --production --order-ready` additionally requires imported external JLCPCB/JLCDFM evidence files for every order-review item.
- `scripts/jlcpcb_order_status.py` must not replace `jlcpcb_gate.py`; it calls the gates and writes the spec-declared status report so local production readiness and external order readiness stay visibly separate.

These are workflow guardrails, not a substitute for checking the live JLCPCB capability page and the JLCDFM upload result before ordering.

## Production Claim Rule

Read `references/verification-gates.md` before making verification or readiness claims. The executable gates and release manifests are authoritative; do not maintain a parallel prose checklist that can drift from them.

A production-package claim requires confirmed intake and budget, ready architecture, fresh compatible/costed part lock, verified library/pin-map binding, closed pre-fabrication decisions, generated-net agreement, strict ERC/DRC with zero unconnected items, fabrication exports, hashes/manifests, and target-manufacturer local gates. Freerouting, advisory review, and SI/PI/EMC/thermal prechecks cannot replace them.

An order-ready claim additionally requires current external JLCPCB/JLCDFM evidence bound to the exact release hashes, with every required item recorded as passed. Post-fabrication bring-up/lab/fit plans may remain open for a production package, but stronger functional, validated, or mass-production claims require their corresponding evidence.

If a required gate is missing or failed, call the result a draft, local MVP, blocked production package, or blocked order-ready release according to `flow_state_gate.py`.

## Command Index

Stage-specific commands live in the referenced stage documents. The core entry points are:

```bash
python3 <skill>/scripts/requirement_intake_gate.py --before-generation specs/<project>.yaml
python3 <skill>/scripts/architecture_gate.py --before-sourcing specs/<project>.yaml
python3 <skill>/scripts/part_lock_transaction.py --apply specs/<project>.yaml
python3 <skill>/scripts/part_selection_check.py --require --before-generation specs/<project>.yaml
python3 <skill>/scripts/spec_freeze_transaction.py --apply specs/<project>.yaml
python3 <skill>/scripts/spec_freeze_check.py --before-generation specs/<project>.yaml
python3 <skill>/scripts/spec_output_binding.py --check-release specs/<project>.yaml
python3 <skill>/scripts/fabrication_capability_gate.py --before-generation specs/<project>.yaml
python3 scripts/run_flow.py specs/<project>.yaml
python3 scripts/kicad_check.py --strict-violations specs/<project>.yaml
python3 scripts/jlcpcb_gate.py --production specs/<project>.yaml
python3 scripts/jlcpcb_gate.py --production --order-ready specs/<project>.yaml
python3 <skill>/scripts/run_golden_benchmark.py
```

## Final Response Requirements

Report only evidence-backed status:

- KiCad version used.
- Files generated.
- Commands run.
- ERC/DRC/export/final gate result.
- JLCPCB package and gate result when `manufacturing.target: jlcpcb`.
- Remaining TODO and whether fabrication is blocked.
