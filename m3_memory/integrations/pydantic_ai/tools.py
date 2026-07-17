"""Tier 1 — the informal PydanticAI memory surface: deps-injected tools + a
recall history-processor.

This is the lightest way to give a PydanticAI agent m3-backed memory. The agent
declares ``deps_type=M3Deps``; ``register_m3_tools(agent)`` attaches three tools
the model can call (``remember`` / ``recall`` / ``forget``); ``m3_recall_processor``
optionally injects relevant memories into the message history automatically each
turn.

Both go through ``M3Deps`` → the shared ``M3Client`` dispatch core, so they work
over every m3 storage backend (SQLite / PostgreSQL / MariaDB) with no per-backend
code, and a memory written here is immediately searchable by every other m3 agent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import mapping
from .deps import M3Deps

if TYPE_CHECKING:  # import only for types; never a hard runtime dep on pydantic_ai
    from pydantic_ai import Agent, RunContext


def register_m3_tools(agent: "Agent[M3Deps, Any]") -> "Agent[M3Deps, Any]":
    """Attach ``remember`` / ``recall`` / ``forget`` tools to ``agent``.

    The agent MUST have been constructed with ``deps_type=M3Deps`` (the tools read
    ``ctx.deps`` for the tenant + m3 handle). Returns the same agent for chaining.
    """
    # PydanticAI resolves each tool's annotations at registration via
    # get_type_hints, so the string annotation "RunContext[M3Deps]" on the tool
    # functions must be evaluable in this module's globals. Bind it for real (it's
    # only under TYPE_CHECKING at module top to keep the import optional).
    from pydantic_ai import RunContext  # noqa: F401 — annotation resolution
    globals().setdefault("RunContext", RunContext)

    @agent.tool
    def remember(
        ctx: "RunContext[M3Deps]",
        content: str,
        importance: float = 0.5,
    ) -> str:
        """Save a fact to long-term memory so it can be recalled in future turns.

        Args:
            content: The fact to remember, in natural language.
            importance: 0.0–1.0 salience; higher is retained/ranked more strongly.
        """
        new_id = ctx.deps.remember(content, importance=importance)
        return f"Remembered (id={new_id})." if new_id else "Nothing to remember."

    @agent.tool
    def recall(
        ctx: "RunContext[M3Deps]",
        query: str,
        limit: int = 5,
    ) -> list[dict]:
        """Search long-term memory for facts relevant to ``query``.

        Returns a list of memories (most relevant first), each with its content,
        relevance score, type, and id.
        """
        rows = ctx.deps.recall(query, k=limit)
        return mapping.recall_hits_to_dicts(rows)

    @agent.tool
    def forget(ctx: "RunContext[M3Deps]", memory_id: str) -> str:
        """Delete a memory by its id (from a prior ``recall`` result)."""
        ok = ctx.deps.forget(memory_id)
        return "Forgotten." if ok else "No such memory."

    return agent


def m3_recall_processor(
    *,
    k: int = 5,
    header: str = "Relevant memories",
) -> Any:
    """Build a PydanticAI ``history_processor`` that prepends relevant memories.

    The returned async callable matches PydanticAI's history-processor contract
    ``(ctx, messages) -> messages``: on each model request it takes the latest
    user text, searches this tenant's m3 memories, and — if any are found —
    prepends a single system message carrying them, so the model sees recalled
    context without the agent author writing retrieval glue.

    Bounded by design (§ token hygiene): only the LATEST user turn drives the
    search, and only ``k`` hits are injected. Empty results inject nothing (no
    empty-block noise). Never raises into the run — a memory backend hiccup must
    not break generation; on error it returns the messages unchanged.
    """
    # Bind RunContext for real so PydanticAI can resolve the processor's
    # annotation (it inspects the signature to decide whether to pass ctx).
    from pydantic_ai import RunContext  # noqa: F401 — annotation resolution
    globals().setdefault("RunContext", RunContext)

    async def _processor(ctx: "RunContext[M3Deps]", messages: list) -> list:
        try:
            deps = getattr(ctx, "deps", None)
            if not isinstance(deps, M3Deps):
                return messages
            query = _latest_user_text(messages)
            if not query:
                return messages
            rows = deps.recall(query, k=k)
            block = mapping.recalled_memories_block(rows, header=header)
            if not block:
                return messages
            return [_system_message(block), *messages]
        except Exception:  # noqa: BLE001 — recall is best-effort; never break the run
            return messages

    return _processor


# ── PydanticAI message-shape helpers (kept out of mapping.py because they need
#    the live pydantic_ai message classes; imported lazily so the module stays
#    importable without pydantic_ai installed) ─────────────────────────────────

def _latest_user_text(messages: list) -> str:
    """Extract the most recent user-authored text from a message list.

    Walks messages newest-first, looking for a ``UserPromptPart``-shaped part.
    Robust to either the real pydantic_ai part classes or a duck-typed test
    stand-in (checks ``part_kind == 'user-prompt'`` then falls back to a
    ``content`` attr on a request-role message).
    """
    for msg in reversed(messages or []):
        parts = getattr(msg, "parts", None)
        if not parts:
            continue
        for part in reversed(parts):
            kind = getattr(part, "part_kind", None)
            if kind == "user-prompt":
                content = getattr(part, "content", None)
                if isinstance(content, str) and content.strip():
                    return content.strip()
                # multimodal content: take the first str chunk
                if isinstance(content, (list, tuple)):
                    for c in content:
                        if isinstance(c, str) and c.strip():
                            return c.strip()
    return ""


def _system_message(text: str) -> Any:
    """A ``ModelRequest`` carrying one ``SystemPromptPart`` with ``text``.

    Imported lazily so this module imports without pydantic_ai present (the
    version guard in __init__ is the single fail-loud point).
    """
    from pydantic_ai.messages import ModelRequest, SystemPromptPart

    return ModelRequest(parts=[SystemPromptPart(content=text)])
