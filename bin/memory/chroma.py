"""Chroma federation helpers.

Phase 4.A of the migration. Holds:

  - `_queue_chroma` — write-side helper that enqueues a memory_id into
    `chroma_sync_queue` for the background sync worker. Called from the
    write/bulk paths.
  - `_CHROMA_COLLECTION_ID_CACHE`, `_resolve_chroma_collection_id` —
    process-wide cache of (base_url, collection) -> UUID so federated
    queries don't pay the resolve round-trip on every call.
  - `_query_chroma` — federation read path: posts a vector query to the
    remote Chroma instance, returns federated rows for the result-set
    fusion in `memory_search_scored_impl`.

Subtle dependency: `_query_chroma` uses `_get_embed_client()` from
memory.embed for its httpx client (NOT a separate Chroma client) so
it shares the pool tuning and keepalive expiry with embed traffic. That
import is one-way: memory.embed does NOT need anything from memory.chroma.
"""
from __future__ import annotations

import logging

from . import config
from .db import _db
from .embed import _get_embed_client

logger = logging.getLogger("memory.chroma")

# Process-wide cache: (base_url, collection_name) -> Chroma collection UUID.
# Invalidated on any 4xx / connection error so the next call re-resolves.
_CHROMA_COLLECTION_ID_CACHE: dict[tuple[str, str], str] = {}


def _queue_chroma(memory_id: str, operation: str) -> None:
    """Enqueue a memory_id into chroma_sync_queue for the background sync.

    Best-effort: logs at DEBUG and continues on failure so a Chroma outage
    can't block the write path.
    """
    try:
        with _db() as db:
            db.execute(
                "INSERT INTO chroma_sync_queue (memory_id, operation) VALUES (?,?)",
                (memory_id, operation),
            )
    except Exception as e:
        logger.debug(f"ChromaDB queue insert failed: {e}")


async def _resolve_chroma_collection_id(client, base_url: str, collection: str) -> str | None:
    """Resolve and cache a Chroma collection UUID for the process lifetime.

    A missing / 4xx response invalidates the cache slot so the next call
    re-resolves. The previous code paid one extra round-trip per federated
    search — meaningful when the local pool is weak and federation fires on
    every other query.
    """
    key = (base_url, collection)
    cached = _CHROMA_COLLECTION_ID_CACHE.get(key)
    if cached:
        return cached
    resp = await client.get(
        f"{base_url}{config.CHROMA_V2_PREFIX}/{collection}",
        timeout=config.CHROMA_CONNECT_T,
    )
    resp.raise_for_status()
    col_id = resp.json().get("id")
    if col_id:
        _CHROMA_COLLECTION_ID_CACHE[key] = col_id
    return col_id


async def _query_chroma(
    query_vec: list[float],
    k: int = 5,
    scope_filter: dict | None = None,
) -> list[dict]:
    """Queries the remote ChromaDB instance for federated results.

    Args:
        query_vec: Embedding vector for the query.
        k: Maximum number of results to return.
        scope_filter: Optional dict of {field: value} pairs to filter results
            by metadata (e.g. {'user_id': ..., 'scope': ..., 'agent_id': ...}).
            Empty/None values are skipped. Translated to ChromaDB v2 where syntax.
    """
    if not config.CHROMA_BASE_URL or not config.CHROMA_BASE_URL.startswith("http"):
        return []
    try:
        client = _get_embed_client()
        # 1. Resolve collection ID (cached for the process lifetime; invalidated
        #    on any error below).
        col_id = await _resolve_chroma_collection_id(
            client, config.CHROMA_BASE_URL, config.CHROMA_COLLECTION,
        )
        if not col_id:
            logger.warning("ChromaDB collection response missing 'id' field")
            return []

        # 2. Build query payload
        col_path = f"{config.CHROMA_BASE_URL}{config.CHROMA_V2_PREFIX}/{col_id}"
        payload: dict = {
            "query_embeddings": [query_vec],
            "n_results": k,
            "include": ["documents", "metadatas", "distances"],
        }

        # Translate scope_filter to ChromaDB v2 where-clause syntax
        source_tag = "federated_chroma_unscoped"
        if scope_filter:
            where_clauses = []
            for field, value in scope_filter.items():
                if value:  # skip empty strings / None
                    where_clauses.append({field: {"$eq": value}})
            if where_clauses:
                payload["where"] = (
                    where_clauses[0]
                    if len(where_clauses) == 1
                    else {"$and": where_clauses}
                )
                source_tag = "federated_chroma_scoped"

        # 3. Perform query
        query_resp = await client.post(
            f"{col_path}/query", json=payload, timeout=config.CHROMA_READ_T,
        )
        query_resp.raise_for_status()

        data = query_resp.json()
        results = []
        if data["ids"] and data["ids"][0]:
            for i in range(len(data["ids"][0])):
                # Chroma distance is often squared L2, but we'll treat it as a score component
                score = 1.0 - (data["distances"][0][i] / 2.0) if data["distances"] else 0.5
                results.append({
                    "id": data["ids"][0][i],
                    "content": data["documents"][0][i],
                    "title": data["metadatas"][0][i].get("title", ""),
                    "type": data["metadatas"][0][i].get("type", "federated"),
                    "score": score,
                    "_explanation": {"source": source_tag},
                })
        return results
    except Exception as e:
        logger.debug(f"ChromaDB federated query failed: {e}")
        # Drop any cached collection UUID — a 404/connection error may mean
        # the collection was recreated with a new id.
        _CHROMA_COLLECTION_ID_CACHE.pop(
            (config.CHROMA_BASE_URL, config.CHROMA_COLLECTION), None,
        )
        return []
