#!/usr/bin/env python3
"""Validate component pad connectivity declared by a spec."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pcb_skill_checks import net_graph_main


if __name__ == "__main__":
    raise SystemExit(net_graph_main(sys.argv))
