#!/usr/bin/env python3
"""Validate requirements completeness before generating a real PCB."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _readiness_checks import requirements_main


if __name__ == "__main__":
    raise SystemExit(requirements_main(sys.argv))
