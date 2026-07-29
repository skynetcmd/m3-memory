"""Hybrid search over leaves in files.db.

FTS5 full-text channel + vector cosine channel + Reciprocal Rank Fusion
(RRF). Default filter is non-superseded (current versions only); pass
include_history=True for time-travel queries.

Phase-1 ranking is intentionally simple: RRF over the top-K from each
channel, then return up to `limit` results. MMR diversity rerank is a
phase-2 upgrade (we can crib it from bin/memory/search.py once the
straightforward path proves it earns the complexity).

Public API:
    files_search(query, **opts) -> list[SearchHit]
    SearchHit — dataclass
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from .db import _db
from .embed import embed_texts

logger = logging.getLogger("files_memory.search")


@dataclass
class SearchHit:
    """One ranked result from files_search.

    `path` is the ingested path (what we mined). `original_path` is the
    user-facing source path when set via sidecar or --original-path;
    None means the ingested file is its own original. UIs should prefer
    `original_path` for citations and fall back to `path` when None.
    """
    leaf_uuid: str
    file_node_uuid: str
    filename: str
    path: str
    division_type: str
    division_id: str
    division_label: Optional[str]
    text: str
    score: float
    fts_rank: Optional[int] = None
    vec_rank: Optional[int] = None
    original_path: Optional[str] = None
    corpus_id: Optional[str] = None
    metadata: dict = field(default_factory=dict)


def _corpus_filter_clause(
    corpus_id: Optional[str],
    corpora: Optional[list[str]],
    _d=None,
) -> tuple[str, list]:
    """Build the SQL fragment + params for corpus filtering.

    - corpora (list) takes precedence: emits `fn.corpus_id IN (p,p,...)`.
    - else corpus_id (single string): emits `fn.corpus_id = p`.
    - else: empty filter.
    Returns ("", []) when no filter applies. ``_d`` is the active dialect
    (placeholders); defaults to the active backend's dialect when omitted.
    """
    if _d is None:
        from memory.backends import dialect as _dialect
        _d = _dialect()
    if corpora:
        clean = [c for c in corpora if c]
        if not clean:
            return ("", [])
        placeholders = _d.placeholder(len(clean))
        return (f" AND fn.corpus_id IN ({placeholders})", list(clean))
    if corpus_id:
        return (f" AND fn.corpus_id = {_d.param()}", [corpus_id])
    return ("", [])


def _fts_query(conn, query: str, limit: int,
               current_only: bool, corpus_id: Optional[str],
               filetype: Optional[str],
               corpora: Optional[list[str]] = None) -> list[tuple[str, float]]:
    """Keyword full-text channel. Returns [(leaf_uuid, score)] in rank order,
    score HIGHER = better (the fusion convention) on both backends.

    Backend-routed via the storage seam (decision 2026-07-25 — native full-text
    on both, NO pg_search dependency):
      * SQLite: FTS5 ``leaves_fts MATCH ?`` ranked by ``bm25()`` (lower=better,
        negated to higher=better).
      * PostgreSQL: the generated ``leaves.search_vector`` tsvector matched with
        ``@@ to_tsquery('english', ...)`` and ranked with ``ts_rank`` (higher=
        better already). No FTS5, no pg_search — native tsvector/GIN (pg_004).
    """
    from memory.backends import dialect as _dialect
    _d = _dialect()
    is_pg = _d.backend != "sqlite"
    if is_pg:
        return _fts_query_pg(conn, query, limit, current_only, corpus_id,
                             filetype, corpora, _d)
    return _fts_query_sqlite(conn, query, limit, current_only, corpus_id,
                             filetype, corpora, _d)


def _fts_query_sqlite(conn, query, limit, current_only, corpus_id, filetype,
                      corpora, _d) -> list[tuple[str, float]]:
    """SQLite FTS5 arm. bm25() is LOWER=better; negated so higher=better."""
    # Sanitize query for FTS5: treat the query as a permissive token match.
    safe = " ".join(t for t in query.split() if t.replace("-", "").replace("_", "").isalnum())
    if not safe:
        return []

    sql = (
        "SELECT l.uuid, bm25(leaves_fts) AS rank "
        "FROM leaves_fts "
        "JOIN leaves l ON l.rowid = leaves_fts.rowid "
        "JOIN file_nodes fn ON fn.uuid = l.file_node "
        "WHERE leaves_fts MATCH ? "
    )
    params: list = [safe]
    if current_only:
        sql += " AND l.superseded_by IS NULL AND fn.superseded_by IS NULL"
    corpus_clause, corpus_params = _corpus_filter_clause(corpus_id, corpora, _d)
    sql += corpus_clause
    params.extend(corpus_params)
    if filetype:
        sql += " AND fn.filetype = ?"
        params.append(filetype)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        # FTS5 syntax error: fall back to no FTS hits.
        logger.debug("FTS5 query failed (%s); skipping channel", e)
        return []
    return [(r["uuid"], -r["rank"]) for r in rows]


def _fts_query_pg(conn, query, limit, current_only, corpus_id, filetype,
                  corpora, _d) -> list[tuple[str, float]]:
    """PostgreSQL tsvector arm. Matches leaves.search_vector @@ to_tsquery and
    ranks with ts_rank (already higher=better). Reuses the core query compiler
    (_compile_tsquery) so query terms normalize identically to the FTS5 path."""
    from memory.fts import _compile_tsquery

    from .config import files_table

    tsquery, ok = _compile_tsquery(query, "fts5")
    if not ok or not tsquery:
        return []
    _p = _d.param()
    _lv = files_table("leaves")
    _fn = files_table("file_nodes")
    sql = (
        f"SELECT l.uuid AS uuid, "
        f"       ts_rank(l.search_vector, to_tsquery('english', {_p})) AS rank "
        f"FROM {_lv} l "
        f"JOIN {_fn} fn ON fn.uuid = l.file_node "
        f"WHERE l.search_vector @@ to_tsquery('english', {_p}) "
    )
    params: list = [tsquery, tsquery]
    if current_only:
        sql += " AND l.superseded_by IS NULL AND fn.superseded_by IS NULL"
    corpus_clause, corpus_params = _corpus_filter_clause(corpus_id, corpora, _d)
    sql += corpus_clause
    params.extend(corpus_params)
    if filetype:
        sql += f" AND fn.filetype = {_p}"
        params.append(filetype)
    sql += f" ORDER BY rank DESC LIMIT {_p}"
    params.append(limit)

    try:
        rows = conn.execute(sql, params).fetchall()
    except Exception as e:  # noqa: BLE001 — a tsquery error skips the channel
        if not _d.is_undefined_object_error(e):
            logger.debug("tsvector query failed (%s); skipping channel", e)
        return []
    # ts_rank is already higher=better; keep as-is for fusion.
    return [(r["uuid"], float(r["rank"])) for r in rows]


def _vec_query(conn: sqlite3.Connection, query: str, limit: int,
               current_only: bool, corpus_id: Optional[str],
               filetype: Optional[str],
               corpora: Optional[list[str]] = None) -> list[tuple[str, float]]:
    """Vector cosine search. Returns [(leaf_uuid, cosine_score)].

    Embeds the query then scans the text-kind embeddings, computing
    cosine for each. SQLite has no native vector index in this DB — we
    scan. Phase-2 upgrade: sqlite-vec or a precomputed IVF index.
    """
    vecs = embed_texts([query])
    if not vecs or vecs[0][0] is None:
        return []
    qvec, qmodel = vecs[0]

    from memory.backends import dialect as _dialect

    from .config import files_table
    _d = _dialect()
    _p = _d.param()
    _le = files_table("leaf_embeddings")
    _lv = files_table("leaves")
    _fn = files_table("file_nodes")
    sql = (
        f"SELECT le.leaf_uuid, le.embedding "
        f"FROM {_le} le "
        f"JOIN {_lv} l ON l.uuid = le.leaf_uuid "
        f"JOIN {_fn} fn ON fn.uuid = l.file_node "
        f"WHERE le.kind = 'text' AND le.embed_model = {_p}"
    )
    params: list = [qmodel]
    if current_only:
        sql += " AND l.superseded_by IS NULL AND fn.superseded_by IS NULL"
    corpus_clause, corpus_params = _corpus_filter_clause(corpus_id, corpora, _d)
    sql += corpus_clause
    params.extend(corpus_params)
    if filetype:
        sql += f" AND fn.filetype = {_p}"
        params.append(filetype)

    rows = conn.execute(sql, params).fetchall()
    # Route ranking through the canonical Rust-backed batch cosine — a single FFI
    # hop (numpy / pure-Python fallback inside), the SAME path memory vector
    # search uses — instead of a per-row Python cosine loop. A wrong-length blob
    # scores 0.0 in every path. Candidates are still the full compatible-embedding
    # set (the add-on-free brute-force baseline; an sqlite-vec/pgvector ANN index
    # is the Phase-4 accelerator), but scored in one Rust call, not N Python ones.
    pairs = [(r["leaf_uuid"], r["embedding"]) for r in rows if r["embedding"] is not None]
    if not pairs:
        return []
    from memory.util import _cosine_batch_packed
    uuids = [p[0] for p in pairs]
    # psycopg returns BYTEA as memoryview; normalize to bytes like the seam does.
    blobs = [bytes(b) if isinstance(b, memoryview) else b for _, b in pairs]
    scores = _cosine_batch_packed(qvec, blobs, len(qvec))
    scored = list(zip(uuids, (float(s) for s in scores)))
    scored.sort(key=lambda x: -x[1])
    return scored[:limit]


def _rrf_fuse(
    fts: list[tuple[str, float]],
    vec: list[tuple[str, float]],
    k: int = 60,
) -> dict[str, dict]:
    """Reciprocal Rank Fusion. Returns {uuid: {'score', 'fts_rank', 'vec_rank'}}.

    RRF formula: score(d) = sum over channels of 1 / (k + rank_in_channel).
    k=60 is the canonical default — robust, doesn't need tuning per corpus.
    """
    fused: dict[str, dict] = {}
    for rank, (uid, _s) in enumerate(fts, start=1):
        fused.setdefault(uid, {"score": 0.0, "fts_rank": None, "vec_rank": None})
        fused[uid]["score"] += 1.0 / (k + rank)
        fused[uid]["fts_rank"] = rank
    for rank, (uid, _s) in enumerate(vec, start=1):
        fused.setdefault(uid, {"score": 0.0, "fts_rank": None, "vec_rank": None})
        fused[uid]["score"] += 1.0 / (k + rank)
        fused[uid]["vec_rank"] = rank
    return fused


def files_search(
    query: str,
    *,
    limit: int = 10,
    corpus_id: Optional[str] = None,
    corpora: Optional[list[str]] = None,
    filetype: Optional[str] = None,
    include_history: bool = False,
    channel_limit: int = 50,
    db_path: Optional[str] = None,
) -> list[SearchHit]:
    """Hybrid search over leaves.

    Args:
        query: free text.
        limit: number of hits to return.
        corpus_id: single-corpus scope filter.
        corpora: list of corpus IDs to fan out across. When set, overrides
            corpus_id; results from all listed corpora are fused into one
            ranked list.
        filetype: filter by file_nodes.filetype.
        include_history: if False (default), filter superseded leaves
            and file_nodes out.
        channel_limit: per-channel top-K before fusion. Larger = better
            recall, slower. 50 is a sane default for corpora ≤ 100k leaves.
        db_path: target files.db.
    """
    if not query or not query.strip():
        return []

    current_only = not include_history
    from memory.backends import dialect as _dialect

    from .config import files_table
    _d = _dialect()
    with _db(db_path) as conn:
        # No forced conn.row_factory: the SQLite files connection already carries
        # row_factory=Row (files_memory.db._new_connection) and the PG compat
        # cursor yields dual name/position rows — so the name access below works on
        # both; forcing sqlite3.Row would break PG.
        fts = _fts_query(conn, query, channel_limit, current_only, corpus_id, filetype, corpora=corpora)
        vec = _vec_query(conn, query, channel_limit, current_only, corpus_id, filetype, corpora=corpora)
        fused = _rrf_fuse(fts, vec)

        if not fused:
            return []

        # Pull leaf + file_node metadata for the top `limit` by score.
        top = sorted(fused.items(), key=lambda kv: -kv[1]["score"])[:limit]
        if not top:
            return []
        uuids = [u for u, _ in top]
        placeholders = _d.placeholder(len(uuids))
        _lv = files_table("leaves")
        _fn = files_table("file_nodes")
        rows = conn.execute(
            f"SELECT l.uuid AS leaf_uuid, l.file_node, l.division_type, "
            f"  l.division_id, l.division_label, l.text, "
            f"  fn.filename, fn.path_absolute AS path, fn.metadata AS fn_metadata, "
            f"  fn.corpus_id AS corpus_id "
            f"FROM {_lv} l "
            f"JOIN {_fn} fn ON fn.uuid = l.file_node "
            f"WHERE l.uuid IN ({placeholders})",
            uuids,
        ).fetchall()
        row_map = {r["leaf_uuid"]: r for r in rows}

        from .provenance import original_path_for_metadata
        hits: list[SearchHit] = []
        for uid, meta in top:
            row = row_map.get(uid)
            if row is None:
                continue
            hits.append(SearchHit(
                leaf_uuid=row["leaf_uuid"],
                file_node_uuid=row["file_node"],
                filename=row["filename"],
                path=row["path"],
                division_type=row["division_type"],
                division_id=row["division_id"],
                division_label=row["division_label"],
                text=row["text"],
                score=meta["score"],
                fts_rank=meta["fts_rank"],
                vec_rank=meta["vec_rank"],
                original_path=original_path_for_metadata(row["fn_metadata"]),
                corpus_id=row["corpus_id"],
            ))

        # Promotion-suggestion bookkeeping: record a hit for every fact
        # whose leaf surfaced. Skipped in include_history mode (those
        # are explicit history queries, not "what's promotable now").
        if hits and not include_history:
            try:
                from .promotability import record_leaf_hits
                record_leaf_hits(conn, [h.leaf_uuid for h in hits])
            except Exception as e:
                logger.debug("record_leaf_hits failed (non-fatal): %s", e)

        return hits
