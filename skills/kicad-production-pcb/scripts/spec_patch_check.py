#!/usr/bin/env python3
"""Validate an additive specs.yaml proposal."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _spec_patch_txn import patch_check_main


if __name__ == "__main__":
    raise SystemExit(patch_check_main(sys.argv))
