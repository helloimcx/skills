# Stdlib operator collision guard
if __name__ == "operator":
    import _operator as _std_op

    for _k, _v in _std_op.__dict__.items():
        if not _k.startswith("__"):
            globals()[_k] = _v
else:
    #!/usr/bin/env python3
    # -*- coding: utf-8 -*-
    """operator.py — Thin skill CLI shim for code-loop operator round CLI."""
    from pathlib import Path
    import sys

    # Ensure _shim can be imported
    _here = Path(__file__).resolve().parent
    if str(_here) not in sys.path:
        sys.path.append(str(_here))
    from _shim import delegate  # noqa: E402

    if __name__ == "__main__":
        delegate("operator.py", from_file=Path(__file__))
