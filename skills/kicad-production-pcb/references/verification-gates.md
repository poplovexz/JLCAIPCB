# Verification Gates Reference

Use this reference before claiming or designing ERC, DRC, DFM, SI, PI, EMC, or thermal verification behavior.

## Gate Types

Classify every verification check as one of:

- `hard_gate`: deterministic pass/fail from local project data and tool output.
- `risk_precheck`: deterministic or checklist-based risk reduction from declared thresholds; cannot prove the board is safe or compliant.
- `advisory_gate`: review notes only; cannot block or prove the board is safe or compliant.
- `external_evidence_gate`: requires manufacturer web result, simulation output, lab measurement, or manual review evidence.

No model, no threshold, no tool output, no evidence file means no verification claim.

## Capability Matrix

| Area | Type | Acceptable automated evidence | Claim limit |
|---|---|---|---|
| Net graph | hard_gate | expected vs actual netlist report | connectivity only, not design correctness |
| ERC | hard_gate | KiCad ERC report | schematic electrical-rule cleanliness |
| DRC | hard_gate | KiCad DRC report | local geometry-rule cleanliness |
| Gerber/drill/BOM/CPL | hard_gate | file manifest, hashes, format audits | package completeness |
| JLCPCB local rules | hard_gate | local gate JSON/MD | local conservative rule pass |
| JLCPCB web DFM | external_evidence_gate | imported JLCPCB/JLCDFM evidence bound to release hash | order-side DFM result |
| SI | risk_precheck or external_evidence_gate | `verification_preflight.py`, length/skew/impedance-rule report, simulation evidence | precheck only without SI model/simulation |
| PI | risk_precheck or external_evidence_gate | `verification_preflight.py`, power tree, current budget, width/via/drop checks, simulation evidence | precheck only without PI model/simulation |
| EMC | risk_precheck, advisory_gate, or external_evidence_gate | `verification_preflight.py`, checklist, layout risk report, lab evidence | no certification claim without lab evidence |
| Thermal | risk_precheck or external_evidence_gate | `verification_preflight.py`, power/temperature estimate with thresholds, thermal model/test evidence | no real temperature claim without model/test |

## Required Spec Inputs

Verification must be driven from spec data:

```yaml
verification:
  signal_integrity:
    differential_pairs:
      - name: USB2
        nets: [USB_D_P, USB_D_N]
        impedance_ohm: 90
        max_skew_mm: 1.0
        stackup_required: true
  power_integrity:
    rails:
      - name: +3V3
        nominal_v: 3.3
        tolerance_percent: 5
        max_load_ma: 500
  thermal:
    components:
      - ref: U2
        max_power_w: 0.8
        max_case_temp_c: 85
  dfm:
    manufacturer: jlcpcb
    require_web_evidence: true
```

Missing verification inputs should block claims, not trigger guesswork.

## Risk Precheck Execution

Use:

```bash
python3 <skill>/scripts/verification_preflight.py specs/<project>.yaml
python3 <skill>/scripts/verification_preflight.py --strict specs/<project>.yaml
```

`verification_preflight.py` only runs declared checks by default. Use `--strict` when a project must declare SI, PI, EMC, and thermal risk prechecks.

Passing output means only:

- `SI risk precheck passed`
- `PI risk precheck passed`
- `EMC risk precheck passed`
- `thermal estimate precheck passed`

It does not mean compliance, real-world signal integrity, power-integrity signoff, EMC certification, or measured temperature proof.

## Fast Audit Order

1. Intake/spec completeness.
2. Spec schema and net graph.
3. Library and component sourcing audit.
4. KiCad generation and netlist comparison.
5. ERC/DRC.
6. Fabrication export and package gates.
7. External evidence gates.
8. Advisory SI/PI/EMC/thermal checks.

Run the full matrix only after the target spec passes local checks.

## Claim Language

Use precise language:

- Say `ERC passed` only when strict ERC report proves it.
- Say `DRC passed` only when strict DRC report proves it.
- Say `local production package passed` when local packaging and gates pass.
- Say `order-ready` only when external evidence gates pass.
- Say `SI precheck passed`, `PI precheck passed`, `EMC precheck passed`, or `thermal estimate passed` unless proper models/evidence prove stronger claims.
