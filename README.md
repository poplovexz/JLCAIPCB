# JLCAIPCB

[简体中文](README.zh-CN.md)

JLCAIPCB is a Codex-oriented, spec-driven KiCad production workflow. It turns a beginner's use-case description into a controlled engineering process covering requirement intake, architecture, component sourcing, Spec Freeze, schematic generation, package/pin mapping, PCB layout, routing candidates, strict local validation, and JLCPCB/JLCDFM order evidence.

This repository provides:

- a complete Codex skill in `skills/kicad-production-pcb/`;
- deterministic Python gates, policies, references, and golden benchmarks;
- a project-level pre-final workflow hook in `hooks/`;
- an `AGENTS.md` template that makes the hook part of Codex's project instructions;
- English and Simplified Chinese installation documentation.

## What It Is

JLCAIPCB is a workflow controller and evidence system. It keeps `specs.yaml` as the source of truth and rejects stale or incomplete downstream artifacts. It does not claim that ERC/DRC proves functional correctness, and it does not replace KiCad, Freerouting, laboratory testing, or manufacturer review.

## From a User Request to a PCB

The project starts from the user's plain-language description of what the board should do. Codex turns that description into a PCB through a gated process:

1. Confirm function, power, interfaces, important parts, dimensions, manufacturing target, and safety constraints with the user.
2. Convert the confirmed requirements into a machine-readable `specs.yaml`, block architecture, power/current budget, and sourcing constraints.
3. Select and lock real components, symbols, footprints, pin mappings, and supplier evidence before generating hardware files.
4. Freeze the approved Spec, then generate the real KiCad schematic and PCB instead of treating an AI sketch as the design source.
5. Apply stackup, impedance, layout, routing, via, backdrill, ERC, and DRC gates; incomplete or stale evidence stops the flow.
6. Export and verify Gerber, drill, BOM, placement, release manifests, and manufacturer review evidence for the requested fabrication stage.

The current workflow supports policy-driven even copper-layer counts from 2 through 32, with automated 6–16 layer generation/DSN regression coverage. Detailed physical stackup, controlled-impedance evidence, blind/buried/microvias, backdrill outputs, and manufacturer capabilities are validated before a production or order-ready claim.

## Quick Install

```bash
git clone https://github.com/poplovexz/JLCAIPCB.git
cd JLCAIPCB
./install.sh
```

Restart Codex after installation. Then start with:

```text
Use $kicad-production-pcb to start a PCB design from my use case.
```

For a project-local installation with the workflow hook:

```bash
./install.sh --project /path/to/your/kicad-project
```

Review [the English installation guide](docs/INSTALL.md) before enabling the hook in an existing project.

## Core Stages

1. Beginner requirement intake and confirmation.
2. Block-level architecture.
3. Supply-chain-first candidate selection and part locking.
4. Local transactional Spec Freeze and PCB Build Brief.
5. Real KiCad schematic generation and strict ERC.
6. Symbol, footprint, pad, polarity, and assembly mapping.
7. Constraint-driven PCB layout.
8. Transactional routing candidates and strict DRC.
9. Clean local production validation and exact release manifest.
10. External JLCPCB/JLCDFM evidence bound to release hashes.

Physical bring-up and laboratory validation happen after fabrication and are intentionally outside these ten pre-fabrication stages.

## Requirements

- OpenAI Codex with filesystem skills support;
- Python 3.10 or newer;
- PyYAML;
- KiCad CLI matching the version required by the project spec;
- Bash and `flock` for the bundled hook;
- project-side KiCad generation/export adapters described in the installation guide.

## Security

Never put GitHub tokens, supplier credentials, passwords, CAPTCHA responses, SMS codes, or payment data in specs, prompts, evidence files, Git remotes, or shell history. Browser authentication and payment remain explicit user actions.

## License

MIT. See [LICENSE](LICENSE).
