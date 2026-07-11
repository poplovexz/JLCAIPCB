#!/usr/bin/env python3
"""Verify a specs.yaml change is additive-only."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _spec_patch_txn import diff_guard_main


if __name__ == "__main__":
    raise SystemExit(diff_guard_main(sys.argv))
