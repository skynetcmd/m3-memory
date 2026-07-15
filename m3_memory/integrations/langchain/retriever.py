"""``M3Retriever(BaseRetriever)`` — the RAG drop-in.

PR-3. A LangChain ``BaseRetriever`` backed by m3's hybrid FTS5+vector+MMR recall.
``BaseRetriever``'s only abstract method is ``_get_relevant_documents(query, *,
run_manager) -> list[Document]``; ``_aget_relevant_documents`` is the async twin.

Each hit → ``Document(page_content, metadata={id, score, type, confidence,
valid_from, valid_to, ...})``. m3's bitemporal + confidence signal rides the
Document metadata so downstream chains can time-travel / filter (§2.3, §0b).

Tenancy (§7): ``user_id`` is REQUIRED (raise if absent) and every search sets
``scope="user"`` — same model as the mem0/store surfaces. Uses the direct-impl
search path (§2.4) so ``extra_columns`` surfaces the temporal fields.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import PrivateAttr

from . import mapping
from .m3client import M3Client

if TYPE_CHECKING:
    from langchain_core.callbacks import (
        AsyncCallbackManagerForRetrieverRun,
        CallbackManagerForRetrieverRun,
    )


class M3Retriever(BaseRetriever):
    """Retrieve m3 memories as LangChain ``Document``s.

    BaseRetriever is a pydantic model; declare config as pydantic fields and keep
    the (non-serializable) M3Client as a PrivateAttr, lazily built.
    """

    user_id: str = ""
    k: int = 4
    scope: str = "user"
    type_filter: str = ""
    as_of: str = ""
    recency_bias: float = 0.0
    agent_id: str = "langchain"
    call_timeout: float = 30.0

    _client: Optional[M3Client] = PrivateAttr(default=None)

    def _get_client(self) -> M3Client:
        if self._client is None:
            self._client = M3Client(agent_id=self.agent_id,
                                    call_timeout=self.call_timeout)
        return self._client

    def _require_user(self) -> str:
        if not self.user_id:
            raise ValueError(
                "M3Retriever requires user_id (m3 enforces per-user tenancy). "
                "Construct as M3Retriever(user_id='...')."
            )
        return self.user_id

    # ── the search → Document mapping ─────────────────────────────────────────
    def _search(self, query: str) -> list[Document]:
        uid = self._require_user()
        from memory_core import memory_search_scored_impl

        rows = self._get_client()._call_impl(
            memory_search_scored_impl,
            query=query, user_id=uid, scope=self.scope, k=self.k,
            type_filter=self.type_filter, as_of=self.as_of,
            recency_bias=self.recency_bias,
            extra_columns=mapping.EXTRA_COLUMNS,
        ) or []
        return [self._to_document(score, item) for score, item in rows]

    @staticmethod
    def _to_document(score: float, item: dict) -> Document:
        md: dict[str, Any] = {
            "id": item.get("id"),
            "score": score,
            "type": item.get("type"),
        }
        for k in ("confidence", "valid_from", "valid_to"):
            v = item.get(k)
            if v is not None and v != "":
                md[k] = v
        # user metadata_json rides through too (lossless round-trip).
        md.update(mapping._loads_metadata(item.get("metadata_json")))
        return Document(page_content=item.get("content", ""), metadata=md)

    # ── observability: honest retrieval explanation (LangSmith-friendly) ──────
    def explain(self, query: str) -> dict:
        """Return the REAL signal behind a retrieval, for tracing / debugging.

        m3's search returns a single blended relevance ``score`` per hit (hybrid
        FTS5 + vector + MMR + optional recency), not separated per-component
        sub-scores — so this reports what m3 actually computes, never invented
        component numbers. The shape is a plain dict you can attach to a LangSmith
        run (``config={"metadata": {"m3_retrieval": r.explain(q)}}``) or log::

            {"query", "config": {k, type_filter, as_of, recency_bias, scope},
             "results": [{"id", "score", "confidence", "type", "valid_from",
                          "valid_to", "preview"}, ...]}

        Score is the blended relevance; ``confidence`` and the bitemporal validity
        are m3's own per-memory signal. No component breakdown is fabricated.
        """
        docs = self._search(query)
        return {
            "query": query,
            "config": {
                "k": self.k, "type_filter": self.type_filter or None,
                "as_of": self.as_of or None, "recency_bias": self.recency_bias,
                "scope": self.scope,
            },
            "results": [
                {
                    "id": d.metadata.get("id"),
                    "score": d.metadata.get("score"),
                    "confidence": d.metadata.get("confidence"),
                    "type": d.metadata.get("type"),
                    "valid_from": d.metadata.get("valid_from"),
                    "valid_to": d.metadata.get("valid_to"),
                    "preview": (d.page_content or "")[:80],
                }
                for d in docs
            ],
        }

    # ── BaseRetriever contract ────────────────────────────────────────────────
    def _get_relevant_documents(
        self, query: str, *, run_manager: "CallbackManagerForRetrieverRun"
    ) -> list[Document]:
        return self._search(query)

    async def _aget_relevant_documents(
        self, query: str, *, run_manager: "AsyncCallbackManagerForRetrieverRun"
    ) -> list[Document]:
        # _call_impl rides the shared loop-thread; safe to call from the sync body.
        return self._search(query)
