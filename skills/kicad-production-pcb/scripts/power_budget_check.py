#!/usr/bin/env python3
"""Validate declared PCB power domains and current budgets."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _readiness_checks import power_budget_main


if __name__ == "__main__":
    raise SystemExit(power_budget_main(sys.argv))
