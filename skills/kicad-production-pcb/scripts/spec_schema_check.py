#!/usr/bin/env python3
"""Validate a spec before generating KiCad files."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pcb_skill_checks import schema_main


if __name__ == "__main__":
    raise SystemExit(schema_main(sys.argv))
