"""PostgreSQL candidate fetch for the scored search path.

The SQLite search path fuses keyword (FTS5 ``MATCH`` + ``bm25()``) and vector
(sqlite-vec ``vec_distance_cosine``) in a SINGLE SQL statement. None of those
primitives exist on PostgreSQL, so this module reproduces the *candidate-fetch
stage* through the backend seam instead — ``keyword_search`` (tsvector) and
``vector_search`` (BYTEA + Rust cosine) — then returns candidate rows in the
EXACT dict shape the shared downstream scorer expects. Everything after fetch
(``_hybrid_score_batch``, role boosts, MMR, ranking) is backend-agnostic and
runs unchanged.

Contract: returns ``(rows, has_vec=False)`` where each row is a dict with keys
``id, content, title, type, importance, embedding, bm25_score, metadata_json``
(+ any ``extra_columns``). ``has_vec`` is always False — PG has no sqlite-vec, so
the caller computes cosine downstream via ``_cosine_batch_packed`` exactly as it
does on the SQLite no-vec path. This keeps PG on the SAME functional flow, only
the fetch differs.

Scope boundary (see project memory): this is backend PORTABILITY only. It changes
nothing about what is stored or how tenancy/identity work — it consumes the same
user_id/scope the SQLite path does, in %s form.
"""
from __future__ import annotations

from typing import Any

# bm25 sentinel for candidates found ONLY by the vector search (no keyword hit).
# The SQLite path gives every fts-matched row a real bm25 and vector-only rows
# never enter the fts join; here we union both, so a vector-only row needs a
# neutral bm25 that _hybrid_score_batch treats as "no keyword signal". SQLite
# bm25 is <= 0 (more negative = better); 0.0 is the weakest, matching the
# "0.0 as bm25_score" the SQLite semantic (no-fts) branch already uses.
_NO_KEYWORD_BM25 = 0.0


def fetch_candidates_pg(
    backend: Any,
    conn: Any,
    *,
    query: str,
    q_vec: "list[float]",
    search_mode: str,
    where_columns: str,
    dim: int,
    embed_models: "tuple[str, ...]",
    tenancy_sql: str,
    tenancy_params: "tuple[Any, ...]",
    row_limit: int,
    extra_columns: "list[str] | None" = None,
) -> "tuple[list[dict], bool]":
    """Fetch scored-search candidates on PostgreSQL via the seam.

    ``where_columns`` is the extra SELECT column list (already ``mi.``-qualified,
    leading comma) mirroring the SQLite ``extra_sql``. ``search_mode`` is
    ``semantic`` | ``hybrid`` | ``fts5``:
      * semantic  -> vector candidates only
      * fts5      -> keyword candidates only
      * hybrid    -> union of both (keyword bm25 + vector cosine per row)
    Returns ``(rows, False)``.
    """
    extra_columns = extra_columns or []

    # 1. gather candidate ids from each modality via the seam.
    kw_by_id: dict[str, float] = {}
    if search_mode in ("hybrid", "fts5"):
        for hit in backend.keyword_search(
            conn, query, limit=row_limit,
            tenancy_sql=tenancy_sql, tenancy_params=tenancy_params,
        ):
            kw_by_id[hit.memory_id] = hit.score

    vec_ids: list[str] = []
    if search_mode in ("hybrid", "semantic"):
        for hit in backend.vector_search(
            conn, q_vec, limit=row_limit, dim=dim, embed_models=embed_models,
            tenancy_sql=tenancy_sql, tenancy_params=tenancy_params,
        ):
            vec_ids.append(hit.memory_id)

    candidate_ids = list(dict.fromkeys([*kw_by_id.keys(), *vec_ids]))  # ordered unique
    if not candidate_ids:
        return [], False
    if len(candidate_ids) > row_limit:
        candidate_ids = candidate_ids[:row_limit]

    # 2. hydrate the full rows (with embeddings) for the downstream scorer.
    extra_select = "".join(f", mi.{c}" for c in extra_columns)
    placeholders = ", ".join(["%s"] * len(candidate_ids))
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT mi.id, mi.content, mi.title, mi.type, mi.importance,
               me.embedding, mi.metadata_json{extra_select}
        FROM memory_items mi
        JOIN memory_embeddings me ON mi.id = me.memory_id
        WHERE mi.id IN ({placeholders}) AND mi.is_deleted = 0 AND me.dim = %s
        """,
        (*candidate_ids, dim),
    )
    colnames = [d[0] for d in cur.description]
    rows: list[dict] = []
    for raw in cur.fetchall():
        row = dict(zip(colnames, raw))
        # psycopg BYTEA -> memoryview; normalize to bytes for the Rust scorer.
        emb = row.get("embedding")
        if isinstance(emb, memoryview):
            row["embedding"] = bytes(emb)
        # metadata_json is JSONB -> psycopg returns a dict; the SQLite path holds
        # TEXT and downstream does substring checks like '"role"' in meta_raw.
        # Re-serialize to a JSON string so the downstream code is identical.
        meta = row.get("metadata_json")
        if meta is not None and not isinstance(meta, str):
            import json

            row["metadata_json"] = json.dumps(meta)
        # bm25: keyword score if this row was a keyword hit, else neutral.
        row["bm25_score"] = kw_by_id.get(row["id"], _NO_KEYWORD_BM25)
        rows.append(row)

    # Order to mirror the SQLite intent: keyword-ranked first (bm25 asc = better),
    # then vector-only candidates. The downstream _hybrid_score_batch re-scores
    # everything, so this is just a stable pre-order, not the final ranking.
    kw_order = {mid: i for i, mid in enumerate(kw_by_id)}
    rows.sort(key=lambda r: kw_order.get(r["id"], len(kw_order)))
    return rows, False
