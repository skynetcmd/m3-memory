"""m3-memory ↔ PydanticAI integration.

Public surface — give a PydanticAI agent m3-backed long-term memory:

    from pydantic_ai import Agent
    from m3_memory.integrations.pydantic_ai import (
        M3Deps, register_m3_tools, m3_recall_processor,
    )

    agent = Agent(
        "anthropic:claude-sonnet-5",
        deps_type=M3Deps,
        history_processors=[m3_recall_processor()],   # optional auto-recall
    )
    register_m3_tools(agent)                           # remember/recall/forget tools

    agent.run_sync("remember I prefer dark roast", deps=M3Deps(user_id="alice"))

PydanticAI ships NO built-in persistent memory; it exposes memory through its
dependency system + tools + history-processors. This adapter provides both tiers:

  * Tier 1 (this module's ``register_m3_tools`` + ``m3_recall_processor``) — the
    informal, deps-injected pattern.
  * Tier 2 (``M3MemoryToolset``) — a formal ``AbstractToolset`` subclass, so m3 is
    a first-class, attachable, isinstance-checkable PydanticAI toolset (the direct
    analog to CrewAI's StorageBackend conformance). See ``toolset.py``.

Everything rides m3's one canonical in-process dispatch (Recipe 2,
docs/EXTENDING.md), so it works over every storage backend (SQLite/PostgreSQL)
unchanged, and a memory written here stays searchable by every other m3 agent.
PydanticAI is an OPTIONAL dependency, imported lazily so importing this package
never hard-requires it (§3 fail-loud at access time).
"""

from __future__ import annotations

from typing import Any

# PydanticAI is fully Python-3.14-compatible (built on Pydantic v2, no chromadb),
# so — unlike the CrewAI adapter — there is no interpreter cap here. We pin the
# API this adapter uses: deps_type + RunContext + AbstractToolset (Tier 2), all
# present in the 2.x line.
MIN_PYDANTIC_AI_VERSION = "2.0.0"

__all__ = [
    "M3Deps",
    "register_m3_tools",
    "m3_recall_processor",
    "M3MemoryToolset",
    "MIN_PYDANTIC_AI_VERSION",
]

_PAI_HINT = (
    "The PydanticAI integration requires pydantic-ai v{min}+. Install it with:\n"
    "    pip install m3-memory[pydantic-ai]\n"
    "or\n"
    "    pip install 'pydantic-ai>={min}'   # or the lighter 'pydantic-ai-slim'"
).format(min=MIN_PYDANTIC_AI_VERSION)


def _check_pydantic_ai_version() -> None:
    """Fail loud (§3) if pydantic-ai is absent or too old.

    Accepts either the full ``pydantic-ai`` or the slim ``pydantic-ai-slim``
    distribution (both expose the same ``pydantic_ai`` import package).
    """
    from importlib import metadata

    raw = None
    for dist in ("pydantic-ai", "pydantic-ai-slim"):
        try:
            raw = metadata.version(dist)
            break
        except Exception:  # noqa: BLE001 — try the next distribution name
            continue
    if raw is None:
        raise ImportError(_PAI_HINT)

    def _parts(v: str) -> tuple[int, ...]:
        out: list[int] = []
        for chunk in v.split(".")[:3]:
            num = ""
            for ch in chunk:
                if ch.isdigit():
                    num += ch
                else:
                    break
            out.append(int(num) if num else 0)
        return tuple(out)

    if _parts(raw) < _parts(MIN_PYDANTIC_AI_VERSION):
        raise ImportError(
            f"pydantic-ai {raw} is installed, but the m3 integration needs "
            f">={MIN_PYDANTIC_AI_VERSION}.\n{_PAI_HINT}"
        )


def __getattr__(name: str) -> Any:
    """Lazy-import the PydanticAI-coupled surface so importing this package never
    hard-requires pydantic-ai (mirrors the crewai/langchain adapters).

    ``M3Deps`` is pure (no pydantic_ai import) and could be exported eagerly, but
    routing it through the guard too gives one consistent, actionable error when
    the framework is missing.
    """
    if name in __all__ and name != "MIN_PYDANTIC_AI_VERSION":
        _check_pydantic_ai_version()
        try:
            if name == "M3Deps":
                from .deps import M3Deps

                return M3Deps
            if name in ("register_m3_tools", "m3_recall_processor"):
                from . import tools

                return getattr(tools, name)
            if name == "M3MemoryToolset":
                from .toolset import M3MemoryToolset

                return M3MemoryToolset
        except ImportError as e:
            raise ImportError(f"{name}: {_PAI_HINT}") from e
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
