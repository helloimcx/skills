import sys
from pathlib import Path

_here = str(Path(__file__).resolve().parent)
_code_loop = str(Path(__file__).resolve().parents[1])
_scripts = str(Path(__file__).resolve().parents[1] / 'scripts')
for _p in (_here, _code_loop, _scripts):
    if _p not in sys.path:
        sys.path.insert(0, _p)
