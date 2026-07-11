# Freerouting Candidate Routing

Freerouting is an optional candidate generator. It is not the source of truth, a final gate, or permission to delete active-board routing.

## Workflow

1. Pass the formal routing contract and accepted-layout evidence.
2. Select one declared `method: freerouting`, `state: planned` routing batch.
3. Let the runner copy the accepted board and remove only that batch's unlocked nets from the copy.
4. Export DSN and run bounded attempts on disposable files.
5. Import every SES result into a candidate PCB. A successful process without SES and loadable PCB output is failure.
6. `routing_candidate_transaction.py` rejects layout/non-batch changes, per-net violations, DRC violations, and final unconnected items before scoring.
7. Apply one complete passing batch, refreeze, and regenerate from the SHA-bound route lock.

`run_flow.py` validates an accepted route; it never launches candidate searches.

## Spec Shape

```yaml
routing:
  schema_version: 1
  state: ready
  revision: 1
  strategy: hybrid
  batches:
    - id: LOW_RISK_SIGNALS
      order: 2
      nets: [SIGNAL_A, SIGNAL_B]
      method: freerouting
      state: planned
      rationale: Critical routing is already locked; these signals permit bounded candidates.
  net_constraints:
    SIGNAL_A: {net_class: Default, connection_mode: tracks, topology: point-to-point, allowed_layers: [F.Cu, B.Cu], min_width_mm: 0.2, max_vias: 2, rationale: Low-risk signal.}
    SIGNAL_B: {net_class: Default, connection_mode: tracks, topology: point-to-point, allowed_layers: [F.Cu, B.Cu], min_width_mm: 0.2, max_vias: 2, rationale: Low-risk signal.}
  freerouting:
    enabled: true
    batch_id: LOW_RISK_SIGNALS
    attempts: 8
    seed: 100
    timeout_seconds: 900
    max_consecutive_failures: 3
    max_no_improvement: 3
    tool_version: replace-with-observed-version
    dsn_file: artifacts/freerouting/project/project.dsn
    candidate_output_dir: artifacts/freerouting_candidates/project
    export_dsn_command: [replace-with-project-command, "{pcb_in}", "{dsn}"]
    run_command: [java, -jar, tools/freerouting/freerouting.jar, -de, "{dsn}", -do, "{ses}"]
    import_ses_command: [replace-with-project-command, "{pcb_in}", "{ses}", "{pcb_out}"]
```

Commands and tool locations belong in Spec data. Candidate paths must stay under `project.artifacts_dir`. Attempts, timeouts, consecutive failures, and no-improvement limits may not exceed policy to keep an unsuccessful run alive indefinitely.

The compatibility `route_score_check.py` may inspect an already selected routed PCB. Candidate selection uses hard gates first; score is only a tie-breaker among passing candidates. Neither score nor DRC proves SI, PI, EMC, thermal, or functional correctness.
