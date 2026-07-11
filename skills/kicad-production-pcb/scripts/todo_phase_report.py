#!/usr/bin/env python3
"""Report pre-fabrication TODO blockers separately from post-fabrication validation items."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _readiness_checks import todo_phase_report_main


if __name__ == "__main__":
    raise SystemExit(todo_phase_report_main(sys.argv))
