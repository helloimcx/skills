#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""worker_agent.py — Thin skill CLI shim for code-loop worker agent."""
from pathlib import Path
import sys

_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.append(str(_here))
from _shim import delegate  # noqa: E402

if __name__ == "__main__":
    delegate("worker_agent.py", from_file=Path(__file__))
