#!/usr/bin/env python3
"""Run proposal -> temporary spec -> generated batch validation -> optional merge."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _spec_patch_txn import transaction_main


if __name__ == "__main__":
    raise SystemExit(transaction_main(sys.argv))
