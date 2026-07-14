"""mem0-compatible ``Memory`` / ``MemoryClient`` — the one-line import swap.

The v1 headline (§0a, §7 PR-1). We shadow mem0's EXACT class names and method
signatures so migration changes only the import line:

    # before                                  # after
    from mem0 import Memory                    from m3_memory.langchain import Memory
    m = Memory()                               m = Memory()
    m.add(messages, user_id="alex")            m.add(messages, user_id="alex")
    m.search("diet", user_id="alex")           m.search("diet", user_id="alex")

We mirror mem0's *shape*, never import mem0 (no dependency on it). ``M3Memory`` is
an explicit-name alias; ``MemoryClient`` is the hosted-name shadow (same class —
m3 is always local).

Faithful to the tenets:
  * §7 Privacy — ``user_id`` is MANDATORY and enforced HERE. ``user_id`` is the
    per-tenant isolation key; ``scope`` is a bounded CATEGORY (m3's
    ``VALID_SCOPES = {user, session, agent, org}`` — an out-of-set value silently
    coerces to the shared ``"agent"`` bucket). So we pin ``scope="user"`` and
    isolate on ``user_id`` on every write AND search (they must agree, or a write
    lands where the read never looks). Absent user_id → raise (§3 fail-loud).
  * §0a read-your-writes — ``.add()`` does a SYNCHRONOUS ``memory_write`` (so an
    immediate ``.search()`` finds it) AND enqueues async ``chatlog_write`` for
    m3's deeper Observer/Reflector extraction (the free upgrade).
  * §4 Efficiency — a list ``.add()`` coalesces into ONE ``memory_write_bulk_impl``
    call (direct impl, §11), not N awaits.
  * §3 Robustness — mem0-shaped ``{"results":[...]}`` returns via mapping.py;
    ``from_config`` accepts-and-ignores infra config, never raises (§0a).
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Union

from . import mapping
from .extras import M3ExtrasMixin
from .m3client import M3Client

logger = logging.getLogger("m3_memory.langchain")

# mem0's default text field per message dict.
_MSG_CONTENT_KEYS = ("content", "text")


def _normalize_messages(messages: Union[str, list, dict]) -> list[dict]:
    """Coerce mem0's flexible ``.add()`` input into a list of role/content dicts.

    mem0 accepts a bare string, a single message dict, or a list of message
    dicts (``[{"role": "user", "content": "..."}]``). We normalize all three.
    """
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]
    if isinstance(messages, dict):
        return [messages]
    if isinstance(messages, list):
        out = []
        for m in messages:
            if isinstance(m, str):
                out.append({"role": "user", "content": m})
            elif isinstance(m, dict):
                out.append(m)
        return out
    return []


def _msg_text(msg: dict) -> str:
    for k in _MSG_CONTENT_KEYS:
        v = msg.get(k)
        if v:
            return str(v)
    return ""


class Memory(M3ExtrasMixin):
    """mem0-compatible local memory backed by m3.

    Construct with an optional default ``user_id`` (mem0 lets you pass it per
    call too). ``conversation_id`` defaults to ``user_id`` when unset (§2.2
    conversation-id mapping). ``provider``/``model_id`` fill the chatlog's
    required fields (§0a).
    """

    def __init__(
        self,
        user_id: Optional[str] = None,
        *,
        agent_id: str = "langchain",
        conversation_id: Optional[str] = None,
        provider: str = "unknown",
        model_id: str = "unknown",
        host_agent: str = "langchain",
        call_timeout: float = 30.0,
        **_ignored: Any,
    ):
        self._default_user_id = user_id
        self._agent_id = agent_id
        self._default_conversation_id = conversation_id
        self._provider = provider
        self._model_id = model_id
        self._host_agent = host_agent
        self._client = M3Client(agent_id=agent_id, call_timeout=call_timeout)
        if _ignored:
            logger.info(
                "m3 Memory: ignored unsupported constructor keys %s "
                "(m3 self-configures its embedder + store).",
                sorted(_ignored),
            )

    # ── mem0 config shim (accept-and-ignore, §0a) ─────────────────────────────
    @classmethod
    def from_config(cls, config: Optional[dict] = None) -> "Memory":
        """Accept a mem0 config dict and IGNORE the infra keys (§0a).

        m3 self-configures (in-process Rust embedder, no vector DB to provision),
        so ``embedder``/``vector_store``/``llm`` blocks are harmless no-ops — we
        log one INFO line naming what m3 overrode and never raise. Only genuine
        semantic conflicts would warrant a warning; the default path is silent
        success so an existing mem0 config just works.
        """
        config = config or {}
        overridden = [k for k in ("embedder", "vector_store", "llm", "graph_store")
                      if k in config]
        if overridden:
            logger.info(
                "m3 Memory.from_config: using m3's in-process embedder + hybrid "
                "store; ignored mem0 infra config %s.", overridden,
            )
        # Pass through only the keys we understand as construction defaults.
        known = {k: config[k] for k in
                 ("user_id", "agent_id", "conversation_id", "provider", "model_id")
                 if k in config}
        return cls(**known)

    # ── tenancy (§7 privacy — enforced HERE, the tools don't) ─────────────────
    def _require_user(self, user_id: Optional[str]) -> str:
        uid = user_id or self._default_user_id
        if not uid:
            raise ValueError(
                "user_id is required (m3 enforces per-user tenancy — there is no "
                "anonymous/global mode). Pass user_id= to the constructor or the "
                "method call."
            )
        return uid

    def _conversation_id(self, uid: str, override: Optional[str] = None) -> str:
        return override or self._default_conversation_id or uid

    # ── .add() — read-your-writes + async extraction (§0a) ────────────────────
    def add(
        self,
        messages: Union[str, list, dict],
        *,
        user_id: Optional[str] = None,
        metadata: Optional[dict] = None,
        conversation_id: Optional[str] = None,
        extract: bool = True,
        **_ignored: Any,
    ) -> dict:
        """Store turns and return what was stored (mem0 contract).

        Synchronously ``memory_write``s each turn (immediately searchable —
        read-your-writes) and, unless ``extract=False``, enqueues an async
        ``chatlog_write`` for m3's Observer/Reflector deep extraction. A list of
        turns coalesces into ONE ``memory_write_bulk_impl`` call (§4/§11).

        Returns ``{"results": [{"id","memory","metadata"}, ...]}`` — mem0 shape.
        """
        uid = self._require_user(user_id)
        conv = self._conversation_id(uid, conversation_id)
        msgs = _normalize_messages(messages)
        if not msgs:
            return {"results": []}

        results: list[dict] = []
        if len(msgs) == 1:
            text = _msg_text(msgs[0])
            raw = self._client._tool(
                "memory_write",
                type="conversation",
                content=text,
                user_id=uid,
                scope="user",
                auto_classify=True,
                source="langchain",
                metadata=metadata or {},
            )
            new_id = mapping.parse_written_id(raw)  # "Created: <uuid>" -> uuid
            results.append({"id": new_id, "memory": text, "metadata": metadata or {}})
        else:
            # Coalesce into ONE bulk write (direct impl, check_contradictions=True
            # so batched writes get the same supersession as single writes — §11).
            items = [
                {
                    "type": "conversation",
                    "content": _msg_text(m),
                    "user_id": uid,
                    "scope": "user",         # user_id is the tenancy key; scope
                                             # is a bounded category {user,session,
                                             # agent,org} — anything else coerces
                                             # to "agent" (verified §2.1)
                    "auto_classify": True,
                    "source": "langchain",
                    "metadata": metadata or {},
                }
                for m in msgs
            ]
            ids = self._bulk_write(items)
            for m, mid in zip(msgs, ids):
                text = _msg_text(m)
                results.append({"id": mid, "memory": text, "metadata": metadata or {}})

        if extract:
            self._enqueue_chatlog(msgs, uid, conv)
        return {"results": results}

    def _bulk_write(self, items: list[dict]) -> list[str]:
        """Direct in-process ``memory_write_bulk_impl`` (§11) with
        ``check_contradictions=True`` — bulk speed WITH normal supersession."""
        from memory_core import memory_write_bulk_impl

        return self._client._call_impl(
            memory_write_bulk_impl, items, check_contradictions=True,
        ) or []

    def _enqueue_chatlog(self, msgs: list[dict], uid: str, conv: str) -> None:
        """Fire async Observer extraction (best-effort — never fails ``.add()``).

        chatlog impls self-resolve the chatlog DB (they read
        ``chatlog_config.chatlog_db_path()`` internally), so no
        ``active_database`` wrap is needed on this path (§2.5).
        """
        for i, m in enumerate(msgs):
            try:
                self._client._tool(
                    "chatlog_write",
                    content=_msg_text(m),
                    role=m.get("role", "user"),
                    conversation_id=conv,
                    host_agent=self._host_agent,
                    provider=self._provider,
                    model_id=self._model_id,
                    user_id=uid,
                    turn_index=i,
                )
            except Exception as e:  # extraction is a bonus, not the contract
                logger.debug("chatlog_write enqueue skipped: %s", e)

    # ── .search() — mem0-shaped, temporal-aware ───────────────────────────────
    def search(
        self,
        query: str,
        *,
        user_id: Optional[str] = None,
        limit: int = 10,
        as_of: str = "",
        recency_bias: float = 0.0,
        filters: Optional[dict] = None,
        **_ignored: Any,
    ) -> dict:
        """Hybrid recall → mem0 ``{"results":[{"id","memory","score","metadata"}]}``.

        ``as_of`` enables m3's native bitemporal time-travel; ``recency_bias``
        tilts toward recent facts (both are m3-native extras a plain mem0 user
        silently benefits from — §0b). ``filters`` maps to m3's ``type_filter``.
        """
        uid = self._require_user(user_id)
        type_filter = ""
        if filters and isinstance(filters, dict):
            type_filter = str(filters.get("type", "") or "")
        rows = self._search_impl(
            query=query, user_id=uid, scope="user", k=limit,
            as_of=as_of, recency_bias=recency_bias, type_filter=type_filter,
        )
        return mapping.to_mem0_results(rows)

    def _search_impl(self, **kwargs: Any) -> list:
        """Direct ``memory_search_scored_impl`` call (§2.4, §11).

        Routed through the impl — NOT ``execute_tool_structured`` — because
        ``extra_columns`` is an impl-only param (not on the MCP schema, so the
        dispatcher filters it out; verified 2026-07-14). Direct call is m3's own
        124:1 in-process norm AND the only way to surface the temporal/confidence
        columns (``confidence``/``valid_from``/``valid_to``/``metadata_json``)
        that a plain mem0 ``.search()`` user silently benefits from (§0b).
        Returns ``list[(score, item_dict)]``.
        """
        from memory_core import memory_search_scored_impl

        kwargs.setdefault("extra_columns", mapping.EXTRA_COLUMNS)
        return self._client._call_impl(memory_search_scored_impl, **kwargs) or []

    def get_all(self, *, user_id: Optional[str] = None, limit: int = 100,
                **_ignored: Any) -> dict:
        """All memories for a user → mem0 shape, deterministic + newest-first.

        Uses a direct listing (NOT empty-query semantic search) so it returns
        just-written rows even before their embeddings backfill (§3 robustness).
        """
        uid = self._require_user(user_id)
        items = self._client.list_by_user(uid, scope="user", limit=limit)
        # Listing has no relevance score — mem0's get_all results carry no
        # meaningful score either; surface 0.0.
        return mapping.to_mem0_results([(0.0, it) for it in items])

    def get(self, memory_id: str, **_ignored: Any) -> Optional[dict]:
        """Fetch one memory by id → mem0-shaped dict, or None if absent."""
        raw = self._client._tool("memory_get", id=memory_id)
        item = mapping.parse_get(raw)
        if item is None:
            return None
        return mapping.to_mem0_result(item.get("bm25_score", 0.0), item)

    def delete(self, memory_id: str, **_ignored: Any) -> dict:
        """Delete one memory by id. Ungated (typed method, §2.1b)."""
        self._client._tool("memory_delete", id=memory_id)
        return {"message": f"Memory {memory_id} deleted successfully."}

    def delete_all(self, *, user_id: Optional[str] = None, **_ignored: Any) -> dict:
        """Delete all memories for a user (two-step: fetch ids → bulk delete)."""
        uid = self._require_user(user_id)
        items = self._client.list_by_user(uid, scope="user", limit=100_000)
        ids = [it.get("id") for it in items if it.get("id")]
        if ids:
            self._client._tool("memory_delete_bulk", ids=ids)
        return {"message": f"Deleted {len(ids)} memories for user {uid}."}

    # ── escape hatch (§9) ─────────────────────────────────────────────────────
    def call(self, tool: str, **args: Any) -> Any:
        """Passthrough to ANY m3 catalog tool by name (§9). Gated for destructive
        tools exactly as the MCP surface is."""
        return self._client.call(tool, **args)


# mem0's hosted client name → same local class (m3 is always local).
MemoryClient = Memory
# Explicit-name alias for users who prefer it over the mem0 shadow name.
M3Memory = Memory
