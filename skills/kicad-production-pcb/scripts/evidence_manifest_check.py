#!/usr/bin/env python3
"""Validate release evidence manifests and order-ready evidence."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pcb_skill_checks import evidence_manifest_main


if __name__ == "__main__":
    raise SystemExit(evidence_manifest_main(sys.argv))
