#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""operator.py — Thin skill CLI shim for code-loop operator round CLI."""
if __name__ == "operator":
    # Collision guard: this file was imported as 'operator' because its directory is on sys.path.
    # Load the real standard library operator module by bypassing the script directory.
    import importlib
    import sys

    _scripts_dirs = [p for p in sys.path if p.replace("\\", "/").rstrip("/").endswith("/scripts")]
    for _d in _scripts_dirs:
        sys.path.remove(_d)
    try:
        del sys.modules["operator"]
        _real_op = importlib.import_module("operator")
        sys.modules["operator"] = _real_op
        globals().update(_real_op.__dict__)
    finally:
        for _d in _scripts_dirs:
            sys.path.append(_d)
else:
    import importlib.util
    from pathlib import Path

    _shim_path = Path(__file__).resolve().parent / "_shim.py"
    _spec = importlib.util.spec_from_file_location("_shim", str(_shim_path))
    _shim = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_shim)

    if __name__ == "__main__":
        _shim.delegate("operator.py", from_file=Path(__file__))
