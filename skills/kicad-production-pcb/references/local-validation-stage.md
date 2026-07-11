# Local Validation Stage

Use this stage after routed generation and before fabrication export. It is the authoritative local ERC/DRC evidence boundary.

## Required Sequence

1. Verify current schematic, package-binding, layout, and routing evidence.
2. Run KiCad strict ERC as JSON with `--severity-all --exit-code-violations`.
3. Run KiCad strict DRC as JSON with zone refill and `--exit-code-violations`.
4. Require zero ERC violations, zero DRC violations, and zero unconnected items. Schematic-to-PCB net agreement remains owned by the exact generated-net and stage gates because generated boards may not carry KiCad schematic-link UUID metadata.
5. Write deterministic human summaries plus a manifest binding the current Spec, schematic, PCB, KiCad version, policy, executor, commands, counts, and output hashes.
6. `final_gate.py` and JLCPCB production validation must consume `--check-evidence`; report existence or text regex is not sufficient.

Run:

```bash
python3 <skill>/scripts/local_validation_gate.py --run specs/<project>.yaml
python3 <skill>/scripts/local_validation_gate.py --check-evidence specs/<project>.yaml
```

The runner writes a failed manifest when a strict command or count fails, preventing an older passing manifest from remaining authoritative. Legacy/draft specs remain no-op unless `--require` is used.
