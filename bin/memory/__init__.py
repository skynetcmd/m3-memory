"""m3-memory modularization package.

Holds extracted concerns from the legacy `bin/memory_core.py`. The legacy
module remains the public surface and re-exports symbols from here.

See `docs/MEMORY_CORE_MODULARIZATION.md` for the migration plan and the
list of which Phase moves which submodule.

## Why submodules are imported eagerly

The legacy `memory_core` shim does `from .memory.config import *` (et al.)
at its own module-evaluation time. Submodules are therefore guaranteed
to be loaded by the time any caller touches `memory_core`. We don't
defer imports here, because:

  1. `importlib.reload(memory_core)` — used by `tests/test_memory_bridge.py`
     — should reload all submodules together. Eager imports make that
     trivial; lazy imports complicate it.
  2. Cold-start cost is negligible (~few ms). The original `memory_core`
     paid all of this already.
"""
from . import config  # noqa: F401
from . import util  # noqa: F401

__all__ = ["config", "util"]
