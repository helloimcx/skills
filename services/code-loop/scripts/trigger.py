#!/usr/bin/env python3
"""Event trigger CLI (thin entry): the implementation lives in codeloop.trigger.

Path-stable delegate target for the skill shim (runpy) and scheduled tasks;
argv parsing, JSON output and exit codes are produced by codeloop.trigger.main.
"""
import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
if str(SERVICE) not in sys.path:
    sys.path.insert(0, str(SERVICE))

from codeloop.trigger import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
