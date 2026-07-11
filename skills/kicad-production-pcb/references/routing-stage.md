# Routing Stage

Use this stage only after current schematic, package-binding, and layout evidence pass. Routing is a transactional design stage; it is not an automatic side effect of validation.

## Contract

Declare `routing.schema_version`, `state`, `revision`, `strategy`, ordered `batches`, and one `net_constraints` entry for every routed electrical net. Each net belongs to exactly one batch. Use `connection_mode: tracks`, `zone`, or `mixed`; plane-only nets declare concrete `zone_ids` and remain covered by strict DRC after zone refill.

Critical power, high-current, differential, clock, RF, and sensitive analog batches must be routed and locked before unrestricted low-risk autorouting. Constraints belong in Spec data: allowed layers, minimum width, via count/diameter/drill, optional maximum length, required reference net, topology, differential-pair gap/skew/coupled ratio, and current-path requirements. The actual-board gate rejects branches on point-to-point/daisy-chain nets and requires a declared star branch to occur at its pad anchor.

## Candidate Workflow

1. Pass `routing_stage_gate.py --before-routing` and current layout evidence.
2. Select one complete routing batch. Never select arbitrary individual tracks.
3. Create an isolated candidate from the accepted layout/current route lock. Remove only the selected batch's unlocked nets in that copy.
4. Export and inspect DSN. Reject zero-length geometry, invalid padstacks, missing nets, or exporter warnings classified as blocking.
5. Run bounded Freerouting attempts with a timeout, consecutive-failure limit, and no-improvement limit.
6. Import SES into candidate PCBs. A process exit code without a loadable candidate PCB is failure.
7. Run `routing_candidate_transaction.py` for every candidate. It rejects layout/netlist/rule mutations, checks the whole batch, allows unconnected items only on later `planned` batches, runs strict KiCad DRC with zone refill, and scores only hard-gate PASS candidates.
8. Apply only the selected complete candidate with `--apply`. This writes an immutable route artifact and SHA-bound route lock transaction; it does not copy an unchecked PCB over the main project.
9. Refreeze and regenerate. `routing_stage_gate.py --after-generation` must reproduce the accepted route lock before final gates.

`run_flow.py` must never launch Freerouting searches automatically. It validates the accepted routing result. Candidate generation is explicit so normal verification cannot run indefinitely.

## Acceptance Boundary

DRC and zero unconnected items are hard gates, not score terms. Length and via count rank only candidates that already pass connectivity, invariant, per-net constraint, and strict DRC checks. A lower score is not proof of SI, PI, EMC, thermal, or functional correctness.
