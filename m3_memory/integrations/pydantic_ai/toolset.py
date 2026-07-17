"""Tier 2 — ``M3MemoryToolset``: m3 as a first-class PydanticAI toolset.

Where Tier 1 (``register_m3_tools``) attaches tools onto an existing agent, this is
the *formal conformance* surface: a subclass of PydanticAI's ``AbstractToolset``
(via the concrete ``FunctionToolset``), so an ``M3MemoryToolset`` instance **is** a
PydanticAI toolset — ``isinstance(ts, AbstractToolset)`` holds — and can be passed
to ``Agent(..., toolsets=[ts])``, composed with other toolsets (``.prefixed()``,
``.filtered()``, …), and introspected exactly like a native one.

This is the direct analog to the CrewAI adapter conforming to CrewAI's
``StorageBackend`` protocol: m3 advertises conformance to the framework's own
extension interface rather than only using the informal pattern. We subclass
``FunctionToolset`` (which already implements ``id``/``get_tools``/``call_tool``
correctly) and register the same three m3-backed tools — reusing the framework's
tool machinery instead of hand-rolling ``ToolsetTool`` objects (EXTENDING.md:
"implement only what's called; reuse the rest").

The tools read the tenant + m3 handle from ``ctx.deps`` (an ``M3Deps``), so an
agent still declares ``deps_type=M3Deps`` and passes ``deps=M3Deps(user_id=...)``
per run. All calls ride the shared ``M3Client`` dispatch core → every m3 backend,
cross-agent-searchable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import mapping
from .deps import M3Deps

if TYPE_CHECKING:
    from pydantic_ai import RunContext


def _build_base():
    """Resolve ``FunctionToolset`` lazily (pydantic_ai is an optional dep)."""
    from pydantic_ai.toolsets import FunctionToolset

    return FunctionToolset


# We can't subclass a lazily-imported base at module top level without importing
# pydantic_ai, which would break the "importable without the framework" contract.
# So the class is built on first construction via a factory that closes over the
# real base. __getattr__ in this module returns the built class.
_CLASS_CACHE: dict[str, type] = {}


def _make_m3_memory_toolset() -> type:
    if "M3MemoryToolset" in _CLASS_CACHE:
        return _CLASS_CACHE["M3MemoryToolset"]

    FunctionToolset = _build_base()
    # Import RunContext for REAL here (not just under TYPE_CHECKING): PydanticAI
    # resolves each tool's annotations at registration via get_type_hints, so the
    # string annotation "RunContext[M3Deps]" on the tool functions must be
    # evaluable in their module globals. Binding it at module scope makes it so.
    from pydantic_ai import RunContext  # noqa: F401 — used by annotation resolution
    globals().setdefault("RunContext", RunContext)

    class M3MemoryToolset(FunctionToolset):  # type: ignore[valid-type,misc]
        """A PydanticAI toolset backing an agent's memory with m3.

        Example::

            from pydantic_ai import Agent
            from m3_memory.integrations.pydantic_ai import M3Deps, M3MemoryToolset

            agent = Agent(
                "anthropic:claude-sonnet-5",
                deps_type=M3Deps,
                toolsets=[M3MemoryToolset()],
            )
            agent.run_sync("remember I like dark roast", deps=M3Deps(user_id="alice"))

        ``isinstance(M3MemoryToolset(), AbstractToolset)`` is True — it is a
        first-class toolset, composable with any other.
        """

        def __init__(self, *, id: str = "m3-memory", **kwargs: Any) -> None:
            super().__init__(tools=[], id=id, **kwargs)
            # Register the three memory tools on this toolset instance. Signatures
            # mirror Tier 1's register_m3_tools so behavior is identical whether an
            # agent uses the toolset or the loose tools.
            self.add_function(self._remember, name="remember")
            self.add_function(self._recall, name="recall")
            self.add_function(self._forget, name="forget")

        # ── the m3-backed tool functions (ctx.deps is an M3Deps) ──────────────

        @staticmethod
        def _remember(
            ctx: "RunContext[M3Deps]", content: str, importance: float = 0.5
        ) -> str:
            """Save a fact to long-term memory so it can be recalled later.

            Args:
                content: The fact to remember, in natural language.
                importance: 0.0–1.0 salience; higher is retained/ranked more strongly.
            """
            new_id = _deps(ctx).remember(content, importance=importance)
            return f"Remembered (id={new_id})." if new_id else "Nothing to remember."

        @staticmethod
        def _recall(
            ctx: "RunContext[M3Deps]", query: str, limit: int = 5
        ) -> list[dict]:
            """Search long-term memory for facts relevant to ``query`` (best first)."""
            rows = _deps(ctx).recall(query, k=limit)
            return mapping.recall_hits_to_dicts(rows)

        @staticmethod
        def _forget(ctx: "RunContext[M3Deps]", memory_id: str) -> str:
            """Delete a memory by its id (from a prior ``recall`` result)."""
            ok = _deps(ctx).forget(memory_id)
            return "Forgotten." if ok else "No such memory."

    _CLASS_CACHE["M3MemoryToolset"] = M3MemoryToolset
    return M3MemoryToolset


def _deps(ctx: Any) -> M3Deps:
    """Read the M3Deps off a RunContext, failing loud (§3) if the agent wasn't
    wired with ``deps_type=M3Deps`` / ``deps=M3Deps(...)``."""
    deps = getattr(ctx, "deps", None)
    if not isinstance(deps, M3Deps):
        raise TypeError(
            "M3MemoryToolset tools require the agent's deps to be an M3Deps. "
            "Construct the agent with deps_type=M3Deps and pass "
            "deps=M3Deps(user_id=...) to run()."
        )
    return deps


def __getattr__(name: str) -> Any:
    if name == "M3MemoryToolset":
        return _make_m3_memory_toolset()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
