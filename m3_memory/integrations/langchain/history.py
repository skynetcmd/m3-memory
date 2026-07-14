"""``M3ChatMessageHistory(BaseChatMessageHistory)`` + ``with_m3_history`` — short-term.

PR-3. The short-term / message-history drop-in for chatbot builders. Backed by
m3's ``chatlog_*`` family (§2.2c decision): unlike a bare conversation insert,
``chatlog_write`` fires m3's async Observer/Reflector extraction — so a chatbot's
raw turns are ALSO distilled into long-term memory for free (the §0b "chat →
long-term memory" differentiator).

``BaseChatMessageHistory``'s only abstract method is ``clear``; we also provide
``messages`` (the read) and override ``add_messages``/``aget_messages`` for
efficiency. All three map to m3 as multi-step adapter shims over EXISTING tools
(§2.2), never a new core tool:

  * ``add_messages`` → ``chatlog_write`` per message (bulk when >1)
  * ``messages`` → ``chatlog_search(query="", conversation_id=…)`` (empty query
    returns the recent window, ``created_at`` DESC) → REVERSED to chronological
  * ``clear`` → search ids → ``memory_delete_bulk`` (two-step, hidden)

Tenancy/topology (§2.1c, §2.5): chatlog impls self-resolve the chatlog DB (they
read ``chatlog_config.chatlog_db_path()`` internally), so the write/read paths
need no ``active_database`` wrap; only the ``clear()`` delete over chatlog rows
(which live in the chatlog DB) does — handled below.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional, Sequence

from langchain_core.chat_history import BaseChatMessageHistory
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from . import mapping
from .m3client import M3Client

if TYPE_CHECKING:
    from langchain_core.runnables import Runnable
    from langchain_core.runnables.history import RunnableWithMessageHistory

logger = logging.getLogger("m3_memory.langchain")

# LangChain message type ⇄ m3 chatlog role (m3 VALID_ROLES = user/assistant/system/tool).
_ROLE_BY_TYPE = {
    "human": "user",
    "ai": "assistant",
    "system": "system",
    "tool": "tool",
}
_MSG_BY_ROLE = {
    "user": HumanMessage,
    "assistant": AIMessage,
    "system": SystemMessage,
}


def _msg_to_role(msg: BaseMessage) -> str:
    return _ROLE_BY_TYPE.get(msg.type, "user")


def _row_to_message(row: dict) -> BaseMessage:
    # chatlog_search surfaces role inside the row's `metadata` dict, NOT as a
    # top-level column (verified 2026-07-14) — the returned columns are
    # content/conversation_id/created_at/id/metadata/model_id/title.
    md = row.get("metadata")
    if not isinstance(md, dict):
        md = mapping._loads_metadata(row.get("metadata_json"))
    role = md.get("role") or row.get("role") or "user"
    content = row.get("content", "")
    if role == "tool":
        # ToolMessage requires a tool_call_id; synthesize from metadata or a stub.
        return ToolMessage(content=content,
                           tool_call_id=str(row.get("id", "") or "unknown"))
    cls = _MSG_BY_ROLE.get(role, HumanMessage)
    return cls(content=content)


class M3ChatMessageHistory(BaseChatMessageHistory):
    """Message history for one conversation, backed by m3's chatlog.

    ``session_id`` (RunnableWithMessageHistory) / ``thread_id`` (LangGraph) both
    map verbatim to m3's ``conversation_id`` (§2.2). ``user_id`` is a DIFFERENT
    axis (tenancy) and is carried through for provenance.
    """

    def __init__(
        self,
        conversation_id: str,
        *,
        user_id: str = "",
        agent_id: str = "langchain",
        provider: str = "other",
        model_id: str = "unknown",
        host_agent: str = "langchain",
        window: int = 100,
        call_timeout: float = 30.0,
    ):
        if not conversation_id:
            raise ValueError("conversation_id (session_id/thread_id) is required.")
        self.conversation_id = conversation_id
        self._user_id = user_id
        self._agent_id = agent_id
        self._provider = provider
        self._model_id = model_id
        self._host_agent = host_agent
        self._window = window
        self._client = M3Client(agent_id=agent_id, call_timeout=call_timeout)
        self._ensure_chatlog_schema()

    def _ensure_chatlog_schema(self) -> None:
        """Self-heal the chatlog DB schema (§4 "just works"): in *separate*
        topology the chatlog DB has no schema until m3 install/doctor runs, so a
        first ``chatlog_write`` would spill with "no such table: memory_items".
        Resolve the chatlog path and ensure it's migrated (one-time, cached)."""
        try:
            from m3_memory import installer  # noqa: F401  (bin already on path)
        except Exception:
            pass
        try:
            import chatlog_config
            self._client.ensure_schema(chatlog_config.chatlog_db_path())
        except Exception as e:  # never block construction on a heal attempt
            import logging
            logging.getLogger("m3_memory.langchain").debug(
                "chatlog schema self-heal skipped: %s", e)

    # ── read: messages (chronological) ────────────────────────────────────────
    def _fetch_rows(self) -> list[dict]:
        """Fetch this conversation's rows, sorted oldest→newest.

        ``chatlog_search`` order is NOT stable across topologies (verified
        2026-07-14: unified returns ASC, separate returns ``created_at`` DESC),
        and ``created_at`` is only second-granular, so we sort on the
        per-conversation ``turn_index`` we persist (round-trips in each row's
        ``metadata``). Rows without a turn_index sort last, by created_at.
        """
        raw = self._client._tool(
            "chatlog_search", query="", conversation_id=self.conversation_id,
            k=self._window,
        )
        rows = mapping.parse_chatlog_search(raw)  # JSON string → list[dict]

        def _key(r: dict):
            md = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
            ti = md.get("turn_index")
            # (has_index, turn_index_or_0, created_at) — indexed rows first, in
            # order; unindexed rows fall back to created_at, appended after.
            return (0, int(ti), "") if ti is not None else (1, 0, r.get("created_at", ""))

        return sorted(rows, key=_key)

    @property
    def messages(self) -> list[BaseMessage]:  # type: ignore[override]
        """The recent window, oldest→newest (LangChain message-history order).

        A read-only property is the idiomatic ``BaseChatMessageHistory``
        implementation (the base types it as a writeable attr; subclasses back it
        with storage). Reads flow through :meth:`_fetch_rows`."""
        return [_row_to_message(r) for r in self._fetch_rows()]

    async def aget_messages(self) -> list[BaseMessage]:
        # The sync path already rides the loop-thread; reuse it.
        return self.messages

    # ── write: add_messages (bulk) ────────────────────────────────────────────
    def add_messages(self, messages: Sequence[BaseMessage]) -> None:
        """Append turns via chatlog_write (fires async Observer extraction).

        ``turn_index`` continues from the conversation's current length so it
        stays a monotonic per-conversation counter ACROSS separate
        ``add_messages`` calls — the stable sort key ``messages`` reads back.
        """
        base = self._next_turn_index()
        for i, msg in enumerate(messages):
            self._client._tool(
                "chatlog_write",
                content=str(msg.content),
                role=_msg_to_role(msg),
                conversation_id=self.conversation_id,
                host_agent=self._host_agent,
                provider=self._provider,
                model_id=self._model_id,
                user_id=self._user_id,
                turn_index=base + i,
            )

    def _next_turn_index(self) -> int:
        """Highest existing turn_index in this conversation + 1 (0 if empty)."""
        rows = self._fetch_rows()
        max_ti = -1
        for r in rows:
            md = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
            ti = md.get("turn_index")
            if ti is not None and int(ti) > max_ti:
                max_ti = int(ti)
        return max_ti + 1

    def add_message(self, message: BaseMessage) -> None:
        self.add_messages([message])

    # ── clear: two-step (search ids → bulk delete), hidden behind one call ────
    def clear(self) -> None:
        """Delete every turn in this conversation (§2.2b two-step shim).

        chatlog rows ARE memory_items, but in the CHATLOG DB (separate/hybrid
        topology). ``memory_delete_bulk`` is a CORE tool that resolves the main
        DB by default — so we MUST pin it to the chatlog DB (§2.5 N2), or the ids
        (which live in the chatlog DB) aren't found and the delete is a silent
        no-op. Two steps (search ids → bulk delete), hidden behind one call.
        """
        raw = self._client._tool(
            "chatlog_search", query="", conversation_id=self.conversation_id,
            k=mapping.MAX_CLEAR_ROWS,
        )
        rows = mapping.parse_chatlog_search(raw)
        ids = [r.get("id") for r in rows if r.get("id")]
        if ids:
            self._client._delete_chatlog_rows(ids)


def with_m3_history(
    runnable: "Runnable",
    *,
    user_id: str = "",
    provider: str = "other",
    model_id: str = "unknown",
    input_messages_key: Optional[str] = None,
    history_messages_key: Optional[str] = None,
    **history_kwargs,
) -> "RunnableWithMessageHistory":
    """Wrap a runnable so its message history persists to m3 — the one-liner.

    The ``session_id`` in ``config={"configurable": {"session_id": ...}}`` becomes
    m3's ``conversation_id`` per invocation. Example::

        chain = with_m3_history(prompt | model, user_id="alex")
        chain.invoke({"input": "hi"},
                     config={"configurable": {"session_id": "conv-1"}})
    """
    from langchain_core.runnables.history import RunnableWithMessageHistory

    def _factory(session_id: str) -> M3ChatMessageHistory:
        return M3ChatMessageHistory(
            session_id, user_id=user_id, provider=provider, model_id=model_id,
            **history_kwargs,
        )

    kwargs: dict[str, Any] = {}
    if input_messages_key is not None:
        kwargs["input_messages_key"] = input_messages_key
    if history_messages_key is not None:
        kwargs["history_messages_key"] = history_messages_key
    return RunnableWithMessageHistory(runnable, _factory, **kwargs)
