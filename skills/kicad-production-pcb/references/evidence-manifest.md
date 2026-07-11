# Evidence Manifest Reference

Use this reference when making production-ready/order-ready claims or changing release, evidence, hash, or gate behavior.

## Principle

Final status must be derived from evidence files, not from Codex confidence.

Each gate should produce machine-readable and human-readable evidence:

```text
artifacts/verification/<project>/<gate>.json
artifacts/verification/<project>/<gate>.md
```

## Evidence Record

Recommended JSON shape:

```json
{
  "gate": "net_graph",
  "result": "passed",
  "type": "hard_gate",
  "project": "example",
  "inputs": [
    {"path": "specs/example.yaml", "sha256": "..."},
    {"path": "projects/example/example.kicad_sch", "sha256": "..."}
  ],
  "checks": [
    {"id": "expected_pin_connected", "result": "passed"}
  ],
  "blocking_issues": [],
  "advisory_issues": []
}
```

## Required Manifest Roles

For production claims, require evidence roles such as:

- `spec_schema_report`
- `net_graph_report`
- `erc_report`
- `drc_report`
- `gerber_manifest`
- `bom_cpl_audit`
- `library_audit`
- `jlcpcb_gate_report`
- `release_manifest`
- `upload_manifest` when configured
- `external_order_evidence` for order-ready

## Hash Binding

Evidence must bind to the current release inputs.

Reject evidence when:

- a fingerprinted upload file changed;
- release manifest hashes no longer match;
- evidence references unknown IDs;
- evidence result is not `passed` for an order-ready requirement;
- evidence source URL/type/reviewer fields are missing when required.

## JLCPCB Web Evidence

For JLCPCB/JLCDFM order-ready evidence:

- Run local production gates first.
- Use Chrome MCP only after the release bundle and upload manifest are current.
- If the browser is already logged in or auto-logged-in, continue the upload and review flow.
- If login, CAPTCHA, SMS, QR, password, 2FA, account selection, or payment appears, stop and ask the user to complete it. Resume only after the user confirms login or manual action is complete.
- Do not capture or store credentials or verification codes.
- Import only visible passed web results as `--result passed`.
- Bind imported evidence to the current release fingerprint; changing Gerber zip, BOM, CPL, designators, order options, or upload manifest makes old evidence stale.

## Claim Rules

- No evidence means no claim.
- Stale evidence means blocked until refreshed or explicitly revalidated.
- Advisory evidence cannot satisfy a hard gate.
- External evidence cannot be faked by local package files.
- TODO items that affect pre-fabrication electrical, mechanical, thermal, sourcing, or manufacturing risk block production-package claims.
- Post-fabrication bring-up, lab, load, thermal-measurement, USB real-device, and mechanical-fit TODO items belong in the post-fab validation plan. Use explicit `post_fab_*` categories, or set `phase: post_fab` on legacy `bringup`/`lab_test` categories. They do not block AI generation of Gerber/BOM/CPL/JLCDFM-ready packages; they block only function-validated, lab-validated, or mass-production-proven claims.

## Blocked Report

When blocked, report:

- current stage;
- missing evidence roles;
- stale evidence fingerprints;
- exact files or user actions required;
- commands intentionally not run.
