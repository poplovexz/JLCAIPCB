#!/usr/bin/env python3
"""Validate module-level connectivity batches declared by a spec."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pcb_skill_checks import connectivity_batch_main


if __name__ == "__main__":
    raise SystemExit(connectivity_batch_main(sys.argv))
