# Package Binding Stage

Use this stage to prevent a correct-looking schematic from producing a physically wrong board. It sits after strict schematic ERC and before PCB generation, but its semantic symbol-pin inputs must already be locked before schematic generation.

## Stage Boundaries

1. Before Spec Freeze and schematic generation, lock part identity, datasheet pin functions, symbol semantics, and project-local library files.
2. After strict ERC, validate the current semantic pin map, physical footprint geometry, import provenance, and orientation contract.
3. Only a current package-binding evidence manifest may unlock PCB generation.
4. After placement, validate actual CPL coordinates, side, rotation, polarity, and centroid against the orientation contract. This later assembly check does not replace this stage.

## Required Evidence

Use library binding manifest schema 2. Each binding must provide:

- an independent datasheet pin-table evidence file bound to a local raw manufacturer-datasheet snapshot and the matching part-lock evidence ID/hash; its pin/name/function/locator records must exactly match the semantic pin map;
- a pin-map evidence file with datasheet pin number/name/function, symbol pin number/name/electrical type, footprint pad, connected/no-connect disposition, actual Spec net where connected, and a datasheet page/table locator;
- a footprint geometry evidence file containing exact normalized physical pads and parsed Courtyard/Fab/Silk/Paste/custom-pad/3D-model features, positive body length/width/height, source evidence IDs/hashes, and a fabrication contract;
- a fabrication contract covering Courtyard, Fab outline, silkscreen Pin 1 marker, paste strategy, exposed-pad strategy, 3D-model disposition, KiCad CPL anchor, and body-center offset;
- an orientation contract covering datasheet view, Pin 1 reference, footprint zero axis, placement origin, CPL rotation offset, bottom-side transform, polarity kind, and locked source locator; PCBA parts additionally require an independent assembly/supplier evidence record;
- `library_origin`; imported libraries additionally require a raw source snapshot bound to part-lock evidence, converter name/version/command, output hashes, candidate directory, and successful transactional promotion.

Evidence files and raw import snapshots stay under `project.artifacts_dir`. Promoted KiCad libraries stay in project-local library paths. Import into a disposable candidate directory and promote the complete symbol/footprint/model set only after validation; failed candidates must not modify the active project library.

Use `assets/library-import-transaction-template.yaml`, then dry-run and apply the import transaction:

```bash
python3 <skill>/scripts/library_import_transaction.py specs/<project>.yaml <transaction>.yaml
python3 <skill>/scripts/library_import_transaction.py --apply specs/<project>.yaml <transaction>.yaml
```

Do not set `import_provenance.transaction.promoted` by hand. The transaction executor writes provenance in the same atomic operation as the promoted library files.

## Gate Commands

Validate the contract without generating a board:

```bash
python3 <skill>/scripts/package_binding_stage_gate.py --check-contract specs/<project>.yaml
```

After strict schematic evidence exists, write the stage evidence and unlock board generation:

```bash
python3 <skill>/scripts/package_binding_stage_gate.py --before-board specs/<project>.yaml
```

Final, fabrication, and JLCPCB gates must recheck it:

```bash
python3 <skill>/scripts/package_binding_stage_gate.py --check-evidence specs/<project>.yaml
```

The gate is automatically required when `sourcing.downstream_binding.status: ready`, when `verification.package_binding_stage.required: true`, or with `--require`. Legacy/draft specs without those signals remain a no-op.

## Interpretation

PASS proves that current local files agree with an independently stored, source-hash-bound datasheet pin table, physical manufacturing-layer contract, and traceable import transaction. It does not prove the manufacturer datasheet itself is correct, nor does it prove the assembler's live orientation preview. JLCPCB web DFM and post-placement BOM/CPL review remain later independent evidence.
