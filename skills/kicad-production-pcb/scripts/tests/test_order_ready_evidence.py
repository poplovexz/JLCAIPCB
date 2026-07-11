#!/usr/bin/env python3
"""Exercise exact and fresh order-ready evidence fingerprints."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
from _pcb_skill_checks import CheckResult, check_evidence_manifest  # noqa: E402


def timestamp(hours_ago: int = 0) -> str:
    value = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run_check(spec: dict) -> CheckResult:
    result = CheckResult()
    check_evidence_manifest(spec, result, order_ready=True)
    return result


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="order-ready-evidence-") as temporary:
        previous = Path.cwd()
        root = Path(temporary)
        os.chdir(root)
        try:
            release = root / "release"
            evidence_dir = release / "evidence"
            evidence_dir.mkdir(parents=True)
            (evidence_dir / "dfm.png").write_bytes(b"evidence")
            (release / "release.json").write_text(
                json.dumps({"release_files": [{"role": "upload", "file": "upload.zip", "sha256": "abc"}]}),
                encoding="utf-8",
            )
            entry = {
                "file": str(evidence_dir / "dfm.png"),
                "sha256": "ee8250fb76e094b34b471f13a73dbbe51d1ae142e9df59d7c0d31ec20f0a0a8e",
                "result": "passed",
                "reviewer": "browser-mcp",
                "source_url": "https://example.invalid/review",
                "evidence_type": "dfm_screenshot",
                "imported_at_utc": timestamp(),
                "release_fingerprint": {"roles": ["upload"], "files": {"upload.zip": "abc"}},
            }
            manifest = {"evidence": {"dfm": entry}}
            (release / "evidence.json").write_text(json.dumps(manifest), encoding="utf-8")
            spec = {
                "manufacturing": {"jlcpcb": {"release": {
                    "output_dir": str(release),
                    "manifest": "release.json",
                    "order_review": {
                        "evidence_manifest": "evidence.json",
                        "fingerprint_roles": ["upload"],
                        "evidence_max_age_hours": 24,
                        "required_evidence_fields": ["reviewer", "source_url", "evidence_type"],
                        "required_items": [{"id": "dfm", "evidence_type": "dfm_screenshot"}],
                    },
                }}},
            }
            result = run_check(spec)
            if result.issues:
                failures.append(f"valid evidence failed: {result.issues}")

            entry["release_fingerprint"]["files"] = {}
            (release / "evidence.json").write_text(json.dumps(manifest), encoding="utf-8")
            result = run_check(spec)
            if not any("does not exactly match" in issue for issue in result.issues):
                failures.append("missing fingerprint file was accepted")

            entry["release_fingerprint"]["files"] = {"upload.zip": "abc"}
            entry["imported_at_utc"] = timestamp(48)
            (release / "evidence.json").write_text(json.dumps(manifest), encoding="utf-8")
            result = run_check(spec)
            if not any("older than" in issue for issue in result.issues):
                failures.append("expired web evidence was accepted")
        finally:
            os.chdir(previous)

    if failures:
        print("order-ready evidence tests: FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("order-ready evidence tests: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
