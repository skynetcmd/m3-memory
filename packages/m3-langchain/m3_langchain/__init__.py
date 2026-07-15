"""``m3-langchain`` — a recognizable alias for m3-memory's LangChain surface.

This package is a **thin re-export shim**. The integration itself lives in
``m3-memory`` (installed as a dependency here, with its ``[langchain]`` extra);
this package exists only so the conventional, discoverable name
``m3-langchain`` (à la ``databricks-langchain``) resolves on PyPI and imports:

    pip install m3-langchain
    from m3_langchain import Memory, M3Store, M3Saver   # same objects as m3_memory.langchain

It intentionally holds **no integration code** — every name forwards to
``m3_memory.langchain``, so a new surface added upstream is available here with
no change to this package (nothing to keep in sync, nothing to drift). Prefer
``from m3_memory.langchain import ...`` in new code; this alias is for
name-discoverability, not a separate API.
"""

from __future__ import annotations

from typing import Any

# Re-export the eager (no-LangChain-dep) surface directly so plain
# `from m3_langchain import Memory` works even before the lazy machinery runs.
from m3_memory.langchain import (  # noqa: F401
    M3Memory,
    Memory,
    MemoryClient,
    __all__,
)


def __getattr__(name: str) -> Any:
    """Forward every other attribute (M3Store / M3Saver / M3Retriever /
    history / LCEL) to ``m3_memory.langchain``, preserving its lazy
    optional-dependency ImportError hints. Because this delegates rather than
    enumerating, any surface added upstream is exposed here automatically."""
    import m3_memory.langchain as _pkg

    return getattr(_pkg, name)


def __dir__() -> list[str]:
    import m3_memory.langchain as _pkg

    return sorted(set(list(globals()) + list(getattr(_pkg, "__all__", []))))
