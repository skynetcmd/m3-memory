"""``M3Deps`` — the m3 memory handle a PydanticAI agent receives via ``deps``.

PydanticAI has no built-in persistent memory; memory is added by injecting a
service through the agent's dependency system (``deps_type=M3Deps``) and exposing
tools/history-processors that call it. ``M3Deps`` is that service: a thin,
tenant-scoped facade over the framework-agnostic ``M3Client`` dispatch core
(reused from the langchain adapter — Recipe 2, docs/EXTENDING.md).

Tenancy (§7): m3 has no anonymous/global mode. ``user_id`` is required at
construction and stamped on every call; a missing tenant fails loud rather than
leaking across tenants. One ``M3Deps`` per user/session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# The agent_id stamped on writes this adapter makes (mirrors "crewai"/"langchain").
_AGENT_ID = "pydantic-ai"
# All memories this adapter writes live under one scope so they round-trip and are
# filterable; other m3 agents can still search across scopes.
_M3_SCOPE = "agent"


@dataclass
class M3Deps:
    """Injected m3 memory service for a PydanticAI agent.

    Example::

        from pydantic_ai import Agent
        from m3_memory.integrations.pydantic_ai import M3Deps, register_m3_tools

        agent = Agent("anthropic:claude-sonnet-5", deps_type=M3Deps)
        register_m3_tools(agent)
        agent.run_sync("remember I prefer dark roast", deps=M3Deps(user_id="alice"))
    """

    user_id: str
    scope: str = _M3_SCOPE
    call_timeout: float = 30.0
    # Lazily-constructed shared dispatch client (not part of the public API).
    _client: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.user_id or not str(self.user_id).strip():
            raise ValueError(
                "M3Deps requires a non-empty user_id — m3 enforces per-tenant "
                "isolation (DESIGN_PHILOSOPHIES §7); there is no anonymous/global "
                "mode. Pass one M3Deps per user/session, e.g. "
                "M3Deps(user_id='alice')."
            )
        self.user_id = str(self.user_id).strip()

    @property
    def client(self) -> Any:
        """The shared ``M3Client`` (constructed on first use).

        Reuses the langchain adapter's framework-agnostic dispatch core — one
        event-loop thread process-wide, all m3 tools reachable, our agent_id
        stamped on writes.
        """
        if self._client is None:
            from ..langchain.m3client import M3Client

            self._client = M3Client(
                agent_id=_AGENT_ID, call_timeout=self.call_timeout
            )
        return self._client

    # ── the three memory operations the tools + toolset call ──────────────────

    def remember(self, content: str, *, type: str = "auto", importance: float = 0.5,
                 metadata: str = "{}") -> str:
        """Write a memory for this tenant. Returns the new memory id (or a status
        string on the rare parse-miss). ``type='auto'`` lets m3 classify it."""
        content = str(content or "").strip()
        if not content:
            return ""
        raw = self.client._tool(
            "memory_write",
            type=type,
            content=content,
            user_id=self.user_id,
            scope=self.scope,
            importance=importance,
            metadata=metadata,
            auto_classify=(type == "auto"),
        )
        return _parse_written_id(raw) or (raw if isinstance(raw, str) else "")

    def recall(self, query: str, *, k: int = 5) -> list:
        """Vector+keyword search this tenant's memories for ``query``.

        Returns m3's native ``list[(score, item_dict)]``. PydanticAI hands us
        TEXT (not a vector), so this uses the standard search path which embeds
        internally — no CrewAI-style vector-space handling needed.
        """
        query = str(query or "").strip()
        if not query:
            return []
        rows = self.client._tool(
            "memory_search_scored",
            query=query,
            user_id=self.user_id,
            scope=self.scope,
            k=int(k),
        )
        return rows or []

    def forget(self, memory_id: str) -> bool:
        """Soft-delete one memory by id (bi-temporal — closed, not destroyed; §9).
        Returns True if a row was affected."""
        mid = str(memory_id or "").strip()
        if not mid:
            return False
        raw = self.client._tool("memory_delete_bulk", ids=[mid])
        return _deleted_any(raw)


def _parse_written_id(raw: Any) -> str:
    """memory_write's success string is 'Created: <uuid>[ (...)]'. Extract the
    uuid defensively (same contract supersede/distill rely on)."""
    if isinstance(raw, str) and raw.startswith("Created:"):
        tok = raw.split("Created:", 1)[1].strip().split()
        return tok[0] if tok else ""
    return ""


def _deleted_any(raw: Any) -> bool:
    """Best-effort read of memory_delete_bulk's result → 'did anything delete?'."""
    if isinstance(raw, dict):
        for key in ("deleted", "count", "n"):
            if isinstance(raw.get(key), int):
                return raw[key] > 0
    if isinstance(raw, int):
        return raw > 0
    # A non-error string reply means it ran; treat as success.
    return isinstance(raw, str) and not raw.lower().startswith("error")
