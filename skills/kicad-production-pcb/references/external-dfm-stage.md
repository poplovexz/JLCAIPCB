# External DFM and Order-Ready Stage

Use this stage only after the Local Validation Stage, clean manufacturing export, local final/JLC production gates, and production manifest pass.

## Required Sequence

1. Check the current production manifest. Do not upload stale or mixed release files.
2. Open the spec-declared manufacturer/DFM page with the available browser MCP. Continue when already authenticated; pause only for login, CAPTCHA, QR, SMS, 2FA, account selection, payment, or other credential-bearing action.
3. Upload only files selected by the current upload manifest. Do not substitute similarly named files from another directory.
4. Review every spec-declared item: DFM result, board parameters/preview, BOM mapping, CPL placement/rotation/polarity, DNP handling, and any project-specific manufacturing checks.
5. Capture the visible result and source URL. Import evidence with its exact item ID, type, reviewer/automation identity, result, and notes. Validate every item before writing the batch.
6. Require every item to be `passed`, every evidence file hash to match, and every release fingerprint to exactly equal the current files for all declared roles. Missing fingerprint files fail closed.
7. When `evidence_max_age_hours` is declared, reject expired evidence. Any fingerprinted upload change always invalidates evidence regardless of age.
8. Run the evidence checker and the JLC order-ready gate, then rewrite/check the production manifest so the final evidence inventory is hash-bound.

```bash
python3 <skill>/scripts/production_manifest_gate.py --check specs/<project>.yaml
python3 scripts/jlcpcb_evidence.py specs/<project>.yaml \
  --item <id> <captured-file> --result passed \
  --evidence-type <declared-type> --reviewer <browser-adapter> --source-url <visible-url>
python3 <skill>/scripts/evidence_manifest_check.py --order-ready specs/<project>.yaml
python3 scripts/jlcpcb_gate.py --production --order-ready specs/<project>.yaml
python3 <skill>/scripts/production_manifest_gate.py --write specs/<project>.yaml
python3 <skill>/scripts/production_manifest_gate.py --check specs/<project>.yaml
```

## Boundary

This proves traceable evidence for the exact uploaded release and observed web result. It cannot cryptographically prove that an arbitrary image originated from an authenticated browser session; Codex must capture it through the browser operation chain and must never relabel a local package image as external evidence. Payment and order submission remain explicit user actions unless separately authorized.
