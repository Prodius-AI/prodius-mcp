"""Pytest bootstrap for the prodius-mcp test suite.

Puts `src/` on sys.path when the package isn't installed, so
`python -m pytest tests` works from a plain checkout as well as after
`pip install -e .`.
"""

import importlib.util
import os
import sys
from pathlib import Path

if importlib.util.find_spec("prodius_mcp") is None:
    _SRC = Path(__file__).parent / "src"
    if _SRC.is_dir():
        _src = str(_SRC)
        # In-process imports (test modules import prodius_mcp directly).
        sys.path.insert(0, _src)
        # Subprocess imports: test_jsonrpc spawns `python -m prodius_mcp`,
        # which inherits os.environ but not this process's sys.path. Prepend
        # src to PYTHONPATH so the child can import the package too.
        _pp = os.environ.get("PYTHONPATH", "")
        if _src not in _pp.split(os.pathsep):
            os.environ["PYTHONPATH"] = (
                _src + os.pathsep + _pp if _pp else _src
            )
