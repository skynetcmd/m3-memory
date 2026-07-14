"""Public re-export shim: ``from m3_memory.langchain import Memory`` / ``M3Store``.

This is the one-line-swap path every doc promises. It re-exports the integration
package (``m3_memory.integrations.langchain``) at the short, memorable path a
mem0 migrant reaches for:

    from mem0 import Memory          →   from m3_memory.langchain import Memory

Everything lives in ``m3_memory/integrations/langchain/``; this module just
forwards, including the lazy ``__getattr__`` for the LangChain-subclassing
surfaces so a missing optional dep still fails loud with a clear message.
"""

from __future__ import annotations

from typing import Any

from m3_memory.integrations import langchain as _pkg
from m3_memory.integrations.langchain import (  # noqa: F401
    M3Memory,
    Memory,
    MemoryClient,
    __all__,
)


def __getattr__(name: str) -> Any:
    # Forward lazy attributes (M3Store / history / retriever) to the package.
    return getattr(_pkg, name)
