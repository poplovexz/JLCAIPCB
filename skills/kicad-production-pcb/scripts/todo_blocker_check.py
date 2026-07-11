#!/usr/bin/env python3
"""Block production claims when unresolved TODO items remain."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _readiness_checks import todo_blocker_main


if __name__ == "__main__":
    raise SystemExit(todo_blocker_main(sys.argv))
