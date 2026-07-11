# JLCAIPCB Codex Instructions

- Use the `kicad-production-pcb` skill for PCB requirement intake, generation, review, validation, manufacturing export, and order-ready work.
- Keep the project spec as the source of truth. Do not hardcode project names, electrical values, paths, manufacturer thresholds, tool commands, or evidence IDs in shared scripts.
- Start new boards at requirement intake. Do not generate KiCad files until requirement confirmation, architecture, sourcing, and Spec Freeze gates pass.
- Continue existing work from the first failed gate. Do not repeatedly edit individual wires when a connectivity batch can be validated and rolled back transactionally.
- Never bypass strict ERC/DRC, `final_gate`, production manifest, or manufacturer gates.
- Before claiming a production package or order-ready release, run:

```bash
hooks/pre-final-pcb-flow.sh specs/<project>.yaml --require-checks --require-fab
```

- Browser login, CAPTCHA, QR, SMS, 2FA, payment, and order submission require explicit user action. Never request or store credentials in project files.
