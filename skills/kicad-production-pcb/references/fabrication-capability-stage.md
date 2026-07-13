# Fabrication Capability Stage

Use this stage before generating or approving a board that triggers the trusted fabrication-capability policy. The executable policy, not prose or manufacturer values embedded in Python, defines copper-layer bounds, trigger paths, stackup fields, evidence schemas, source registries, and freshness rules.

## Physical Stackup Contract

Declare `board.stackup.schema_version`, authoritative `board_thickness_mm`, `total_thickness_tolerance_mm`, `total_thickness_scope`, and a `layers` list. Copper entries name every enabled KiCad copper layer in top-to-bottom order and include physical thickness. Dielectric entries declare core/prepreg type, thickness, material, dielectric constant, and loss tangent. Top and bottom solder-mask entries declare thickness, material, dielectric constant, and loss tangent; mask thickness is excluded from the copper-plus-dielectric board-thickness sum but remains bound into the stackup digest. The calculated copper-plus-dielectric thickness must match the authoritative board thickness within the declared tolerance.

The generator writes this data into the KiCad board stackup and sets board thickness through `pcbnew`. The gate independently checks layer count, exact copper order, entry alternation, required material properties, and total thickness. A global copper weight is not a substitute for per-layer copper thickness.

## Controlled Impedance Contract

Each controlled differential pair or single-ended net declares a stable target ID, net identity, target impedance, tolerance, routing layer, trace geometry, and reference layers. Differential geometry includes pair gap; single-ended geometry does not invent a pair gap. The gate binds those fields to `routing.net_constraints` and `routing.differential_pairs`; the routing-stage gate then checks the actual segment layers and widths against the same geometry.

`verification.signal_integrity.impedance_evidence` points to a SHA-bound evidence file. That file must cover the current target set exactly, bind the current canonical stackup digest and exact geometry, identify the calculation tool/version/method, contain a calculated result, and pass the Spec tolerance. Evidence age limits may be made stricter by the Spec but cannot exceed the trusted-policy maximum. This proves only the recorded calculation. It does not replace field-solver review, manufacturer engineering confirmation, coupons, TDR, or laboratory measurements when those are required by the product claim.

## Manufacturer Capability Contract

`manufacturing.fabrication_capability` declares the manufacturer identity, requested features, process dispositions, and an evidence descriptor. The descriptor and evidence file must agree on capture time and source URL, pass the trusted manufacturer-source registry, be within the declared age limit, and match the file SHA256.

Capability evidence must explicitly support the current copper-layer count, canonical physical-stackup digest, and every requested process feature. Manufacturer capability numbers live in the captured evidence, never in Python. An unknown manufacturer or an untrusted source domain remains blocked until the trusted policy is intentionally extended.

Blind, buried, and microvia mappings come from `routing.net_constraints.*.allowed_via_types` and `via_type_by_layer_pair`. Backdrill intent comes from the corresponding backdrill constraint. Every triggered special process must have a process disposition naming its dedicated fabrication deliverable, or explaining which declared KiCad/Gerber/drill outputs the manufacturer will interpret. Each disposition declares output artifact globs and required content markers. After export, `fabrication_capability_gate.py --check-outputs` requires real non-empty files, checks their configured FileFunction/content markers, and records hashes. SES import must reject a non-full-span via without a unique type mapping and must reject required backdrill because SES cannot carry its complete machining definition.

## Commands

```bash
python3 <skill>/scripts/fabrication_capability_gate.py --before-generation specs/<project>.yaml
python3 <skill>/scripts/fabrication_capability_gate.py --check-outputs specs/<project>.yaml
python3 scripts/tests/test_generate_project_stackup.py
python3 scripts/tests/test_generate_project_via_technology.py
python3 scripts/tests/test_special_via_drill_export.py
python3 scripts/tests/test_multilayer_generation_dsn.py
python3 <skill>/scripts/tests/test_fabrication_capability_gate.py
python3 <skill>/scripts/tests/test_import_routing_batch_ses.py
```

The 6–16-layer regression verifies real KiCad board generation, every enabled copper layer in DSN, and rejection of invalid layer counts, Spec/board mismatches, and known-but-disabled layers. It is a workflow regression, not manufacturer approval for those layer counts.
