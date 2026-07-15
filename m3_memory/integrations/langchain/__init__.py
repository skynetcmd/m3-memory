"""m3-memory ↔ LangChain / LangGraph integration.

Public surface (the one-line import swap all docs promise):

    from m3_memory.langchain import Memory        # mem0-compatible, PR-1 headline
    from m3_memory.langchain import M3Memory       # explicit-name alias
    from m3_memory.langchain import MemoryClient    # mem0 hosted-name shadow (local)
    from m3_memory.langchain import M3Store          # LangGraph/LangMem BaseStore (PR-2)
    from m3_memory.langchain import M3ChatMessageHistory, with_m3_history   # (PR-3)
    from m3_memory.langchain import M3Retriever      # RAG BaseRetriever (PR-3)
    from m3_memory.langchain import M3Saver           # LangGraph BaseCheckpointSaver (PR-4)

The mem0-compat surface (``Memory``/``M3Memory``/``MemoryClient``) has NO hard
LangChain dependency — it's pure m3, so it imports eagerly. The LangChain-native
surfaces (``M3Store``/``M3Retriever``/history) subclass LangChain base classes,
so they import LAZILY via ``__getattr__``: a missing ``langchain-core``/
``langgraph`` fails loud with a clear ``pip install m3-memory[langchain]``
message at ACCESS time, never a mid-call crash (§3 robustness).
"""

from __future__ import annotations

from typing import Any

# Pure-m3, no LangChain dep — safe to import eagerly.
from .mem0_compat import M3Memory, Memory, MemoryClient

__all__ = [
    "Memory",
    "MemoryClient",
    "M3Memory",
    "M3Store",
    "M3ChatMessageHistory",
    "with_m3_history",
    "M3Retriever",
    "M3Saver",
    "MemoryWrite",
    "MemoryRetrieve",
    "with_m3_memory",
]

_LANGCHAIN_HINT = (
    "This surface requires LangChain. Install it with:\n"
    "    pip install m3-memory[langchain]\n"
    "(the mem0-compatible Memory/M3Memory classes need no LangChain and are "
    "already importable.)"
)


def __getattr__(name: str) -> Any:
    """Lazy-import the LangChain-subclassing surfaces so core stays dep-free."""
    if name in ("M3Store",):
        try:
            from .store import M3Store
        except ImportError as e:  # missing optional langchain dep
            raise ImportError(f"{name}: {_LANGCHAIN_HINT}") from e
        return M3Store
    if name in ("M3ChatMessageHistory", "with_m3_history"):
        try:
            from .history import M3ChatMessageHistory, with_m3_history
        except ImportError as e:
            raise ImportError(f"{name}: {_LANGCHAIN_HINT}") from e
        return {"M3ChatMessageHistory": M3ChatMessageHistory,
                "with_m3_history": with_m3_history}[name]
    if name in ("M3Retriever",):
        try:
            from .retriever import M3Retriever
        except ImportError as e:
            raise ImportError(f"{name}: {_LANGCHAIN_HINT}") from e
        return M3Retriever
    if name in ("M3Saver",):
        try:
            from .checkpoint import M3Saver
        except ImportError as e:
            raise ImportError(f"{name}: {_LANGCHAIN_HINT}") from e
        return M3Saver
    if name in ("MemoryWrite", "MemoryRetrieve", "with_m3_memory"):
        try:
            from .lcel import MemoryRetrieve, MemoryWrite, with_m3_memory
        except ImportError as e:
            raise ImportError(f"{name}: {_LANGCHAIN_HINT}") from e
        return {"MemoryWrite": MemoryWrite, "MemoryRetrieve": MemoryRetrieve,
                "with_m3_memory": with_m3_memory}[name]
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
