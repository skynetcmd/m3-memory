"""catalog.lazy — LazyImpl / LazyModuleProxy.

Moved verbatim from mcp_tool_catalog.py (bin/mcp_tool_catalog.py, module top)
as part of the catalog/ subpackage split. Defers `import module_name` until
first call, so heavyweight modules (memory_core, memory_maintenance, ...) are
not imported at catalog-build time.
"""
from __future__ import annotations

import importlib


class LazyImpl:
    def __init__(self, module_name: str, attr_name: str):
        self.module_name = module_name
        self.attr_name = attr_name
        self._cached_func = None

    def __call__(self, *args, **kwargs):
        if self._cached_func is None:
            mod = importlib.import_module(self.module_name)
            self._cached_func = getattr(mod, self.attr_name)
        return self._cached_func(*args, **kwargs)

class LazyModuleProxy:
    def __init__(self, module_name: str):
        self._module_name = module_name

    def __getattr__(self, name: str):
        return LazyImpl(self._module_name, name)
