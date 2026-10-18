#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_ledger.py — Thin skill CLI shim for handler anti-collapse ledger check."""
from pathlib import Path
import sys

_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.append(str(_here))
from _shim import delegate  # noqa: E402

if __name__ == "__main__":
    delegate("check_ledger.py", from_file=Path(__file__))
