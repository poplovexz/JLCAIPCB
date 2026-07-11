#!/usr/bin/env python3
"""Apply an additive specs.yaml proposal after deterministic checks."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _spec_patch_txn import patch_apply_main


if __name__ == "__main__":
    raise SystemExit(patch_apply_main(sys.argv))
