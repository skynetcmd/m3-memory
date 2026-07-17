"""Synchronous facade over m3-memory's in-process dispatch.

Bridges LangChain / LangGraph code (synchronous ``Memory``/``M3Store.batch`` and
async ``abatch``) to m3-memory's async catalog dispatch. Returns structured rows,
never formatted strings (m3 DESIGN_PHILOSOPHIES §3 robustness); rides the one
canonical dispatch path so behavior cannot drift (§12a tool-shape); runs
in-process — no HTTP/proxy hop (§4 efficiency).

Unlike the Hermes facade, this adapter IS the installed memory module
(``pip install m3-memory[langchain]``): it runs in m3's own import environment,
so it needs no path-*priority* shim to evict a shadowing ``memory`` package.
It DOES still put m3's ``bin/`` on ``sys.path`` — every m3 entry point does this
explicitly (there is no ``.pth`` magic), via ``installer.bin_dir()`` which honors
``$M3_PATH_BIN`` and the wheel/dev-checkout layout.

Performance (§8): ONE persistent event loop on ONE dedicated thread, shared
process-wide — NOT ``asyncio.run()`` per call. m3's search path uses a connection
pool and an embedder semaphore that are affinity-bound to the loop that created
them; a fresh loop per call breaks that affinity AND pays loop-setup cost on the
hot path, blowing the P50<5ms budget. Copied verbatim from the Hermes facade
(its hard-won lesson).

THREE dispatch seams, on purpose (§2.1b, §9, §11):
  * ``_tool(name, **args)`` → ``execute_tool_structured`` — the typed-method path.
    Resolves the ToolSpec and dispatches with OUR ``agent_id`` stamped, returning
    the tool's native result (dict / list / str). Typed destructive methods
    (``.delete``/``.forget``) ride this and are UNGATED by design — the gate
    guards the LLM-facing MCP surface, not a user's own explicit API call.
  * ``call(name, **args)`` → ``_dispatch_one`` — the ``.call()`` passthrough.
    Adds the unknown-tool guard + destructive gate + structured ``{"ok":...}``
    envelope. Uses ``agent_id=""`` (the dispatcher hardcodes it), correct for a
    raw escape hatch.
  * ``_call_impl(coro_fn, ...)`` → a direct in-process impl call (m3's own
    124:1-dominant pattern) for the bench-only ``memory_write_bulk_impl``, which
    is deliberately NOT an MCP tool and has no ToolSpec.

All three ride the SAME shared loop-thread, so pool + embedder affinity is
preserved regardless of seam (§8).
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future
from contextlib import nullcontext as _nullcontext
from typing import Any, Awaitable, Callable, Coroutine

# NOTE: mcp_tool_catalog / catalog.dispatch / memory_core are imported LAZILY
# inside the call methods, NOT at module top. They pull a heavy dependency chain
# (memory_core + embedder), and a slow module-level import races under threaded
# tool execution: a second thread can observe this module half-initialized in
# sys.modules. Deferring the import lets the class bind instantly.

_BIN_ON_PATH = False
_BIN_LOCK = threading.Lock()


def _ensure_bin_on_path() -> None:
    """Put m3's ``bin/`` on ``sys.path`` so ``mcp_tool_catalog`` + siblings
    (``memory_core``, ``catalog.dispatch``, ``chatlog_core``, …) import.

    There is no ``.pth`` magic in m3 — every entry point does this explicitly.
    We resolve ``bin/`` via ``installer.bin_dir()`` (honors ``$M3_PATH_BIN``,
    the wheel-packaged location, then a dev-checkout sibling), falling back to
    the checkout-relative ``../../bin`` if the installer resolver yields nothing.
    Idempotent; cheap after the first call. We do NOT reorder sys.path or evict
    a shadowing ``memory`` package — as the installed memory module we own the
    import environment (§2.5). A user's own top-level ``memory.py`` ahead of us
    on the path is a documented, user-caused edge, not something a shim fixes.
    """
    global _BIN_ON_PATH
    if _BIN_ON_PATH:
        return
    with _BIN_LOCK:
        if _BIN_ON_PATH:
            return
        import os
        import sys

        bin_dir = None
        try:
            from m3_memory import installer

            _bd = installer.bin_dir()
            if _bd is not None:
                bin_dir = str(_bd)
        except Exception:
            bin_dir = None
        if bin_dir is None:
            # Dev-checkout fallback: this file is
            # m3_memory/integrations/langchain/m3client.py → repo/bin.
            here = os.path.dirname(os.path.abspath(__file__))
            cand = os.path.abspath(os.path.join(here, "..", "..", "..", "bin"))
            if os.path.isfile(os.path.join(cand, "mcp_tool_catalog.py")):
                bin_dir = cand
        if bin_dir and bin_dir not in sys.path:
            sys.path.insert(0, bin_dir)
        _BIN_ON_PATH = True


class M3Client:
    """Sync wrapper over m3's in-process dispatch.

    One event-loop thread is shared process-wide across all M3Client instances
    (the loop, not the client, is the scarce resource). ``agent_id`` is stamped
    on inject_agent_id tools (memory_write/chatlog_write) non-bypassably by the
    dispatcher; read tools ignore it.
    """

    _loop: asyncio.AbstractEventLoop | None = None
    _thread: threading.Thread | None = None
    _lock = threading.Lock()

    def __init__(self, agent_id: str = "langchain", call_timeout: float = 30.0):
        self._agent_id = agent_id
        self._timeout = call_timeout
        _ensure_bin_on_path()
        self._ensure_loop()

    # ── the shared loop ───────────────────────────────────────────────────────
    @classmethod
    def _ensure_loop(cls) -> None:
        """Start the shared loop thread once, process-wide. Idempotent."""
        with cls._lock:
            if cls._loop is not None and cls._loop.is_running():
                return
            loop = asyncio.new_event_loop()
            t = threading.Thread(
                target=loop.run_forever, name="m3client-langchain-loop", daemon=True
            )
            t.start()
            cls._loop, cls._thread = loop, t

    def _run(self, coro: Awaitable[Any]) -> Any:
        """Run a coroutine on the shared loop from a sync caller and block."""
        fut: Future = asyncio.run_coroutine_threadsafe(
            coro, self._loop  # type: ignore[arg-type]
        )
        return fut.result(timeout=self._timeout)

    async def _run_async(self, coro_factory: "Callable[[], Coroutine[Any, Any, Any]]") -> Any:
        """Run work on the shared loop-thread from an ASYNC caller (e.g. a
        LangGraph ``abatch`` awaited on the caller's own loop), then await the
        result without blocking the caller's loop.

        This is the §8 guarantee for the async path: m3's SQLite pool checks
        connections in/out on ONE thread and the embedder/HTTP client is loop-
        affinity-bound, so ALL m3 work — sync or async caller — must land on the
        single m3client loop-thread. We schedule ``coro_factory()`` there via
        ``run_coroutine_threadsafe`` (the coro must be CREATED on that loop, hence
        a factory, not a coroutine object) and bridge its ``concurrent.futures``
        Future to the caller's loop with ``asyncio.wrap_future``.

        ``coro_factory`` must build the coroutine with NO already-bound loop
        state; it runs entirely on the m3client loop.
        """
        loop = self._loop
        assert loop is not None
        # run_coroutine_threadsafe is threadsafe; schedule on the loop-thread and
        # bridge its concurrent.futures.Future back to the caller's loop.
        cfut = asyncio.run_coroutine_threadsafe(coro_factory(), loop)
        return await asyncio.wrap_future(cfut)

    # ── seam 1: typed-method dispatch (our agent_id, ungated) ─────────────────
    async def _tool_async(self, name: str, args: dict) -> Any:
        """name→spec→``execute_tool_structured`` with OUR agent_id stamped.

        The typed-method path (``.add``/``.search``/``.delete``/…). Returns the
        tool's native result (dict/list/str), NOT the ``.call()`` envelope. No
        destructive gate here by design (§2.1b): the user invoked the method
        explicitly; the gate exists to stop an LLM surprise-deleting over MCP.
        Raises on unknown tool (a programming error in the adapter, not user
        input) — fail loud (§3).
        """
        _ensure_bin_on_path()
        import mcp_tool_catalog as cat  # lazy — see module-top note

        spec = cat.get_tool(name)
        if spec is None:
            raise ValueError(f"unknown m3 tool: {name}")
        # Hand each call its own args dict — execute_tool_structured mutates it
        # in place (pops 'database', filters keys).
        return await cat.execute_tool_structured(spec, dict(args), self._agent_id)

    def _tool(self, name: str, **args: Any) -> Any:
        """Sync bridge over :meth:`_tool_async`."""
        return self._run(self._tool_async(name, args))

    async def _tool_on_db_async(self, db_path: str, name: str, args: dict) -> Any:
        """Like :meth:`_tool_async` but pins the active DB for the call (§2.5 N2).

        Chatlog rows live in a DIFFERENT DB than core in *separate*/hybrid
        topology. A CORE tool (e.g. ``memory_delete_bulk``) operating on chatlog
        rows must run under ``active_database(chatlog_path)`` or it silently hits
        the wrong DB (the row id isn't in main → no-op). ``execute_tool_structured``
        does not set this for us on the direct path, so we wrap it. Empty
        ``db_path`` → default resolution (no-op wrap)."""
        _ensure_bin_on_path()
        from m3_core.paths import active_database

        ctx = active_database(db_path) if db_path else _nullcontext()
        with ctx:
            return await self._tool_async(name, args)

    def _tool_on_db(self, db_path: str, name: str, **args: Any) -> Any:
        """Sync bridge over :meth:`_tool_on_db_async`."""
        return self._run(self._tool_on_db_async(db_path, name, args))

    async def _delete_chatlog_rows_async(self, ids: list) -> int:
        """Soft-delete chatlog rows by id ON the chatlog connection (§2.5 N2).

        The core ``memory_delete_bulk`` resolves its DB via
        ``resolve_db_path`` = explicit > ``M3_DATABASE`` env > active_database
        ContextVar > default — so when ``M3_DATABASE`` is set (real deployments,
        the test fixture), ``active_database(chatlog_path)`` CANNOT redirect it
        and the delete misses chatlog rows entirely. We therefore delete via the
        chatlog CONNECTION directly (``M3Context.get_chatlog_conn()``, the exact
        route chatlog writes/reads use — topology-correct for integrated/
        separate/hybrid), with parameterized SQL (§6). Soft-delete (is_deleted=1)
        matches m3's default delete semantics. Returns rows affected."""
        if not ids:
            return 0
        _ensure_bin_on_path()
        import chatlog_config
        from m3_sdk import M3Context, active_database

        chatlog_path = chatlog_config.chatlog_db_path()
        placeholders = ",".join(["?"] * len(ids))
        n = 0
        # Activate the chatlog path so get_chatlog_conn() routes to its pool
        # (the exact pattern chatlog_write_bulk uses, chatlog_core.py:341).
        with active_database(chatlog_path):
            ctx = M3Context.for_db(None)
            with ctx.get_chatlog_conn() as conn:
                cur = conn.execute(
                    f"UPDATE memory_items SET is_deleted = 1 "
                    f"WHERE id IN ({placeholders})",
                    list(ids),
                )
                n = cur.rowcount
                conn.commit()
        return n

    def _delete_chatlog_rows(self, ids: list) -> int:
        """Sync bridge over :meth:`_delete_chatlog_rows_async`."""
        return self._run(self._delete_chatlog_rows_async(ids))

    # ── seam 2: canonical passthrough (.call escape hatch, gated) ─────────────
    async def _dispatch(self, name: str, args: dict) -> Any:
        """name→spec→dispatch via ``_dispatch_one`` — inherits the unknown-tool
        guard + destructive gate. Structured ``{"ok":...}`` envelope out."""
        _ensure_bin_on_path()
        from catalog.dispatch import _dispatch_one  # lazy — see module-top note

        # Hand each call its own args dict — dispatch mutates it in place.
        # _dispatch_one hardcodes agent_id="" (raw escape hatch, correct).
        return await _dispatch_one(name, dict(args), dry_run=False)

    def call(self, name: str, **args: Any) -> Any:
        """Sync passthrough to ANY catalog tool by name (§9 escape hatch).

        Returns the structured ``{"ok": ..., "tool": ..., "result": ...}``
        envelope (or ``{"ok": False, "error": "unknown_tool"|"destructive_gated",
        ...}``). Destructive tools are gated here exactly as on the MCP surface —
        the gate is inherited, not re-implemented, so ``.call()`` can't become a
        privilege-escalation hole. Contrast the typed methods (seam 1), which are
        ungated because the user invoked them explicitly.
        """
        return self._run(self._dispatch(name, args))

    # ── self-heal: ensure a DB's schema exists (§4 "just works") ──────────────
    _schema_healed: set = set()

    def ensure_schema(self, db_path: str = "") -> None:
        """Make sure the target DB has m3's schema, migrating it if absent.

        Mirrors the main-DB lazy-migration (`_lazy_init` on first `_db()` touch)
        for a DB the caller names — the chatlog DB in *separate* topology, which
        otherwise has no schema until `m3` install/doctor runs. Touching the path
        under `active_database` triggers `_lazy_init`, creating `memory_items`
        (chatlog rows ARE memory_items). Idempotent + cached per path so it's a
        one-time cost. Empty/None path → the default (main) DB.
        """
        key = db_path or "<default>"
        if key in self._schema_healed:
            return
        self._run(self._ensure_schema_async(db_path))
        self._schema_healed.add(key)

    async def _ensure_schema_async(self, db_path: str) -> None:
        _ensure_bin_on_path()
        from m3_core.paths import active_database
        from memory.db import _db

        ctx = active_database(db_path) if db_path else _nullcontext()
        with ctx:
            with _db():
                pass  # opening under active_database triggers _lazy_init/migrate

    # ── deterministic per-user listing (no embedding dependency) ──────────────
    async def _list_by_user_async(
        self, user_id: str, *, scope: str = "user", limit: int = 1000,
    ) -> list[dict]:
        """List a user's live memories deterministically, newest-first.

        ``get_all``/``delete_all`` need a COMPLETE, deterministic listing — NOT
        semantic ranking. An empty-query ``memory_search_scored`` depends on
        vector similarity, which is async-deferred on a fresh write (the write
        returns "embedding deferred — backfills async"), so it can miss
        just-written rows until the sweeper runs (§3 robustness: behavior must be
        deterministic). This reads ``memory_items`` directly via m3's own pooled
        ``_db()`` (parameterized SQL, §6; honors the active DB path), filtering
        the SAME ``user_id``+``scope`` the write set. Returns item dicts shaped
        like ``memory_search_scored`` rows (so mapping.py handles them unchanged).
        """
        _ensure_bin_on_path()
        from memory.db import _db  # canonical pooled connection

        cols = ("id, content, title, type, importance, confidence, "
                "valid_from, valid_to, metadata_json")
        rows: list[dict] = []
        with _db() as db:
            cur = db.execute(
                f"SELECT {cols} FROM memory_items "
                "WHERE user_id = ? AND scope = ? "
                "AND (is_deleted IS NULL OR is_deleted = 0) "
                "ORDER BY created_at DESC LIMIT ?",
                (user_id, scope, int(limit)),
            )
            for r in cur.fetchall():
                rows.append(dict(r))
        return rows

    def list_by_user(self, user_id: str, *, scope: str = "user",
                     limit: int = 1000) -> list[dict]:
        """Sync bridge over :meth:`_list_by_user_async`."""
        return self._run(self._list_by_user_async(user_id, scope=scope, limit=limit))

    # ── seam 3: direct impl call (bench-only bulk write; §11) ─────────────────
    def _call_impl(self, fn: Callable[..., Awaitable[Any]], *fn_args: Any, **fn_kwargs: Any) -> Any:
        """Call an m3 impl coroutine DIRECTLY on the shared loop.

        m3's own normal in-process pattern (124:1 vs the MCP-boundary
        dispatcher). Used for ``memory_write_bulk_impl``, which is deliberately
        NOT an MCP tool (bench-only impl, §11) and so has no ToolSpec to resolve.
        The caller owns arg validation + tenancy (the impl stays permissive).
        """
        return self._run(fn(*fn_args, **fn_kwargs))
