#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cp_server.py — Thin skill CLI shim for code-loop CP server / gateway."""
from pathlib import Path
import sys

_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.append(str(_here))
from _shim import delegate  # noqa: E402

if __name__ == "__main__":
    delegate("cp_server.py", from_file=Path(__file__))
