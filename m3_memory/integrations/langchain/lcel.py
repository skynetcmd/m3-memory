"""LCEL-native m3 memory — composable ``Runnable`` write/retrieve + a decorator.

PR-5. ``M3Retriever`` (retriever.py) already IS a ``Runnable`` (a ``BaseRetriever``),
so retrieval drops into an LCEL pipe unchanged. What was missing is the *write*
side — LangChain has no standard "persist this into memory" Runnable — and a
zero-boilerplate way to auto-capture a function's turns. This module adds both,
over the SAME canonical dispatch as the rest of the surface (§12), so behavior
can't drift:

  * ``MemoryWrite``    — a ``Runnable`` that persists its input to m3 and passes
                         it through unchanged, so it composes at the END of a
                         chain: ``retrieve | prompt | llm | MemoryWrite()``.
  * ``MemoryRetrieve`` — thin ``Runnable`` sugar over ``M3Retriever`` for the
                         head of a chain when you want a plain callable, not a
                         configured retriever object.
  * ``with_m3_memory`` — a decorator that captures a wrapped callable's input
                         (and optionally its output) into m3, no body changes.

Tenancy (§7) is resolved through the shared ``mapping.resolve_user_id`` (explicit
> default > ``M3_DEFAULT_USER_ID`` > raise), identical to every other surface —
there is no anonymous mode.
"""

from __future__ import annotations

import functools
from typing import Any, Callable, Optional

from langchain_core.runnables import Runnable, RunnableConfig, RunnableLambda

from . import mapping
from .m3client import M3Client


def _text_of(value: Any) -> str:
    """Best-effort text for an LCEL payload. Strings pass through; a message-like
    object contributes its ``.content``; a dict its ``content``/``text``/``output``;
    everything else is ``str()``-ed. Keeps the write Runnable tolerant of whatever
    the previous chain step emitted (§3 robustness — never crash mid-pipe)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    content = getattr(value, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(value, dict):
        for k in ("content", "text", "output", "input"):
            v = value.get(k)
            if isinstance(v, str):
                return v
    return str(value)


class MemoryWrite(Runnable):
    """Persist each piped value into m3, then return it UNCHANGED (pass-through).

    Placed at the end of an LCEL chain, it records the turn without altering the
    data flowing through::

        chain = retriever | prompt | llm | MemoryWrite(user_id="alex")
        chain.invoke("what did we decide about the API?")   # llm output persisted

    The write is synchronous (read-your-writes: immediately FTS-searchable) and
    also enqueues m3's async fact extraction, exactly like ``Memory.add``.
    """

    def __init__(
        self,
        *,
        user_id: Optional[str] = None,
        mem_type: str = "conversation",
        metadata: Optional[dict] = None,
        agent_id: str = "langchain",
        call_timeout: float = 30.0,
    ):
        self._default_user_id = user_id
        self._mem_type = mem_type
        self._metadata = metadata or {}
        self._client = M3Client(agent_id=agent_id, call_timeout=call_timeout)

    def _require_user(self, user_id: Optional[str]) -> str:
        uid = mapping.resolve_user_id(user_id, self._default_user_id)
        if not uid:
            raise ValueError(
                "MemoryWrite requires a user_id (m3 enforces per-user tenancy). "
                "Pass user_id= to the constructor, or set M3_DEFAULT_USER_ID."
            )
        return uid

    def invoke(
        self, input: Any, config: Optional[RunnableConfig] = None, **kwargs: Any
    ) -> Any:
        text = _text_of(input)
        if text.strip():
            uid = self._require_user((config or {}).get("configurable", {}).get("user_id")
                                     if config else None)
            self._client._tool(
                "memory_write",
                type=self._mem_type,
                content=text,
                user_id=uid,
                scope="user",
                auto_classify=True,
                source="langchain-lcel",
                metadata=self._metadata,
            )
        return input  # pass-through: never mutate the chain's data

    async def ainvoke(
        self, input: Any, config: Optional[RunnableConfig] = None, **kwargs: Any
    ) -> Any:
        # _tool rides the shared loop-thread; the sync body is loop-safe.
        return self.invoke(input, config, **kwargs)


def MemoryRetrieve(*, user_id: Optional[str] = None, k: int = 4, **kwargs: Any) -> Runnable:
    """A ``Runnable`` that maps a query string → list[Document] via ``M3Retriever``.

    Sugar for the head of an LCEL chain when you want a plain callable rather than
    holding a retriever instance::

        chain = MemoryRetrieve(user_id="alex", k=6) | prompt | llm
    """
    from .retriever import M3Retriever

    retriever = M3Retriever(user_id=user_id or "", k=k, **kwargs)
    return RunnableLambda(retriever.invoke).with_config(run_name="MemoryRetrieve")


def with_m3_memory(
    _func: Optional[Callable] = None,
    *,
    user_id: Optional[str] = None,
    mem_type: str = "conversation",
    capture_output: bool = True,
    agent_id: str = "langchain",
) -> Callable:
    """Decorator: auto-persist a callable's input (and, by default, its output).

    Wrap an agent turn or handler so its I/O is captured into m3 with no body
    changes — the "middleware" pattern LangChain users ask for::

        @with_m3_memory(user_id="alex")
        def answer(question: str) -> str:
            return agent.invoke(question)

    Input is written before the call, output after (so a raising body still
    records the question). Tenancy is resolved via ``M3_DEFAULT_USER_ID`` if no
    ``user_id`` is given — and still raises when nothing resolves.
    """
    def _decorate(func: Callable) -> Callable:
        writer = MemoryWrite(user_id=user_id, mem_type=mem_type, agent_id=agent_id)

        @functools.wraps(func)
        def _wrapped(*args: Any, **kwargs: Any) -> Any:
            if args:
                writer.invoke(args[0])
            result = func(*args, **kwargs)
            if capture_output and result is not None:
                writer.invoke(result)
            return result

        return _wrapped

    return _decorate(_func) if callable(_func) else _decorate
