"""Synchronous facade over m3-memory's structured dispatch.

Bridges Hermes Agent's MemoryProvider (synchronous, called from daemon
threads) to m3-memory's async catalog dispatch. Returns structured rows, never
formatted strings (m3 DESIGN_PHILOSOPHIES §3); rides the one canonical dispatch
path so behavior cannot drift (§12); no HTTP/proxy hop (§4).

IMPROVEMENT 3 — ONE persistent event loop on ONE dedicated thread, not
asyncio.run() per call. m3's search path uses a connection pool and an embedder
semaphore that are affinity-bound to the loop that created them; spinning a
fresh loop per call (from the provider's prefetch/sync daemon threads) breaks
that affinity AND pays loop-setup cost on the hot path, blowing the §8
P50<5ms budget. A single long-lived loop keeps pool + semaphore reuse intact.

Requires m3-memory's bin/ on PYTHONPATH (so `import mcp_tool_catalog` resolves).
The Hermes plugin config / launch env must add it, e.g.:
  PYTHONPATH=/path/to/m3-memory/bin
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future
from typing import Any, List

# NOTE: mcp_tool_catalog is imported lazily inside _call(), NOT at module top.
# It pulls a heavy dependency chain (memory_core + embedder), and a slow
# module-level import races under Hermes' threaded tool execution: a second
# thread can observe this module half-initialized in sys.modules, yielding
# "ImportError: cannot import name 'M3Client'". Deferring the import lets the
# class bind instantly, eliminating the race.


class M3Client:
    """Sync wrapper over mcp_tool_catalog.execute_tool_structured.

    One event loop thread is shared process-wide across all M3Client instances
    (the loop, not the client, is the scarce resource). agent_id is stamped on
    inject_agent_id tools (memory_write/chatlog_write) non-bypassably; read
    tools ignore it.
    """

    _loop: asyncio.AbstractEventLoop | None = None
    _thread: threading.Thread | None = None
    _lock = threading.Lock()

    def __init__(self, agent_id: str = "hermes", call_timeout: float = 30.0):
        self._agent_id = agent_id
        self._timeout = call_timeout
        self._ensure_loop()

    @classmethod
    def _ensure_loop(cls) -> None:
        """Start the shared loop thread once, process-wide. Idempotent."""
        with cls._lock:
            if cls._loop is not None and cls._loop.is_running():
                return
            loop = asyncio.new_event_loop()
            t = threading.Thread(
                target=loop.run_forever, name="m3client-loop", daemon=True
            )
            t.start()
            cls._loop, cls._thread = loop, t

    @staticmethod
    def _ensure_m3_path_priority() -> None:
        """Make m3-memory's bin/ win the `memory` package name.

        m3's internals do bare `import memory.chroma` etc. A host like Hermes
        Agent puts its own `plugins/` (containing `plugins/memory/`) on
        sys.path, which shadows m3's top-level `memory` package and breaks the
        import with "No module named 'memory.chroma'". We resolve mcp_tool_catalog's
        own directory (m3's bin/) and move it to the FRONT of sys.path so m3's
        `memory` resolves first. Idempotent; cheap.
        """
        import importlib.util
        import sys
        spec = importlib.util.find_spec("mcp_tool_catalog")
        if spec and spec.origin:
            import os
            bin_dir = os.path.dirname(spec.origin)
            if sys.path and sys.path[0] != bin_dir:
                # Drop any existing occurrence, then prepend.
                sys.path[:] = [p for p in sys.path if p != bin_dir]
                sys.path.insert(0, bin_dir)
            # If a shadowing `memory` was already imported, evict it so the
            # next import re-resolves against m3's bin.
            _m = sys.modules.get("memory")
            if _m is not None:
                _f = getattr(_m, "__file__", "") or ""
                if not _f.startswith(bin_dir):
                    for _name in [n for n in sys.modules
                                  if n == "memory" or n.startswith("memory.")]:
                        del sys.modules[_name]

    def _call(self, name: str, **args) -> Any:
        self._ensure_m3_path_priority()
        import mcp_tool_catalog as cat  # lazy — see module-top note
        spec = cat.get_tool(name)
        if spec is None:
            raise ValueError(f"unknown m3 tool: {name}")
        # Hand each call its own args dict — execute_tool_structured mutates it
        # in place (pops 'database', filters keys).
        fut: Future = asyncio.run_coroutine_threadsafe(
            cat.execute_tool_structured(spec, dict(args), self._agent_id),
            self._loop,  # type: ignore[arg-type]
        )
        return fut.result(timeout=self._timeout)

    # ── provider-facing methods (structured rows, never strings — §3) ─────────

    def search(self, query: str, user_id: str, top_k: int) -> List[dict]:
        """Hybrid recall → [{"content", "score"}]. memory_search_scored returns
        list[(score, item)]; unpack to the shape the provider surfaces."""
        rows = self._call(
            "memory_search_scored",
            query=query,
            user_id=user_id,
            k=top_k,
            scope="user",
        )
        return [{"content": it.get("content", ""), "score": s} for s, it in rows]

    def get_all(self, user_id: str, type: str) -> List[dict]:
        """Stable user facts via the empty-query filter-only path (the spec's
        validator accepts query='')."""
        rows = self._call(
            "memory_search_scored",
            query="",
            user_id=user_id,
            type_filter=type,
            k=200,
            scope="user",
        )
        return [{"content": it.get("content", "")} for _s, it in rows]

    def conclude(self, content: str, user_id: str) -> None:
        """Verbatim fact write (no Observer re-extraction).

        Scope + type MUST match the read paths: search()/get_all() query
        scope="user", and get_all() filters type="user_fact". Writing under a
        different scope/type (m3's default is the "agent" scope) stores the fact
        where recall never looks — so m3_conclude "succeeds" yet
        m3_search/m3_profile return nothing. Keep these aligned.
        """
        self._call(
            "memory_write",
            content=content,
            user_id=user_id,
            type="user_fact",
            scope="user",
        )

    def chatlog_write(
        self,
        user_id: str,
        session_id: str,
        user_content: str,
        assistant_content: str,
    ) -> None:
        """Enqueue a turn; m3's Observer/Reflector extract + supersede async."""
        self._call(
            "chatlog_write",
            user_id=user_id,
            session_id=session_id,
            user=user_content,
            assistant=assistant_content,
        )
