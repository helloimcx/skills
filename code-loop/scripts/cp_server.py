#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cp_server.py — Thin skill CLI shim for code-loop CP server / gateway."""
import importlib.util
from pathlib import Path

_shim_path = Path(__file__).resolve().parent / "_shim.py"
_spec = importlib.util.spec_from_file_location("_shim", str(_shim_path))
_shim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_shim)

if __name__ == "__main__":
    _shim.delegate("cp_server.py", from_file=Path(__file__))
