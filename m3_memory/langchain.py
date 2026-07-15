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

# §3 fail-loud: an m3 build that predates / omits the integration payload has no
# `m3_memory/integrations/langchain/` on disk, so this import raises a cryptic
# internal namespace error ("cannot import name 'langchain' from
# 'm3_memory.integrations'"). Translate it into an actionable message — a user
# who followed the docs did nothing wrong; the fix is a version upgrade, not a
# code change on their side. A correctly-installed package takes the fast path
# and this except is never entered (zero overhead, identical object identity).
try:
    from m3_memory.integrations import langchain as _pkg
    from m3_memory.integrations.langchain import (  # noqa: F401
        M3Memory,
        Memory,
        MemoryClient,
        __all__,
    )
except ImportError as _e:  # payload missing — NOT the optional-langchain-dep case
    # The optional-dep case (langchain-core/langgraph not installed) is handled
    # per-surface by the package's own __getattr__ with a `pip install
    # m3-memory[langchain]` hint, and does not reach here (Memory/M3Memory/
    # MemoryClient have no LangChain dependency). So an ImportError here means the
    # integration package itself is absent from this install.
    if "integrations" in str(_e) or "langchain" in str(_e):
        raise ImportError(
            "m3_memory.langchain is unavailable because this m3-memory build does "
            "not ship the LangChain integration payload "
            "(m3_memory/integrations/langchain/). Upgrade to a build that includes "
            "it:\n    pip install -U m3-memory\n"
            "If you installed from source, reinstall the package so the "
            "integration subpackage is importable."
        ) from _e
    raise


def __getattr__(name: str) -> Any:
    # Forward lazy attributes (M3Store / history / retriever / M3Saver) to the
    # package, preserving its per-surface optional-dep ImportError hints.
    return getattr(_pkg, name)
